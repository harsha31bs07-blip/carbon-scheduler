"""Execute one due schema-3 candidate on its explicit configured deployment.

This is the adapter boundary for the future durable worker. It does not wait for
future jobs, resolve dependencies, or retry ambiguous external side effects.
The existing schema-2 CLI remains separate until the connected workflow is ready.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from shared.deployments import DeploymentRegistry
from shared.scheduling import (
    Clock, ExecutionAttempt, MetricEstimate, PlannedCandidate, StepEstimates,
    SystemClock,
)
from workflow.deployment_clients import DeploymentCallError, _validate_timeout, call_deployment


def dispatch_planned_candidate(
    candidate: PlannedCandidate,
    registry: DeploymentRegistry,
    prompt: str,
    *,
    workflow_id: str,
    attempt_id: str | None = None,
    clock: Clock | None = None,
    timeout_seconds: float = 20,
    output_limit_tokens: int = 1024,
    enable_thinking: bool | None = None,
    latest_finish: datetime | None = None,
) -> ExecutionAttempt:
    """Dispatch once and retain successful or failed execution facts.

    Future starts fail before inference. The worker must supply a due candidate
    after checking dependencies, deadlines and capacity. ``provider_seconds`` is
    the adapter's observed request wall time, including network/model loading;
    unknown inference-only time, waiting time and resource use stay unknown.
    An optional deadline is checked again immediately before the provider call
    and bounds its inactivity timeout. This cannot cancel an in-flight call.
    """
    if not isinstance(candidate, PlannedCandidate) or not isinstance(registry, DeploymentRegistry):
        raise ValueError("Dispatch requires a planned candidate and deployment registry")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Dispatch requires a nonempty prompt")
    timeout = _validate_timeout(timeout_seconds)
    if latest_finish is not None:
        if (not isinstance(latest_finish, datetime) or latest_finish.tzinfo is None
                or latest_finish.utcoffset() is None):
            raise ValueError("latest_finish must be a timezone-aware datetime")
        latest_finish = latest_finish.astimezone(timezone.utc)
    deployment = registry.require_model(candidate.deployment_id, candidate.model_id)
    if candidate.region is not None and candidate.region != deployment.region:
        raise ValueError("Candidate geography differs from the deployment configuration")
    active_clock = clock or SystemClock()
    started = active_clock.now()
    try:
        planned_finish = (
            candidate.planned_start + timedelta(seconds=candidate.estimates.latency.value)
            if candidate.estimates.latency.value is not None else None
        )
    except OverflowError:
        raise ValueError("Planned finish exceeds the supported timestamp range") from None
    # Construct the initial record before external work to validate IDs/time.
    scheduled = ExecutionAttempt(
        attempt_id=attempt_id if attempt_id is not None else uuid4().hex,
        workflow_id=workflow_id, planned_candidate=candidate, status="scheduled",
        planned_finish=planned_finish,
        output_limit_tokens=output_limit_tokens, enable_thinking=enable_thinking,
    )
    # Normalize/validate the injected clock using the same contract as records.
    starting = ExecutionAttempt(
        attempt_id=scheduled.attempt_id, workflow_id=workflow_id,
        planned_candidate=candidate, status="running", actual_start=started,
        executed_model_id=deployment.provider_model(candidate.model_id),
        executed_deployment_id=deployment.deployment_id,
        output_limit_tokens=output_limit_tokens, enable_thinking=enable_thinking,
    )
    started = starting.actual_start
    if started < candidate.planned_start:
        raise ValueError("Candidate is not due; delayed execution requires the worker")
    if latest_finish is not None:
        # Preparation can consume the budget too. Validate and normalize the
        # second clock value before it becomes a claimed provider start.
        starting = replace(starting, actual_start=active_clock.now())
        started = starting.actual_start
        remaining = (latest_finish - started).total_seconds()
        if remaining <= 0:
            return replace(scheduled, status="failed", error="step-deadline-exceeded")
        if started < candidate.planned_start:
            raise ValueError("Candidate is not due; delayed execution requires the worker")
        timeout = min(timeout, remaining)
    try:
        result = call_deployment(deployment, candidate.model_id, prompt,
                                 timeout_seconds=timeout, max_attempts=1,
                                 output_limit_tokens=output_limit_tokens,
                                 enable_thinking=enable_thinking)
    except DeploymentCallError as exc:
        # Persist a safe error code only. Raw provider text may contain secrets.
        attempted = exc.request_attempted
        return ExecutionAttempt(
            attempt_id=scheduled.attempt_id, workflow_id=workflow_id,
            planned_candidate=candidate, status="failed",
            planned_finish=scheduled.planned_finish,
            actual_start=started if attempted else None,
            actual_finish=active_clock.now() if attempted else None,
            executed_model_id=deployment.provider_model(candidate.model_id) if attempted else None,
            executed_deployment_id=deployment.deployment_id if attempted else None,
            error=exc.error_code,
            output_limit_tokens=output_limit_tokens, enable_thinking=enable_thinking,
        )
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    observations = StepEstimates(
        latency=MetricEstimate(result["latency_ms"] / 1000, "s", "measured",
                               "deployment-adapter-wall-clock"),
        accuracy=unknown("ratio"), cost=unknown("USD"),
        energy=unknown("kWh"), carbon=unknown("gCO2e"),
    )
    return ExecutionAttempt(
        attempt_id=scheduled.attempt_id, workflow_id=workflow_id,
        planned_candidate=candidate, status="completed",
        planned_finish=scheduled.planned_finish,
        actual_start=started, actual_finish=active_clock.now(),
        executed_model_id=result["provider_model_id"],
        executed_deployment_id=result["deployment_id"],
        provider_reported_model_id=result["reported_model_id"],
        provider_seconds=result["latency_ms"] / 1000,
        input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
        observations=observations, output=result["text"],
        output_limit_tokens=output_limit_tokens, enable_thinking=enable_thinking,
        completion_reason=result.get("completion_reason"),
    )
