"""Explicit carbon intensity records with issue-time and coverage checks.

Loading a trace never contacts a provider or assigns a physical grid. Samples
retain their supplied provenance, including ``simulated`` fixtures. All validity
intervals are half-open; an unavailable value stays unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from shared.scheduling import CarbonSample


CARBON_TRACE_VERSION = 1


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _max_age(value: timedelta) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError("max_age must be a positive timedelta")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate carbon trace field")
        result[key] = value
    return result


@dataclass(frozen=True)
class CarbonAvailability:
    """Reason for a missing sample, separate from numeric carbon intensity."""

    status: str
    sample: CarbonSample | None = None

    def __post_init__(self):
        if not isinstance(self.status, str) or self.status not in {
            "unknown-region", "missing-region", "missing-coverage", "not-yet-issued", "stale", "available"
        }:
            raise ValueError("unsupported carbon availability status")
        if ((self.status == "available" and not isinstance(self.sample, CarbonSample))
                or (self.status != "available" and self.sample is not None)):
            raise ValueError("only available carbon status can contain a sample")


@dataclass(frozen=True, init=False)
class HistoricalCarbonTrace:
    """Versioned, caller-supplied samples without synthetic or geographic fallback.

    A region must have unambiguous time coverage. Overlapping revisions should
    be supplied in separate trace snapshots rather than silently selecting one.
    ``max_age`` measures freshness against the sample's issue time at ``as_of``.
    """

    trace_id: str
    samples: tuple[CarbonSample, ...]
    max_age: timedelta

    def __init__(self, trace_id: str, samples: tuple[CarbonSample, ...], max_age: timedelta):
        _text(trace_id, "trace_id")
        _max_age(max_age)
        if not isinstance(samples, tuple) or any(not isinstance(item, CarbonSample) for item in samples):
            raise ValueError("samples must be a tuple of CarbonSample records")
        ordered = tuple(sorted(samples, key=lambda item: (item.region, item.valid_from)))
        for previous, current in zip(ordered, ordered[1:]):
            if previous.region == current.region:
                if previous.unit != current.unit:
                    raise ValueError("carbon trace cannot mix greenhouse-gas units within a region")
                if current.valid_from < previous.valid_until:
                    raise ValueError("carbon trace has ambiguous overlapping coverage")
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "samples", ordered)
        object.__setattr__(self, "max_age", max_age)

    def availability(self, region: str | None, instant: datetime, *, as_of: datetime) -> CarbonAvailability:
        instant, as_of = _utc(instant, "instant"), _utc(as_of, "as_of")
        if region is None:
            return CarbonAvailability("unknown-region")
        _text(region, "region")
        regional = tuple(sample for sample in self.samples if sample.region == region)
        if not regional:
            return CarbonAvailability("missing-region")
        covering = next((sample for sample in regional if sample.valid_from <= instant < sample.valid_until), None)
        if covering is None:
            return CarbonAvailability("missing-coverage")
        if covering.issued_at > as_of:
            return CarbonAvailability("not-yet-issued")
        if as_of - covering.issued_at > self.max_age:
            return CarbonAvailability("stale")
        return CarbonAvailability("available", covering)

    def sample(self, region: str | None, instant: datetime, *, as_of: datetime) -> CarbonSample | None:
        """Implement CarbonProvider; all availability failures return None."""
        return self.availability(region, instant, as_of=as_of).sample

    def to_dict(self) -> dict:
        return {"schema_version": CARBON_TRACE_VERSION, "record_type": "HistoricalCarbonTrace",
                "trace_id": self.trace_id, "max_age_seconds": self.max_age.total_seconds(),
                "samples": [sample.to_dict() for sample in self.samples]}

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping) -> HistoricalCarbonTrace:
        expected = {"schema_version", "record_type", "trace_id", "max_age_seconds", "samples"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("carbon trace requires only its versioned supported fields")
        if type(value["schema_version"]) is not int or value["schema_version"] != CARBON_TRACE_VERSION:
            raise ValueError("unsupported carbon trace schema_version")
        if value["record_type"] != "HistoricalCarbonTrace" or not isinstance(value["samples"], list):
            raise ValueError("invalid carbon trace record")
        age = value["max_age_seconds"]
        if isinstance(age, bool) or not isinstance(age, (int, float)):
            raise ValueError("max_age_seconds must be a positive finite number")
        try:
            max_age = timedelta(seconds=age)
        except (ValueError, OverflowError):
            raise ValueError("max_age_seconds must be a supported positive finite duration") from None
        return cls(value["trace_id"], tuple(CarbonSample.from_dict(item) for item in value["samples"]), max_age)

    @classmethod
    def load(cls, path: str | Path) -> HistoricalCarbonTrace:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
        except json.JSONDecodeError:
            raise ValueError("carbon trace is not valid JSON") from None
        return cls.from_dict(value)


@dataclass(frozen=True, init=False)
class StaticCarbonEstimate:
    """One explicit estimate with a source, known region and bounded validity.

    There is no built-in intensity, region, issue time or unlimited expiry. This
    is an estimate even when supplied by someone who has a measured trace.
    """

    estimate: CarbonSample
    _trace: HistoricalCarbonTrace

    def __init__(self, region: str, value: float, source: str, issued_at: datetime,
                 valid_from: datetime, valid_until: datetime, max_age: timedelta,
                 unit: str = "gCO2e/kWh"):
        estimate = CarbonSample(region, issued_at, valid_from, valid_until, value, source, "estimated", unit)
        object.__setattr__(self, "estimate", estimate)
        object.__setattr__(self, "_trace", HistoricalCarbonTrace("static-estimate", (estimate,), max_age))

    def availability(self, region: str | None, instant: datetime, *, as_of: datetime) -> CarbonAvailability:
        return self._trace.availability(region, instant, as_of=as_of)

    def sample(self, region: str | None, instant: datetime, *, as_of: datetime) -> CarbonSample | None:
        return self.availability(region, instant, as_of=as_of).sample

    def to_dict(self) -> dict:
        return {"schema_version": CARBON_TRACE_VERSION, "record_type": "StaticCarbonEstimate",
                "sample": self.estimate.to_dict(), "max_age_seconds": self._trace.max_age.total_seconds()}

    def fingerprint(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping) -> StaticCarbonEstimate:
        expected = {"schema_version", "record_type", "sample", "max_age_seconds"}
        if not isinstance(value, Mapping) or set(value) != expected or value["record_type"] != "StaticCarbonEstimate":
            raise ValueError("invalid static carbon estimate")
        trace = HistoricalCarbonTrace.from_dict({
            "schema_version": value["schema_version"], "record_type": "HistoricalCarbonTrace",
            "trace_id": "static-estimate", "max_age_seconds": value["max_age_seconds"],
            "samples": [value["sample"]],
        })
        sample = trace.samples[0]
        if sample.provenance != "estimated":
            raise ValueError("static carbon estimates must have estimated provenance")
        return cls(sample.region, sample.value, sample.source, sample.issued_at,
                   sample.valid_from, sample.valid_until, trace.max_age, sample.unit)
