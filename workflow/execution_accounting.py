"""Pure, conservative accounting of immutable provider attempts.

The host interval includes provider execution, durable raw journaling and tracker
cleanup. Its energy is machine energy, not a claim about exclusive model energy.
This module reads retained snapshots only and never loads provider transports.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import math
import re

from data.carbon_sources import HistoricalCarbonTrace, StaticCarbonEstimate
from data.cost_accounting import TariffBook, local_api_tariff
from data.quality_reviews import fingerprint
from shared.deployments import DeploymentRegistry
from shared.energy import is_loopback_endpoint
from shared.scheduling import CarbonSample, ExecutionAttempt, MetricEstimate, StepEstimates, WorkflowRequest


ACCOUNTING_SCOPE = "host-provider-and-journal.v1"
_HOST_SCOPE = "host-machine-including-idle-and-other-processes"
_CONTEXT_FIELDS = {"schema_version", "record_type", "tariff_book", "carbon",
                   "energy_mode", "energy_country", "scope"}
_RECORD_FIELDS = {"schema_version", "record_type", "workflow_id", "attempt_id", "attempt_sha256",
                  "context_fingerprint", "accounted_at", "measurement_start", "measurement_finish",
                  "journal_recorded_at", "energy_report", "metrics", "carbon_sample", "errors", "fingerprint"}
_REPORT_FIELDS = {"energy_wh", "energy_measured", "energy_method", "energy_provenance",
                  "energy_components", "energy_scope", "telemetry_error"}
_COMPONENTS = {"cpu", "gpu", "ram"}
_TELEMETRY_CODES = frozenset({
    "another-host-tracker-active", "tracker-start-failed", "tracker-stop-failed",
    "tracker-reading-failed", "tracker-reading-unavailable", "tracker-unavailable",
    "tracker-initialization-failed", "codecarbon-unavailable", "telemetry-unavailable",
    "energy-report-invalid", "energy-scope-unsupported", "energy-tracking-disabled",
    "remote-energy-unobservable", "simulated-energy-disallowed", "fallback-overflow",
})


def _malformed(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("Malformed execution accounting evidence") from exc
    return checked


def _time(value, name):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{name} must be a timezone-aware timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _finite(value):
    try:
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _text(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024 and bool(value.strip())
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _decode_carbon(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Carbon must be an explicit retained snapshot")
    if value.get("record_type") == "HistoricalCarbonTrace":
        if not isinstance(value.get("samples"), list) or len(value["samples"]) > 10000:
            raise ValueError("Carbon snapshot exceeds the supported sample bound")
        return HistoricalCarbonTrace.from_dict(value)
    if value.get("record_type") == "StaticCarbonEstimate":
        return StaticCarbonEstimate.from_dict(value)
    raise ValueError("Unsupported accounting carbon snapshot")


def _context(value, registry):
    if not isinstance(registry, DeploymentRegistry) or len(registry) > 128:
        raise ValueError("Accounting requires a bounded deployment registry")
    if not isinstance(value, dict) or set(value) != _CONTEXT_FIELDS:
        raise ValueError("Accounting context requires exactly its supported fields")
    fingerprint(value)
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["record_type"] != "ExecutionAccountingContext" or value["scope"] != ACCOUNTING_SCOPE):
        raise ValueError("Unsupported execution accounting context")
    if value["energy_mode"] not in {"none", "codecarbon"}:
        raise ValueError("Unsupported accounting energy mode")
    country = value["energy_country"]
    if country is not None and (not isinstance(country, str) or re.fullmatch(r"[A-Z]{3}", country) is None):
        raise ValueError("Energy country must be an explicit ISO3 country code")
    if value["energy_mode"] == "codecarbon" and country is None:
        raise ValueError("CodeCarbon requires an explicit ISO3 country code")
    tariffs = value["tariff_book"]
    if not isinstance(tariffs, dict) or not isinstance(tariffs.get("tariffs"), list) or len(tariffs["tariffs"]) > 4096:
        raise ValueError("Accounting tariff snapshot exceeds the supported bound")
    book = TariffBook.from_dict(tariffs)
    deployments = {item.config_fingerprint(): item for item in registry}
    for tariff in book.tariffs:
        if tariff.source == "local-no-api-subscription" and (
                tariff.provider != "ollama" or tariff.deployment_fingerprint is None
                or tariff.input_usd_per_million != 0 or tariff.output_usd_per_million != 0):
            raise ValueError("Local no-API-fee tariffs require explicit zero rates and a deployment binding")
        if tariff.deployment_fingerprint is not None:
            deployment = deployments.get(tariff.deployment_fingerprint)
            if (deployment is None or tariff.provider != deployment.provider
                    or tariff.model_id not in deployment.models.values()):
                raise ValueError("Tariff does not match its retained deployment")
    carbon = _decode_carbon(value["carbon"])
    return book, carbon


@_malformed
def make_accounting_context(registry, request, *, tariff_book=None, carbon_provider=None,
                            energy_mode="none", energy_country=None) -> dict:
    """Retain explicit rates and grid data, defaulting only Ollama API fees to zero."""
    request = request if isinstance(request, WorkflowRequest) else WorkflowRequest.from_dict(request)
    if not isinstance(registry, DeploymentRegistry):
        raise ValueError("Accounting requires a deployment registry")
    if tariff_book is None:
        tariffs = []
        for deployment in registry:
            if deployment.provider == "ollama":
                for model_id in sorted(set(deployment.models.values())):
                    tariffs.append(local_api_tariff(model_id, verified_at=request.arrived_at,
                        valid_from=request.arrived_at, valid_until=request.completion_deadline,
                        deployment_fingerprint=deployment.config_fingerprint()))
        tariff_book = TariffBook(tuple(tariffs))
    if not isinstance(tariff_book, TariffBook):
        raise ValueError("Accounting tariffs must be an explicit TariffBook")
    if carbon_provider is not None and not isinstance(carbon_provider, (HistoricalCarbonTrace, StaticCarbonEstimate)):
        raise ValueError("Accounting carbon must be an explicit retained snapshot")
    result = {"schema_version": 1, "record_type": "ExecutionAccountingContext",
              "tariff_book": tariff_book.to_dict(),
              "carbon": None if carbon_provider is None else carbon_provider.to_dict(),
              "energy_mode": energy_mode, "energy_country": energy_country, "scope": ACCOUNTING_SCOPE}
    return validate_accounting_context(result, request, registry)


@_malformed
def validate_accounting_context(value, request, registry) -> dict:
    """Validate a data-only accounting snapshot and return an independent copy."""
    request = request if isinstance(request, WorkflowRequest) else WorkflowRequest.from_dict(request)
    _context(value, registry)
    return deepcopy(value)


def unknown_energy_report(reason=None) -> dict:
    """Return the complete EnergySession unknown shape without arbitrary errors."""
    safe_reason = reason if isinstance(reason, str) and reason in _TELEMETRY_CODES else None if reason is None else "telemetry-unavailable"
    return {"energy_wh": None, "energy_measured": False, "energy_method": "unknown",
            "energy_provenance": "unknown", "energy_components": {
                name: {"value_wh": None, "provenance": "unknown", "source": None}
                for name in ("cpu", "gpu", "ram")}, "energy_scope": "unknown", "telemetry_error": safe_reason}


def _report(value, *, local, energy_mode):
    """Normalize optional tracker failures; this operation is idempotent."""
    if not local:
        return unknown_energy_report("remote-energy-unobservable")
    try:
        return _checked_report(value, energy_mode=energy_mode)
    except Exception:
        # Optional telemetry must not replace a completed provider result.
        return unknown_energy_report("energy-report-invalid")


def _checked_report(value, *, energy_mode):
    if not isinstance(value, dict) or set(value) != _REPORT_FIELDS:
        return unknown_energy_report("energy-report-invalid")
    components = value["energy_components"]
    if not isinstance(components, dict) or set(components) != _COMPONENTS:
        return unknown_energy_report("energy-report-invalid")
    if value["energy_provenance"] == "simulated" or any(
            isinstance(item, dict) and item.get("provenance") == "simulated" for item in components.values()):
        return unknown_energy_report("simulated-energy-disallowed")
    for name, item in components.items():
        if not isinstance(item, dict) or set(item) != {"value_wh", "provenance", "source"}:
            return unknown_energy_report("energy-report-invalid")
        if item["value_wh"] is None:
            if item["provenance"] != "unknown" or item["source"] is not None:
                return unknown_energy_report("energy-report-invalid")
        elif (not _finite(item["value_wh"]) or item["provenance"] not in {"measured", "estimated"}
              or not _text(item["source"])):
            return unknown_energy_report("energy-report-invalid")
        elif item["provenance"] == "measured" and item["source"].startswith("codecarbon:"):
            source = item["source"]
            if "power-model" in source or "unverified" in source:
                return unknown_energy_report("energy-report-invalid")
            if name == "cpu":
                prefix = "codecarbon:cpu:"
                modes = set(source.removeprefix(prefix).split(","))
                if not source.startswith(prefix) or not modes <= {"intel_rapl", "intel_power_gadget", "windows_emi"}:
                    return unknown_energy_report("energy-report-invalid")
    if value["energy_wh"] is None:
        expected = unknown_energy_report(value["telemetry_error"])
        if value != expected:
            return unknown_energy_report("energy-report-invalid")
        return expected
    if (not _finite(value["energy_wh"]) or type(value["energy_measured"]) is not bool
            or not _text(value["energy_method"]) or value["energy_provenance"] not in {"measured", "estimated", "mixed"}):
        return unknown_energy_report("energy-report-invalid")
    provenance = value["energy_provenance"]
    measured = [item["provenance"] == "measured" for item in components.values()]
    if (value["energy_measured"] != (provenance == "measured")
            or (provenance == "measured" and not all(measured))
            or (provenance == "estimated" and any(measured))
            or (provenance == "mixed" and (not any(measured) or all(measured)))):
        return unknown_energy_report("energy-report-invalid")
    known_sum = sum(item["value_wh"] or 0 for item in components.values())
    tolerance = max(1e-12, value["energy_wh"] * 1e-6)
    if (not math.isfinite(known_sum) or known_sum > value["energy_wh"] + tolerance
            or provenance == "measured" and abs(known_sum - value["energy_wh"]) > tolerance):
        return unknown_energy_report("energy-report-invalid")
    if value["telemetry_error"] is not None:
        return unknown_energy_report(value["telemetry_error"])
    if value["energy_scope"] != _HOST_SCOPE:
        return unknown_energy_report("energy-scope-unsupported")
    if energy_mode != "codecarbon":
        return unknown_energy_report("energy-tracking-disabled")
    return deepcopy(value)


def _unknown(unit, reason):
    return MetricEstimate(None, unit, "unknown", reason)


def _attempt(value, registry):
    attempt = ExecutionAttempt.from_dict(value.to_dict() if isinstance(value, ExecutionAttempt) else value)
    if attempt.status not in {"completed", "failed", "cancelled", "needs-reconciliation"}:
        raise ValueError("Accounting requires a terminal raw attempt")
    deployment = registry.require_model(attempt.planned_candidate.deployment_id, attempt.planned_candidate.model_id)
    nondispatched = attempt.status in {"failed", "cancelled"} and all(getattr(attempt, key) is None for key in (
        "actual_start", "actual_finish", "executed_model_id", "executed_deployment_id",
        "provider_reported_model_id", "wait_seconds", "provider_seconds", "input_tokens", "output_tokens",
        "observations", "output", "completion_reason"))
    if not nondispatched and (attempt.actual_start is None or attempt.actual_finish is None
            or attempt.executed_deployment_id != deployment.deployment_id
            or attempt.executed_model_id != deployment.provider_model(attempt.planned_candidate.model_id)):
        raise ValueError("Raw attempt execution identity or timing differs from the retained deployment")
    return attempt, deployment, nondispatched


@_malformed
def enrich_attempt(attempt, registry, context, *, energy_report, measurement_start,
                   measurement_finish, journal_recorded_at, accounted_at) -> dict:
    """Account once from actual evidence, without mutating or redispatching it."""
    book, carbon = _context(context, registry)
    attempt, deployment, nondispatched = _attempt(attempt, registry)
    start = _time(measurement_start, "measurement_start")
    finish = _time(measurement_finish, "measurement_finish")
    journal = _time(journal_recorded_at, "journal_recorded_at")
    accounted = _time(accounted_at, "accounted_at")
    if not start <= journal <= finish <= accounted:
        raise ValueError("Accounting measurement/journal timestamps are out of order")
    if not nondispatched and not start <= attempt.actual_start <= attempt.actual_finish <= journal:
        raise ValueError("Host measurement must enclose provider execution and journal persistence")
    report = _report(energy_report, local=is_loopback_endpoint(deployment.endpoint), energy_mode=context["energy_mode"])
    errors = []
    if report["telemetry_error"] is not None:
        errors.append(report["telemetry_error"])
    latency = _unknown("s", "actual-latency-unavailable")
    if not nondispatched:
        observed = None if attempt.observations is None else attempt.observations.latency
        if observed is not None and observed.value is not None and observed.provenance == "measured":
            latency = observed
        elif attempt.provider_seconds is not None:
            latency = MetricEstimate(attempt.provider_seconds, "s", "measured", "provider-adapter-elapsed-time")
        else:
            latency = MetricEstimate((attempt.actual_finish - attempt.actual_start).total_seconds(),
                                     "s", "measured", "actual-provider-wall-clock-interval")
    if nondispatched:
        cost = MetricEstimate(0.0, "USD", "measured", "confirmed-no-provider-dispatch; api-only; electricity-excluded")
    else:
        cost = book.estimate_usage(deployment.provider, attempt.executed_model_id, attempt.input_tokens,
            attempt.output_tokens, instant=attempt.actual_start, as_of=accounted,
            deployment_fingerprint=deployment.config_fingerprint())
        if cost.value is None:
            errors.append("cost-unavailable")
    energy = _unknown("kWh", "host-energy-unavailable")
    if report["energy_wh"] is not None:
        energy = MetricEstimate(report["energy_wh"] / 1000, "kWh", report["energy_provenance"],
                                f"{report['energy_method']}; {_HOST_SCOPE}; {ACCOUNTING_SCOPE}")
    sample = None
    carbon_unit, carbon_reason = "gCO2e", "carbon-not-configured"
    if carbon is not None:
        available = carbon.availability(deployment.grid, start, as_of=accounted)
        sample = available.sample
        carbon_reason = "carbon-" + available.status
        if sample is not None:
            carbon_unit = sample.unit.removesuffix("/kWh")
            # The measured duration is half-open, like retained grid coverage.
            if finish > sample.valid_until:
                sample, carbon_reason = None, "carbon-interval-not-covered"
            elif sample.provenance == "simulated":
                sample, carbon_reason = None, "simulated-carbon-disallowed"
        if sample is None:
            errors.append(carbon_reason)
    carbon_metric = _unknown(carbon_unit, carbon_reason if sample is None else "host-energy-unavailable")
    if sample is not None and energy.value is not None:
        value = energy.value * sample.value
        if math.isfinite(value):
            provenance = ("measured" if energy.provenance == sample.provenance == "measured" else
                          "estimated" if energy.provenance == sample.provenance == "estimated" else "mixed")
            carbon_metric = MetricEstimate(value, carbon_unit, provenance,
                f"host-energy-times-grid-intensity; {sample.source}; {ACCOUNTING_SCOPE}")
        else:
            carbon_metric = _unknown(carbon_unit, "carbon-overflow")
            errors.append("carbon-overflow")
    metrics = StepEstimates(latency, _unknown("ratio", "live-quality-not-reviewed"), cost, energy, carbon_metric)
    result = {"schema_version": 1, "record_type": "AttemptAccounting", "workflow_id": attempt.workflow_id,
        "attempt_id": attempt.attempt_id, "attempt_sha256": fingerprint(attempt.to_dict()),
        "context_fingerprint": fingerprint(context), "accounted_at": _iso(accounted),
        "measurement_start": _iso(start), "measurement_finish": _iso(finish), "journal_recorded_at": _iso(journal),
        "energy_report": report, "metrics": metrics.to_dict(), "carbon_sample": None if sample is None else sample.to_dict(),
        "errors": sorted(set(errors))}
    return {**result, "fingerprint": fingerprint(result)}


@_malformed
def validate_accounting_record(record, attempt, registry, context) -> dict:
    """Recompute all retained semantics, so rehashing fabricated metrics fails."""
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise ValueError("Accounting record requires exactly its supported fields")
    fingerprint(record)
    if (type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["record_type"] != "AttemptAccounting"):
        raise ValueError("Unsupported accounting record schema")
    StepEstimates.from_dict(record["metrics"])
    if record["carbon_sample"] is not None:
        CarbonSample.from_dict(record["carbon_sample"])
    if any(not isinstance(record[name], str) for name in (
            "accounted_at", "measurement_start", "measurement_finish", "journal_recorded_at")):
        raise ValueError("Accounting record timestamps must be ISO strings")
    if record["fingerprint"] != fingerprint({key: value for key, value in record.items() if key != "fingerprint"}):
        raise ValueError("Accounting record fingerprint mismatch")
    expected = enrich_attempt(attempt, registry, context, energy_report=record["energy_report"],
        measurement_start=record["measurement_start"], measurement_finish=record["measurement_finish"],
        journal_recorded_at=record["journal_recorded_at"], accounted_at=record["accounted_at"])
    if record != expected:
        raise ValueError("Accounting record differs from retained raw evidence and accounting inputs")
    return deepcopy(record)
