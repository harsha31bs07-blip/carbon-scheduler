"""Versioned caller-supplied API tariffs; no guessed prices or ownership costs.

Provider model IDs are exact. Token usage may remain unknown after a failed
attempt; a nonzero tariff never converts that missing usage to a zero charge.
The only known zero with missing usage is an explicitly all-zero API tariff.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from shared.deployments import PROVIDERS
from shared.scheduling import MetricEstimate


TARIFF_SCHEMA_VERSION = 1


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _rate(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and nonnegative")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and nonnegative")


def _usage(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a nonnegative integer or unknown")


def _fingerprint(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
        raise ValueError("deployment_fingerprint must be a lowercase SHA-256 fingerprint or unknown")


def _unknown(reason: str) -> MetricEstimate:
    return MetricEstimate(None, "USD", "unknown", f"api-only:{reason}; electricity-excluded")


@dataclass(frozen=True)
class ApiTariff:
    """API input/output rates in USD per million tokens, for a bounded interval.

    A deployment fingerprint binds a rate to that exact deployment configuration.
    None means a provider-wide rate for the exact provider model, not an inferred
    deployment match. Validity is half-open and ``verified_at`` gates use as-of.
    """

    provider: str
    model_id: str
    input_usd_per_million: float
    output_usd_per_million: float
    source: str
    verified_at: datetime
    valid_from: datetime
    valid_until: datetime
    deployment_fingerprint: str | None = None
    scope: str = "api-only"

    def __post_init__(self):
        if not isinstance(self.provider, str) or self.provider not in PROVIDERS:
            raise ValueError("unsupported tariff provider")
        _text(self.model_id, "model_id")
        _text(self.source, "source")
        if self.scope != "api-only":
            raise ValueError("tariffs must explicitly cover api-only costs")
        for name in ("input_usd_per_million", "output_usd_per_million"):
            _rate(getattr(self, name), name)
        for name in ("verified_at", "valid_from", "valid_until"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.valid_until <= self.valid_from:
            raise ValueError("tariff validity interval must be nonempty")
        _fingerprint(self.deployment_fingerprint)

    def valid_at(self, instant: datetime, *, as_of: datetime) -> bool:
        return (self.verified_at <= _utc(as_of, "as_of")
                and self.valid_from <= _utc(instant, "instant") < self.valid_until)

    def to_dict(self) -> dict:
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        for name in ("verified_at", "valid_from", "valid_until"):
            result[name] = result[name].isoformat().replace("+00:00", "Z")
        return {"schema_version": TARIFF_SCHEMA_VERSION, "record_type": "ApiTariff", **result}

    @classmethod
    def from_dict(cls, value: Mapping) -> ApiTariff:
        expected = {field.name for field in fields(cls)} | {"schema_version", "record_type"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("tariff requires all supported fields only")
        if type(value["schema_version"]) is not int or value["schema_version"] != TARIFF_SCHEMA_VERSION:
            raise ValueError("unsupported tariff schema_version")
        if value["record_type"] != "ApiTariff":
            raise ValueError("expected ApiTariff record")
        data = {field.name: value[field.name] for field in fields(cls)}
        for name in ("verified_at", "valid_from", "valid_until"):
            if not isinstance(data[name], str):
                raise ValueError("tariff timestamps must be ISO 8601 strings")
            try:
                data[name] = datetime.fromisoformat(data[name].replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("tariff timestamps must be ISO 8601 strings") from None
        return cls(**data)


def cost_from_usage(tariff: ApiTariff, input_tokens: int | None, output_tokens: int | None, *,
                    provider: str, model_id: str, instant: datetime, as_of: datetime,
                    deployment_fingerprint: str | None = None) -> MetricEstimate:
    """Calculate API cost for one actual attempt; missing nonfree usage is unknown."""
    if not isinstance(tariff, ApiTariff):
        raise ValueError("tariff must be ApiTariff")
    _usage(input_tokens, "input_tokens")
    _usage(output_tokens, "output_tokens")
    _fingerprint(deployment_fingerprint)
    instant, as_of = _utc(instant, "instant"), _utc(as_of, "as_of")
    if provider != tariff.provider or model_id != tariff.model_id:
        return _unknown("tariff-model-mismatch")
    if tariff.deployment_fingerprint is not None and tariff.deployment_fingerprint != deployment_fingerprint:
        return _unknown("tariff-deployment-mismatch")
    if tariff.verified_at > as_of:
        return _unknown("tariff-not-yet-verified")
    if not tariff.valid_from <= instant < tariff.valid_until:
        return _unknown("tariff-outside-validity")
    free = tariff.input_usd_per_million == tariff.output_usd_per_million == 0
    if not free and (input_tokens is None or output_tokens is None):
        return _unknown("missing-token-usage")
    # Short circuit explicit free rates before arithmetic: usage may be missing
    # or an arbitrarily large valid integer without changing this API-only zero.
    if free:
        cost = 0.0
    else:
        try:
            cost = (input_tokens / 1_000_000 * tariff.input_usd_per_million
                    + output_tokens / 1_000_000 * tariff.output_usd_per_million)
        except OverflowError:
            return _unknown("cost-overflow")
        if not math.isfinite(cost):
            return _unknown("cost-overflow")
    return MetricEstimate(cost, "USD", "estimated", f"{tariff.source}; api-only; electricity-excluded")


@dataclass(frozen=True, init=False)
class TariffBook:
    """Exact model tariff lookup. Overlapping applicable rates fail as ambiguous."""

    tariffs: tuple[ApiTariff, ...]

    def __init__(self, tariffs: tuple[ApiTariff, ...]):
        if not isinstance(tariffs, tuple) or any(not isinstance(item, ApiTariff) for item in tariffs):
            raise ValueError("tariffs must be a tuple of ApiTariff records")
        for index, current in enumerate(tariffs):
            for other in tariffs[:index]:
                same_model = (current.provider, current.model_id) == (other.provider, other.model_id)
                shared_scope = (current.deployment_fingerprint is None or other.deployment_fingerprint is None
                                or current.deployment_fingerprint == other.deployment_fingerprint)
                overlap = current.valid_from < other.valid_until and other.valid_from < current.valid_until
                if same_model and shared_scope and overlap:
                    raise ValueError("tariffs have ambiguous overlapping coverage")
        object.__setattr__(self, "tariffs", tariffs)

    def estimate_usage(self, provider: str, model_id: str, input_tokens: int | None,
                       output_tokens: int | None, *, instant: datetime, as_of: datetime,
                       deployment_fingerprint: str | None = None) -> MetricEstimate:
        _usage(input_tokens, "input_tokens")
        _usage(output_tokens, "output_tokens")
        _fingerprint(deployment_fingerprint)
        instant, as_of = _utc(instant, "instant"), _utc(as_of, "as_of")
        rows = [tariff for tariff in self.tariffs if tariff.provider == provider and tariff.model_id == model_id
                and (tariff.deployment_fingerprint is None or tariff.deployment_fingerprint == deployment_fingerprint)]
        if not rows:
            return _unknown("missing-tariff")
        covering = next((tariff for tariff in rows if tariff.valid_from <= instant < tariff.valid_until), None)
        if covering is None:
            return _unknown("tariff-outside-validity")
        return cost_from_usage(covering, input_tokens, output_tokens, provider=provider, model_id=model_id,
                               instant=instant, as_of=as_of, deployment_fingerprint=deployment_fingerprint)

    def to_dict(self) -> dict:
        rows = [tariff.to_dict() for tariff in self.tariffs]
        rows.sort(key=lambda row: (row["provider"], row["model_id"], row["deployment_fingerprint"] or "", row["valid_from"]))
        return {"schema_version": TARIFF_SCHEMA_VERSION, "record_type": "TariffBook", "tariffs": rows}

    def fingerprint(self) -> str:
        data = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping) -> TariffBook:
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "record_type", "tariffs"}:
            raise ValueError("tariff book requires all supported fields only")
        if type(value["schema_version"]) is not int or value["schema_version"] != TARIFF_SCHEMA_VERSION:
            raise ValueError("unsupported tariff book schema_version")
        if value["record_type"] != "TariffBook" or not isinstance(value["tariffs"], list):
            raise ValueError("invalid tariff book record")
        return cls(tuple(ApiTariff.from_dict(item) for item in value["tariffs"]))

    @classmethod
    def load(cls, path: str | Path) -> TariffBook:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate tariff field")
                result[key] = value
            return result
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
        except json.JSONDecodeError:
            raise ValueError("tariff book is not valid JSON") from None
        return cls.from_dict(value)


def local_api_tariff(model_id: str, *, verified_at: datetime, valid_from: datetime,
                     valid_until: datetime, deployment_fingerprint: str | None = None) -> ApiTariff:
    """Explicit zero Ollama API fee, excluding electricity and machine ownership."""
    return ApiTariff("ollama", model_id, 0, 0, "local-no-api-subscription", verified_at,
                     valid_from, valid_until, deployment_fingerprint)
