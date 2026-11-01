"""Behavioral checks for the new scheduling boundary, independent of schema 2."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from shared.schema import Candidate as LegacyCandidate
from shared.scheduling import (
    SCHEMA_VERSION,
    CarbonSample,
    ComparisonContext,
    ExecutionAttempt,
    MetricEstimate,
    PlannedCandidate,
    RejectedCandidate,
    SchedulingDecision,
    StepEstimates,
    StepRequest,
    SystemClock,
    WorkflowRequest,
    legacy_candidate_to_planned,
)


NOW = datetime(2026, 10, 19, 8, 30, tzinfo=timezone.utc)


def metrics() -> StepEstimates:
    return StepEstimates(
        latency=MetricEstimate(1.5, "s", "estimated", "profile-v1", .2),
        accuracy=MetricEstimate(.8, "ratio", "estimated", "evaluation-v1"),
        cost=MetricEstimate(0, "USD", "measured", "local-accounting"),
        energy=MetricEstimate(None, "kWh", "unknown"),
        carbon=MetricEstimate(None, "gCO2e", "unknown"),
    )


def context() -> ComparisonContext:
    return ComparisonContext(
        workload_id="sha256-input-1", workload_version="tickets-v1",
        configuration_id="configuration-1", deployment_snapshot_id="deployments-1",
        calibration_id="profiles-1", policy_version="scorer-v1",
        reference_time=NOW, experiment_mode="simulation",
    )


def candidate(**changes) -> PlannedCandidate:
    return replace(PlannedCandidate("candidate-1", "classify", "llama", "edge-1", NOW, metrics()), **changes)


def step(**changes) -> StepRequest:
    return replace(StepRequest(
        "classify", "classification", "Please classify this ticket.", None, 100,
        NOW, NOW + timedelta(seconds=10),
    ), **changes)


def workflow(**changes) -> WorkflowRequest:
    return replace(WorkflowRequest(
        "workflow-1", "My account is locked.", NOW, "balanced",
        NOW + timedelta(seconds=15), NOW + timedelta(minutes=5),
        "tickets-v1", "constraints-v1", (step(),),
    ), **changes)


def decision(**changes) -> SchedulingDecision:
    return replace(SchedulingDecision(
        "decision-1", "workflow-1", "classify", "balanced",
        {"latency": .5, "accuracy": .5, "cost": 0, "energy": 0, "carbon": 0},
        NOW, context(), candidate(), (), (), 1,
        {"latency": .1, "accuracy": .8}, {"latency": .05, "accuracy": -.4},
    ), **changes)


def completed(**changes) -> ExecutionAttempt:
    return replace(ExecutionAttempt(
        "attempt-1", "workflow-1", candidate(), "completed",
        actual_start=NOW + timedelta(seconds=2), actual_finish=NOW + timedelta(seconds=4),
        executed_model_id="llama3.2:3b", executed_deployment_id="edge-1",
        provider_seconds=2, output="account-access", observations=metrics(),
    ), **changes)


@pytest.mark.parametrize("record", [
    metrics().latency, metrics(), step(), workflow(), context(), candidate(),
    RejectedCandidate("bad-1", ("quality-below-minimum",)), decision(), completed(),
    CarbonSample("IN-KA", NOW, NOW, NOW + timedelta(hours=1), 400, "fixture", "simulated"),
])
def test_records_roundtrip_json_with_explicit_schema(record):
    payload = json.loads(json.dumps(record.to_dict(), allow_nan=False))
    assert payload["schema_version"] == SCHEMA_VERSION == 3
    assert payload["record_type"] == type(record).__name__
    assert type(record).from_dict(payload) == record


def test_timezone_offsets_normalize_to_utc_without_changing_fingerprint():
    india = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    shifted = replace(context(), reference_time=india)
    assert shifted.reference_time == NOW
    assert shifted.reference_time.tzinfo is timezone.utc
    assert shifted.fingerprint() == context().fingerprint()
    assert shifted.to_dict()["reference_time"] == "2026-10-19T08:30:00Z"


@pytest.mark.parametrize("factory", [
    lambda bad: step(earliest_start=bad),
    lambda bad: workflow(arrived_at=bad),
    lambda bad: replace(context(), reference_time=bad),
    lambda bad: candidate(planned_start=bad),
    lambda bad: completed(actual_finish=bad),
    lambda bad: replace(decision(), decided_at=bad),
])
@pytest.mark.parametrize("bad", [datetime(2026, 10, 19), "2026-10-19T00:00:00Z"])
def test_timestamps_require_aware_datetime_instances(factory, bad):
    with pytest.raises(ValueError):
        factory(bad)


@pytest.mark.parametrize("field", ["earliest_start", "latest_finish"])
def test_required_timestamps_cannot_be_null(field):
    with pytest.raises(ValueError):
        step(**{field: None})


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"schema_version": True}, {"schema_version": "3"},
    {"record_type": "ExecutionAttempt"}, {"api_key": "must-not-be-accepted"},
])
def test_reader_rejects_other_schemas_types_and_unknown_fields(change):
    payload = candidate().to_dict() | change
    with pytest.raises(ValueError):
        PlannedCandidate.from_dict(payload)


@pytest.mark.parametrize("field", ["schema_version", "record_type", "deployment_id"])
def test_reader_requires_envelope_and_required_fields(field):
    payload = candidate().to_dict()
    del payload[field]
    with pytest.raises(ValueError):
        PlannedCandidate.from_dict(payload)


def test_reader_validates_nested_fields_and_timestamps():
    payload = candidate().to_dict()
    payload["estimates"]["accuracy"]["value"] = 2
    with pytest.raises(ValueError):
        PlannedCandidate.from_dict(payload)
    payload = candidate().to_dict()
    payload["planned_start"] = "2026-10-19T08:30:00"
    with pytest.raises(ValueError):
        PlannedCandidate.from_dict(payload)


def test_unknown_observation_is_distinct_from_observed_zero():
    payload = metrics().to_dict()
    assert payload["energy"]["value"] is None
    assert payload["energy"]["provenance"] == "unknown"
    assert payload["cost"]["value"] == 0
    assert payload["cost"]["provenance"] == "measured"
    result = StepEstimates.from_dict(payload)
    assert result.energy.source is None
    assert result.cost.source == "local-accounting"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1, True, "1", 10 ** 1000])
def test_metric_values_reject_nonfinite_negative_and_nonnumeric_inputs(bad):
    with pytest.raises(ValueError):
        MetricEstimate(bad, "s", "estimated", "fixture")


@pytest.mark.parametrize("args", [
    (None, "s", "measured", "fixture"),
    (None, "s", "unknown", None, 0),
    (1, "s", "unknown", None),
    (1, "s", "estimated", None),
    (1, "s", [], "fixture"),
    (1, "s", "estimated", "fixture", -.1),
])
def test_metric_requires_consistent_provenance_source_and_uncertainty(args):
    with pytest.raises(ValueError):
        MetricEstimate(*args)


def test_step_metrics_require_canonical_units_and_bounded_quality():
    with pytest.raises(ValueError):
        replace(metrics(), latency=MetricEstimate(1500, "ms", "estimated", "fixture"))
    with pytest.raises(ValueError):
        replace(metrics(), accuracy=MetricEstimate(1.01, "ratio", "estimated", "fixture"))


@pytest.mark.parametrize("changes", [
    {"latest_finish": NOW}, {"input_tokens": True}, {"input_tokens": -1},
    {"input_tokens": 12},
    {"output_limit_tokens": 0}, {"output_limit_tokens": 2.5},
    {"output_limit_tokens": 32769}, {"output_limit_tokens": True},
    {"enable_thinking": 0}, {"enable_thinking": "false"},
    {"allow_deferral": 1}, {"required_for_response": "yes"},
    {"dependencies": ["retrieve"]}, {"dependencies": ("classify",)},
    {"dependencies": ("retrieve", "retrieve")},
])
def test_step_requests_validate_windows_tokens_flags_and_dependencies(changes):
    with pytest.raises(ValueError):
        step(**changes)


def test_tokenizer_can_report_known_zero_without_losing_provenance():
    request = step(input_tokens=0, input_size_source="tokenizer-v1")
    assert StepRequest.from_dict(request.to_dict()).input_tokens == 0


@pytest.mark.parametrize("thinking", [None, False, True])
def test_step_and_attempt_roundtrip_requested_generation_controls(thinking):
    request = step(output_limit_tokens=32768, enable_thinking=thinking)
    assert StepRequest.from_dict(request.to_dict()) == request
    attempt = completed(output_limit_tokens=37, enable_thinking=thinking)
    assert ExecutionAttempt.from_dict(attempt.to_dict()) == attempt
    assert attempt.to_dict()["enable_thinking"] is thinking


def test_generation_controls_can_be_retained_before_dispatch():
    attempt = ExecutionAttempt("attempt-1", "workflow-1", candidate(), "scheduled",
                               output_limit_tokens=57, enable_thinking=False)
    assert attempt.actual_start is None
    assert ExecutionAttempt.from_dict(attempt.to_dict()) == attempt


def test_older_schema3_requests_and_attempts_preserve_unknown_generation_settings():
    request = step().to_dict()
    del request["enable_thinking"]
    assert StepRequest.from_dict(request).enable_thinking is None
    attempt = completed().to_dict()
    del attempt["output_limit_tokens"]
    del attempt["enable_thinking"]
    del attempt["completion_reason"]
    restored = ExecutionAttempt.from_dict(attempt)
    assert restored.output_limit_tokens is None
    assert restored.enable_thinking is None
    assert restored.completion_reason is None


@pytest.mark.parametrize("reason", ["stop", "length", "content-filter", "tool-calls", "other"])
def test_attempt_can_retain_partial_output_and_normalized_completion_reason(reason):
    attempt = completed(output="Partial or filtered output", completion_reason=reason)
    assert ExecutionAttempt.from_dict(attempt.to_dict()) == attempt


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "provider secret text", "", True, 1, {}])
def test_attempt_rejects_unvalidated_provider_completion_metadata(reason):
    with pytest.raises(ValueError, match="completion reason"):
        completed(completion_reason=reason)


def test_undispatched_attempt_cannot_claim_completion_metadata():
    with pytest.raises(ValueError, match="actual_finish"):
        ExecutionAttempt("attempt-1", "workflow-1", candidate(), "failed",
                         error="preflight-failed", completion_reason="stop")


def test_unknown_input_size_can_retain_explanation_of_missing_count():
    request = step(input_size_source="tokenizer-unavailable")
    assert request.input_tokens is None
    assert StepRequest.from_dict(request.to_dict()).input_size_source == "tokenizer-unavailable"


def test_workflow_accepts_branching_response_and_deferrable_background_steps():
    draft = step(step_id="draft", dependencies=("classify",), task_type="generation")
    background = step(
        step_id="archive", dependencies=("draft",), required_for_response=False,
        allow_deferral=True, latest_finish=NOW + timedelta(minutes=4),
    )
    request = workflow(steps=(step(), draft, background))
    assert request.steps[2].latest_finish > request.response_deadline
    assert WorkflowRequest.from_dict(request.to_dict()) == request


@pytest.mark.parametrize("changes", [
    {"response_deadline": NOW},
    {"completion_deadline": NOW + timedelta(seconds=14)},
    {"steps": ()}, {"steps": (step(), step())},
    {"steps": (step(dependencies=("absent",)),)},
    {"steps": (step(earliest_start=NOW - timedelta(seconds=1)),)},
    {"steps": (step(latest_finish=NOW + timedelta(seconds=16)),)},
    {"steps": (step(required_for_response=False, latest_finish=NOW + timedelta(minutes=6)),)},
])
def test_workflows_reject_invalid_deadlines_and_dependency_sets(changes):
    with pytest.raises(ValueError):
        workflow(**changes)


def test_cycles_and_unmarked_response_prerequisites_are_rejected():
    first = step(dependencies=("second",))
    second = step(step_id="second", dependencies=("classify",))
    with pytest.raises(ValueError, match="cycle"):
        workflow(steps=(first, second))
    background = step(step_id="background", required_for_response=False)
    with pytest.raises(ValueError, match="prerequisites"):
        workflow(steps=(step(dependencies=("background",)), background))


@pytest.mark.parametrize("changes", [
    {"workload_id": "sha256-input-2"}, {"workload_version": "tickets-v2"},
    {"configuration_id": "constraints-2"}, {"deployment_snapshot_id": "deployments-2"},
    {"calibration_id": "profiles-2"}, {"policy_version": "scorer-v2"},
    {"reference_time": NOW + timedelta(seconds=1)}, {"experiment_mode": "replay"},
])
def test_changed_comparison_conditions_cannot_join_same_cohort(changes):
    assert replace(context(), **changes).fingerprint() != context().fingerprint()


def test_comparison_context_has_no_run_id_or_credential_fields():
    payload = context().to_dict()
    assert not {"run_id", "credential_env", "api_key", "policy"}.intersection(payload)
    assert ComparisonContext.from_dict(payload).fingerprint() == context().fingerprint()
    assert len(context().fingerprint()) == 64


def test_infeasible_decision_has_reasons_without_fabricated_winner():
    rejected = RejectedCandidate("bad-1", ("deadline", "quality"))
    result = SchedulingDecision(
        "decision-2", "workflow-1", "classify", "balanced", decision().weights,
        NOW, context(), None, (), (rejected,), 0, {}, {},
    )
    assert result.winner is None
    assert result.rejected[0].constraints == ("deadline", "quality")
    assert SchedulingDecision.from_dict(result.to_dict()) == result


def test_decision_signed_quality_contribution_and_feasible_alternatives():
    result = decision(alternatives=(candidate(candidate_id="candidate-2"),), feasible_count=2)
    assert result.weighted_contributions["accuracy"] < 0
    assert result.feasible_count == 2


def test_decision_copies_score_inputs_and_prevents_postvalidation_mutation():
    scores = {"latency": .5}
    result = decision(normalized_metrics=scores, weighted_contributions={"latency": .2})
    scores["latency"] = float("nan")
    assert result.normalized_metrics["latency"] == .5
    with pytest.raises(TypeError):
        result.normalized_metrics["latency"] = float("nan")


def test_policy_choices_share_context_and_retain_exact_requested_weights():
    requested = {"latency": 0, "accuracy": 1, "cost": 2, "energy": 3, "carbon": 4}
    carbon = decision(policy="custom-carbon", weights=requested)
    balanced = decision()
    assert carbon.workflow_id == "workflow-1"
    assert carbon.policy == "custom-carbon"
    assert carbon.context.fingerprint() == balanced.context.fingerprint()
    assert carbon.to_dict()["weights"] == requested
    requested["carbon"] = 0
    assert carbon.weights["carbon"] == 4
    with pytest.raises(TypeError):
        carbon.weights["carbon"] = 0
    assert SchedulingDecision.from_dict(carbon.to_dict()) == carbon


@pytest.mark.parametrize("changes", [
    {"workflow_id": ""}, {"policy": ""},
    {"weights": {"latency": 1}},
    {"weights": {"latency": 1, "accuracy": 1, "cost": 1, "energy": 1, "carbon": 1, "unknown": 1}},
    {"weights": {"latency": 0, "accuracy": 0, "cost": 0, "energy": 0, "carbon": 0}},
    {"weights": {"latency": -1, "accuracy": 1, "cost": 1, "energy": 1, "carbon": 1}},
    {"weights": {"latency": True, "accuracy": 1, "cost": 1, "energy": 1, "carbon": 1}},
    {"weights": {"latency": float("nan"), "accuracy": 1, "cost": 1, "energy": 1, "carbon": 1}},
    {"weights": {"latency": float("inf"), "accuracy": 1, "cost": 1, "energy": 1, "carbon": 1}},
    {"weights": {"latency": 1e308, "accuracy": 1e308, "cost": 1e308, "energy": 1e308, "carbon": 1e308}},
])
def test_decisions_require_identifiable_policy_and_valid_complete_weights(changes):
    with pytest.raises(ValueError):
        decision(**changes)


@pytest.mark.parametrize("changes", [
    {"feasible_count": 2}, {"feasible_count": True}, {"winner": None},
    {"winner": candidate(step_id="other")},
    {"alternatives": (candidate(),), "feasible_count": 2},
    {"rejected": (RejectedCandidate("candidate-1", ("deadline",)),)},
    {"normalized_metrics": {"latency": 1.01}},
    {"normalized_metrics": {"latency": -.1}},
    {"normalized_metrics": {"unsupported": .5}},
    {"weighted_contributions": {"latency": float("nan"), "accuracy": -.1}},
    {"weighted_contributions": {"latency": .1}},
])
def test_decisions_reject_inconsistent_candidates_and_score_explanations(changes):
    with pytest.raises(ValueError):
        decision(**changes)


def test_execution_retains_planned_and_actual_identity_with_unknown_usage():
    result = completed(executed_deployment_id="fallback-edge", executed_model_id="llama3.2:latest", provider_reported_model_id="llama3.2:3b")
    assert result.planned_candidate.deployment_id == "edge-1"
    assert result.executed_deployment_id == "fallback-edge"
    assert result.provider_reported_model_id == "llama3.2:3b"
    assert result.input_tokens is None and result.output_tokens is None
    assert result.observations.energy.value is None
    assert ExecutionAttempt.from_dict(result.to_dict()) == result


@pytest.mark.parametrize("changes", [
    {"actual_start": None}, {"actual_finish": None}, {"output": None},
    {"actual_finish": NOW}, {"executed_deployment_id": None},
    {"wait_seconds": -1}, {"provider_seconds": float("inf")},
    {"input_tokens": -1}, {"output_tokens": True}, {"retry_index": .5},
    {"output_limit_tokens": 0}, {"output_limit_tokens": 32769},
    {"output_limit_tokens": True}, {"output_limit_tokens": "1024"},
    {"enable_thinking": 0}, {"enable_thinking": "false"},
    {"status": "unknown"}, {"status": "running"},
    {"status": "scheduled"}, {"error": "ProviderFailure"},
])
def test_execution_attempt_rejects_impossible_facts_and_nonfinite_usage(changes):
    with pytest.raises(ValueError):
        completed(**changes)


def test_pending_attempt_has_no_execution_facts_and_can_record_planned_finish():
    result = ExecutionAttempt("attempt-1", "workflow-1", candidate(), "scheduled", planned_finish=NOW + timedelta(seconds=2))
    assert result.actual_start is None
    with pytest.raises(ValueError):
        replace(result, input_tokens=0)
    with pytest.raises(ValueError):
        replace(result, planned_finish=NOW - timedelta(seconds=1))


def test_failed_predispatch_attempt_can_explain_failure_without_claiming_execution():
    attempt = ExecutionAttempt("attempt-1", "workflow-1", candidate(), "failed", error="ModelUnavailable")
    assert attempt.executed_deployment_id is None
    assert attempt.actual_start is None
    with pytest.raises(ValueError, match="actual_start"):
        replace(attempt, actual_finish=NOW)
    with pytest.raises(ValueError, match="actual_start"):
        replace(attempt, provider_reported_model_id="llama3.2:3b")
    with pytest.raises(ValueError, match="actual_start"):
        replace(attempt, executed_model_id="llama3.2:3b", executed_deployment_id="edge-1")


def test_carbon_sample_checks_issue_time_and_half_open_validity():
    sample = CarbonSample("IN-KA", NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2), 300, "fixture-forecast", "simulated")
    assert sample.is_valid_at(NOW + timedelta(hours=1), as_of=NOW)
    assert not sample.is_valid_at(NOW + timedelta(hours=2), as_of=NOW)
    assert not sample.is_valid_at(NOW, as_of=NOW)
    assert not sample.is_valid_at(NOW + timedelta(hours=1), as_of=NOW - timedelta(seconds=1))


@pytest.mark.parametrize("changes", [
    {"valid_until": NOW}, {"value": -1}, {"value": float("nan")},
    {"provenance": "unknown"}, {"unit": "kgCO2e/kWh"}, {"issued_at": None},
])
def test_carbon_sample_requires_known_finite_correctly_scaled_data(changes):
    sample = CarbonSample("IN-KA", NOW, NOW, NOW + timedelta(hours=1), 300, "fixture", "simulated")
    with pytest.raises(ValueError):
        replace(sample, **changes)


def legacy(**changes) -> LegacyCandidate:
    return replace(LegacyCandidate("classify", "llama", "us-west", 2, 1500, .8, 0, .001, .4), **changes)


def test_legacy_migration_converts_units_and_keeps_geographic_claim_separate():
    converted = legacy_candidate_to_planned(legacy(), deployment_id="edge-1", reference_time=NOW)
    assert converted.planned_start == NOW + timedelta(seconds=7200)
    assert converted.estimates.latency.value == 1.5
    assert converted.estimates.energy.value == .001
    assert converted.estimates.carbon.value == .4
    assert converted.region is None
    assert converted.legacy_location == "us-west"
    assert converted.estimates.accuracy.provenance == "estimated"
    assert converted.estimates.accuracy.source == "legacy-candidate-v1-unverified"
    same = legacy_candidate_to_planned(legacy(time_offset_hr=2.0), deployment_id="edge-1", reference_time=NOW)
    assert same.candidate_id == converted.candidate_id


def test_legacy_migration_requires_explicit_endpoint_and_accepts_explicit_known_region():
    with pytest.raises(ValueError):
        legacy_candidate_to_planned(legacy(), deployment_id="", reference_time=NOW)
    converted = legacy_candidate_to_planned(legacy(), deployment_id="edge-1", reference_time=NOW, region="IN-KA")
    assert converted.region == "IN-KA"


@pytest.mark.parametrize("changes", [
    {"time_offset_hr": .5}, {"time_offset_hr": -1}, {"time_offset_hr": True},
    {"time_offset_hr": float("nan")}, {"time_offset_hr": 10 ** 12},
    {"est_latency_ms": "1500"}, {"est_accuracy": 2}, {"est_cost_usd": -1},
])
def test_legacy_migration_rejects_fractional_or_invalid_values_without_truncation(changes):
    with pytest.raises(ValueError):
        legacy_candidate_to_planned(legacy(**changes), deployment_id="edge-1", reference_time=NOW)


def test_system_clock_returns_current_aware_utc():
    before = datetime.now(timezone.utc)
    observed = SystemClock().now()
    after = datetime.now(timezone.utc)
    assert observed.tzinfo is timezone.utc
    assert before <= observed <= after
