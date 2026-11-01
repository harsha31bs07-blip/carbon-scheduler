"""Turn empirical profiles and explicit start windows into planner candidates.

This is a pure preparation layer: registry membership establishes configured
support, not endpoint health. It makes no inference calls, invents no quality
and never infers a grid from an endpoint. Carbon is recomputed for each start
from known energy and a sample covering the complete predicted interval.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from data.profiles import estimate_step
from shared.deployments import DeploymentRegistry
from shared.scheduling import (
    CarbonProvider, CarbonSample, MetricEstimate, PlannedCandidate, StepRequest, SystemClock, WorkloadSizeForecast,
)


def _utc(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _unknown_carbon(status):
    return MetricEstimate(None, "gCO2e", "unknown", "start-window-carbon-unavailable:" + status)


@dataclass(frozen=True)
class CandidateDiagnostic:
    deployment_id: str
    model_id: str
    planned_start: datetime
    candidate_id: str
    excluded: bool
    reasons: tuple[str, ...]
    profile_source: str | None
    availability_source: str
    carbon_status: str
    carbon_sample: CarbonSample | None

    def to_dict(self):
        return {"deployment_id": self.deployment_id, "model_id": self.model_id,
                "planned_start": self.planned_start.isoformat(), "candidate_id": self.candidate_id,
                "excluded": self.excluded, "reasons": list(self.reasons), "profile_source": self.profile_source,
                "availability_source": self.availability_source, "carbon_status": self.carbon_status,
                "carbon_sample": self.carbon_sample.to_dict() if self.carbon_sample else None}


@dataclass(frozen=True)
class ProfileCandidateResult:
    step_id: str
    as_of: datetime
    now: datetime
    input_size_estimate: int
    candidates: tuple[PlannedCandidate, ...]
    diagnostics: tuple[CandidateDiagnostic, ...]
    notes: tuple[str, ...]
    workload_forecast: WorkloadSizeForecast | None = None

    def to_dict(self):
        return {"schema_version": 3, "record_type": "ProfileCandidateResult", "step_id": self.step_id,
                "as_of": self.as_of.isoformat(), "now": self.now.isoformat(),
                "input_size_estimate": self.input_size_estimate,
                "input_size_source": "provisional-calibration-interval-upper-endpoint" if self.workload_forecast
                                     else "materialized-prompt-utf8-bytes",
                "workload_forecast": self.workload_forecast.to_dict() if self.workload_forecast else None,
                "candidates": [candidate.to_dict() for candidate in self.candidates],
                "diagnostics": [item.to_dict() for item in self.diagnostics], "notes": list(self.notes)}


def _carbon(energy, latency, deployment, start, *, as_of, provider):
    if energy.value is None:
        return _unknown_carbon("unknown-energy"), "unknown-energy", None
    if latency.value is None:
        return _unknown_carbon("unknown-duration"), "unknown-duration", None
    if deployment.grid is None:
        return _unknown_carbon("unknown-grid"), "unknown-grid", None
    if provider is None:
        return _unknown_carbon("no-provider"), "no-provider", None
    sample = provider.sample(deployment.grid, start, as_of=as_of)
    if sample is None:
        return _unknown_carbon("missing-stale-or-unissued-sample"), "missing-stale-or-unissued-sample", None
    if not isinstance(sample, CarbonSample):
        raise ValueError("Carbon provider must return CarbonSample or None")
    if sample.region != deployment.grid or not sample.is_valid_at(start, as_of=as_of):
        return _unknown_carbon("invalid-sample-region-or-issue-time"), "invalid-sample-region-or-issue-time", sample
    try:
        finish = start + timedelta(seconds=latency.value)
    except OverflowError:
        return _unknown_carbon("duration-overflow"), "duration-overflow", sample
    if finish > sample.valid_until:
        return _unknown_carbon("insufficient-duration-coverage"), "insufficient-duration-coverage", sample
    value = energy.value * sample.value
    uncertainty = energy.uncertainty * sample.value if energy.uncertainty is not None else None
    if not math.isfinite(value) or uncertainty is not None and not math.isfinite(uncertainty):
        return _unknown_carbon("derived-carbon-overflow"), "derived-carbon-overflow", sample
    provenance = ("simulated" if "simulated" in {energy.provenance, sample.provenance}
                  else "mixed" if "mixed" in {energy.provenance, sample.provenance} else "estimated")
    unit = "gCO2" if sample.unit == "gCO2/kWh" else "gCO2e"
    source = f"start-window-carbon:{_fingerprint(sample.to_dict())}:{energy.source}"
    return MetricEstimate(value, unit, provenance, source, uncertainty), "available", sample


def _generate_candidates(
    step: StepRequest,
    workload_version: str,
    registry: DeploymentRegistry,
    profiles: dict,
    starts: Sequence[datetime],
    *,
    as_of: datetime,
    carbon_provider: CarbonProvider | None = None,
    now: datetime | None = None,
    forecast: WorkloadSizeForecast | None = None,
) -> ProfileCandidateResult:
    """Generate exact-settings candidates on a supplied finite UTC start grid.

    ``step.prompt`` must already contain its actual dependency outputs. Input
    size is recomputed from UTF-8 bytes, the profile builder's documented proxy;
    provider token counts are never substituted for that unit. Unsupported
    size/settings/task/deployment combinations remain unknown rather than
    extrapolating. The joint planner rejects their unknown required metrics.

    ``as_of`` is the information cutoff and cannot exceed planning ``now``.
    Feed adapters enforce freshness; this layer independently checks issue time,
    matching grid and full interval coverage. A single sample is used only for
    its entire validity window; no cross-window intensity is extrapolated.
    Exact dependency-ready starts must be supplied by the DAG/grid caller.
    """
    if not isinstance(step, StepRequest) or not isinstance(registry, DeploymentRegistry):
        raise ValueError("Candidate generation requires a StepRequest and deployment registry")
    if not isinstance(workload_version, str) or not workload_version.strip():
        raise ValueError("workload_version must be nonempty text")
    as_of = _utc(as_of, "as_of")
    now = _utc(SystemClock().now() if now is None else now, "now")
    if as_of > now:
        raise ValueError("as_of cannot exceed planning now")
    if not isinstance(starts, (tuple, list)) or not starts:
        raise ValueError("starts must be a nonempty finite list or tuple")
    grid = tuple(sorted(_utc(start, "start") for start in starts))
    if len(set(grid)) != len(grid):
        raise ValueError("start grid must not contain duplicates")
    if carbon_provider is not None and not callable(getattr(carbon_provider, "sample", None)):
        raise ValueError("carbon_provider must implement sample")
    size = len(step.prompt.encode("utf-8")) if forecast is None else forecast.maximum_input_bytes
    candidates, diagnostics = [], []
    for deployment in sorted(registry, key=lambda item: item.deployment_id):
        for model in sorted(deployment.models):
            estimates = estimate_step(profiles, deployment_fingerprint=deployment.config_fingerprint(),
                model_id=model, step_id=step.step_id, task_type=step.task_type, workload_version=workload_version,
                input_size_estimate=size, output_limit_tokens=step.output_limit_tokens,
                enable_thinking=step.enable_thinking)
            if forecast is not None:
                lower = estimate_step(profiles, deployment_fingerprint=deployment.config_fingerprint(),
                    model_id=model, step_id=step.step_id, task_type=step.task_type, workload_version=workload_version,
                    input_size_estimate=forecast.minimum_input_bytes, output_limit_tokens=step.output_limit_tokens,
                    enable_thinking=step.enable_thinking)
                if lower.latency.value is None:
                    estimates = lower
                tag = ":provisional-size-forecast:" + _fingerprint(forecast.to_dict())
                estimates = replace(estimates, **{name: replace(getattr(estimates, name),
                    source=(getattr(estimates, name).source or "unknown") + tag,
                    provenance="simulated" if forecast.provenance == "simulated"
                        and getattr(estimates, name).value is not None else getattr(estimates, name).provenance)
                    for name in ("latency", "accuracy", "cost", "energy", "carbon")})
            for start in grid:
                reasons = []
                excluded = False
                if start < max(now, step.earliest_start) or start >= step.latest_finish:
                    reasons.append("start-outside-current-step-window")
                    excluded = True
                if forecast is not None and start >= forecast.valid_until:
                    reasons.append("start-after-workload-forecast-validity")
                    excluded = True
                if estimates.latency.value is not None:
                    try:
                        if start + timedelta(seconds=estimates.latency.value) > step.latest_finish:
                            reasons.append("predicted-finish-after-step-deadline")
                            excluded = True
                        if forecast is not None and start + timedelta(seconds=estimates.latency.value) > forecast.valid_until:
                            reasons.append("predicted-finish-after-workload-forecast-validity")
                            excluded = True
                    except OverflowError:
                        reasons.append("duration-overflow")
                        excluded = True
                if excluded:
                    carbon, carbon_status, sample = _unknown_carbon("excluded-start"), "excluded-start", None
                else:
                    carbon, carbon_status, sample = _carbon(estimates.energy, estimates.latency, deployment,
                        start, as_of=as_of, provider=carbon_provider)
                at_start = replace(estimates, carbon=carbon)
                for name in ("latency", "accuracy", "cost", "energy", "carbon"):
                    if getattr(at_start, name).value is None:
                        reasons.append("unknown-" + name)
                identity = {"step": step.to_dict(), "workload_version": workload_version,
                            "deployment_fingerprint": deployment.config_fingerprint(), "model_id": model,
                            "start": start.isoformat(), "estimates": at_start.to_dict()}
                if forecast is not None:
                    identity["workload_forecast"] = forecast.to_dict()
                candidate = PlannedCandidate("profile-" + _fingerprint(identity)[:24], step.step_id,
                    model, deployment.deployment_id, start, at_start, deployment.region)
                if not excluded:
                    candidates.append(candidate)
                diagnostics.append(CandidateDiagnostic(deployment.deployment_id, model, start,
                    candidate.candidate_id, excluded, tuple(reasons), estimates.latency.source,
                    "registry-configuration-only", carbon_status, sample))
    return ProfileCandidateResult(step.step_id, as_of, now, size, tuple(candidates), tuple(diagnostics), (
        "Registry support is configuration; endpoint health/capacity must be checked by the planner and worker.",
        "Quality remains a reviewed sample estimate or unknown; format/proxy scores are not semantic accuracy.",
        "Input size uses UTF-8 bytes; no extrapolation or historical carbon-mass reuse is performed.",
        "The DAG caller must add exact dependency-ready starts to its finite grid.",
        "Configured location provenance is retained; this generation does not verify geographic routing.",
    ) + (("Input is a provisional calibration interval; actual dependency output is not available. "
          "Its upper endpoint drives latency, not a guaranteed worst-case bound.",) if forecast is not None else ()), forecast)


def generate_profile_candidates(step, workload_version, registry, profiles, starts, *, as_of,
                                carbon_provider=None, now=None):
    """Generate candidates only from an actually materialized prompt."""
    return _generate_candidates(step, workload_version, registry, profiles, starts, as_of=as_of,
                                carbon_provider=carbon_provider, now=now)


def generate_forecast_candidates(step, workload_version, registry, profiles, starts, *, forecast,
                                 workflow_fingerprint, as_of, carbon_provider=None, now=None):
    """Generate explicitly provisional options without constructing fake output."""
    if not isinstance(step, StepRequest) or not isinstance(profiles, dict):
        raise ValueError("Provisional candidates require a step and profiles")
    if not isinstance(forecast, WorkloadSizeForecast):
        raise ValueError("Provisional candidates require a WorkloadSizeForecast")
    planning_now = _utc(SystemClock().now() if now is None else now, "now")
    cutoff = _utc(as_of, "as_of")
    if (forecast.step_id != step.step_id or forecast.workload_version != workload_version
            or forecast.workflow_fingerprint != workflow_fingerprint
            or forecast.profile_fingerprint != profiles.get("fingerprint")
            or forecast.issued_at > cutoff or forecast.valid_until <= planning_now):
        raise ValueError("Workload forecast is stale or differs from its workflow/profile context")
    return _generate_candidates(step, workload_version, registry, profiles, starts, as_of=cutoff,
                                carbon_provider=carbon_provider, now=planning_now, forecast=forecast)
