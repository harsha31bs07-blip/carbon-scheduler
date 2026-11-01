"""Exercise the schema-3 plan -> explicit HTTP dispatch -> saved evidence path."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import (
    ComparisonContext, ExecutionAttempt, MetricEstimate, PlannedCandidate,
    StepEstimates,
)
from workflow.dispatch import dispatch_planned_candidate
from workflow.scheduling_records import comparison_compatible, load_record, save_record


NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.time = NOW

    def now(self):
        current = self.time
        self.time += timedelta(seconds=1)
        return current


def plan(**overrides):
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    candidate = PlannedCandidate(
        "candidate-1", "classify", "small", "worker-b", NOW,
        StepEstimates(MetricEstimate(2, "s", "estimated", "fixture-profile"),
                      unknown("ratio"), unknown("USD"), unknown("kWh"), unknown("gCO2e")),
    )
    return replace(candidate, **overrides)


def registry(**overrides):
    second = Deployment("worker-b", "ollama", "http://worker-b:11434", {"small": "exact-b:3b"})
    return DeploymentRegistry([
        Deployment("worker-a", "ollama", "http://worker-a:11434", {"small": "exact-a:3b"}),
        replace(second, **overrides),
    ])


def context():
    return ComparisonContext(
        workload_id="input-content-hash", workload_version="tickets-v1",
        configuration_id="constraints-and-configuration-hash", deployment_snapshot_id="registry-hash",
        calibration_id="profiles-v1", policy_version="weighted-v1", reference_time=NOW,
        experiment_mode="live",
    )


def test_selected_plan_dispatches_and_roundtrips_observed_identity(monkeypatch, tmp_path):
    sent = []

    class Response:
        status_code = 200

        def json(self):
            return {"response": "classified", "model": "server-reported-b:3b", "eval_count": 7,
                    "done": True, "done_reason": "length"}

        def close(self):
            pass

    def post(endpoint, **kwargs):
        sent.append((endpoint, kwargs))
        return Response()

    monkeypatch.setattr(requests, "post", post)
    attempt = dispatch_planned_candidate(plan(), registry(), "a real ticket",
                                         workflow_id="ticket-1", clock=Clock())
    assert sent[0][0] == "http://worker-b:11434/api/generate"
    assert sent[0][1]["json"]["model"] == "exact-b:3b"
    assert len(sent) == 1
    assert attempt.status == "completed"
    assert attempt.planned_candidate.model_id == "small"
    assert attempt.executed_model_id == "exact-b:3b"
    assert attempt.provider_reported_model_id == "server-reported-b:3b"
    assert attempt.executed_deployment_id == "worker-b"
    assert attempt.input_tokens is None and attempt.output_tokens == 7
    assert attempt.observations.latency.provenance == "measured"
    assert attempt.observations.energy.value is None
    assert attempt.observations.carbon.value is None
    assert attempt.observations.accuracy.value is None
    assert attempt.wait_seconds is None
    assert attempt.output_limit_tokens == 1024 and attempt.enable_thinking is None
    assert attempt.completion_reason == "length" and attempt.output == "classified"
    saved = tmp_path / "attempt.json"
    save_record(saved, attempt)
    assert load_record(saved) == attempt


@pytest.mark.parametrize("candidate, deployments, kwargs", [
    (plan(planned_start=NOW + timedelta(seconds=1)), registry(), {}),
    (plan(deployment_id="missing-worker"), registry(), {}),
    (plan(model_id="unsupported-model"), registry(), {}),
    (plan(region="unverified-region"), registry(), {}),
    (plan(), registry(), {"workflow_id": ""}),
    (plan(), registry(), {"attempt_id": ""}),
    (plan(), registry(), {"prompt": ""}),
])
def test_invalid_or_future_plan_never_calls_provider(monkeypatch, candidate, deployments, kwargs):
    def post(*args, **kwargs):
        pytest.fail("Invalid or not-yet-due plan reached a provider")

    monkeypatch.setattr(requests, "post", post)
    parameters = {"prompt": "ticket", "workflow_id": "ticket-1", **kwargs}
    with pytest.raises(ValueError):
        dispatch_planned_candidate(candidate, deployments, clock=Clock(), **parameters)


def test_timeout_records_selected_identity_without_retry_or_provider_text(monkeypatch, tmp_path):
    calls = []

    def post(endpoint, **kwargs):
        calls.append(endpoint)
        raise requests.exceptions.Timeout("sensitive-provider-debug-text")

    monkeypatch.setattr(requests, "post", post)
    attempt = dispatch_planned_candidate(plan(), registry(), "ticket",
                                         workflow_id="ticket-1", clock=Clock())
    assert attempt.status == "failed" and attempt.error == "request-timeout"
    assert attempt.executed_deployment_id == "worker-b"
    assert attempt.executed_model_id == "exact-b:3b"
    assert attempt.actual_start == NOW and attempt.actual_finish == NOW + timedelta(seconds=1)
    assert attempt.output is None and attempt.observations is None
    assert len(calls) == 1
    saved = tmp_path / "failed.json"
    save_record(saved, attempt)
    assert "sensitive-provider-debug-text" not in saved.read_text()
    assert load_record(saved) == attempt


def test_missing_credential_records_failure_without_claiming_dispatch(monkeypatch):
    monkeypatch.delenv("WORKER_B_KEY", raising=False)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Missing key reached network"))
    attempt = dispatch_planned_candidate(plan(), registry(credential_env="WORKER_B_KEY"), "ticket",
                                         workflow_id="ticket-1", clock=Clock())
    assert attempt.status == "failed" and attempt.error == "missing-credential"
    assert attempt.executed_deployment_id is None and attempt.executed_model_id is None
    assert attempt.actual_start is None and attempt.actual_finish is None


@pytest.mark.parametrize("thinking", [None, False, True])
def test_dispatch_passes_and_persists_exact_generation_settings(monkeypatch, tmp_path, thinking):
    sent = []

    class Response:
        status_code = 200

        def json(self):
            return {"response": "ok", "done": True}

        def close(self):
            pass

    def post(endpoint, **kwargs):
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(requests, "post", post)
    attempt = dispatch_planned_candidate(plan(), registry(), "ticket", workflow_id="ticket-1",
                                         clock=Clock(), output_limit_tokens=41,
                                         enable_thinking=thinking)
    assert sent[0]["options"]["num_predict"] == 41
    if thinking is None:
        assert "think" not in sent[0]
    else:
        assert sent[0]["think"] is thinking
    assert attempt.output_limit_tokens == 41
    assert attempt.enable_thinking is thinking
    saved = tmp_path / "controls.json"
    save_record(saved, attempt)
    assert load_record(saved) == attempt


@pytest.mark.parametrize("controls", [
    {"output_limit_tokens": 0}, {"output_limit_tokens": 32769},
    {"output_limit_tokens": True}, {"enable_thinking": 1},
])
def test_invalid_generation_controls_fail_before_provider(monkeypatch, controls):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Invalid control reached network"))
    with pytest.raises(ValueError):
        dispatch_planned_candidate(plan(), registry(), "ticket", workflow_id="ticket-1",
                                   clock=Clock(), **controls)


def test_failed_predispatch_attempt_retains_requested_controls(monkeypatch):
    deployment = Deployment("worker-b", "gemini", "https://gateway.example",
                            {"small": "exact-model"}, credential_env="NOT_REQUIRED")
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Unsupported setting reached network"))
    attempt = dispatch_planned_candidate(plan(), DeploymentRegistry([deployment]), "ticket",
                                         workflow_id="ticket-1", clock=Clock(),
                                         output_limit_tokens=29, enable_thinking=False)
    assert attempt.error == "unsupported-generation-setting"
    assert attempt.actual_start is None and attempt.executed_model_id is None
    assert attempt.output_limit_tokens == 29 and attempt.enable_thinking is False


def test_final_deadline_check_prevents_dispatch_after_preparation(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Expired plan reached network"))
    attempt = dispatch_planned_candidate(plan(), registry(), "ticket", workflow_id="ticket-1",
                                         clock=Clock(), latest_finish=NOW + timedelta(seconds=1),
                                         output_limit_tokens=31, enable_thinking=False)
    assert attempt.status == "failed" and attempt.error == "step-deadline-exceeded"
    assert attempt.actual_start is None and attempt.actual_finish is None
    assert attempt.executed_model_id is None and attempt.executed_deployment_id is None
    assert attempt.output_limit_tokens == 31 and attempt.enable_thinking is False


def test_final_deadline_check_clips_timeout_and_retains_actual_start(monkeypatch):
    sent = []

    class Response:
        status_code = 200

        def json(self):
            return {"response": "ok", "done": True, "done_reason": "stop"}

        def close(self):
            pass

    def post(endpoint, **kwargs):
        sent.append(kwargs["timeout"])
        return Response()

    monkeypatch.setattr(requests, "post", post)
    deadline = (NOW + timedelta(seconds=4)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    attempt = dispatch_planned_candidate(plan(), registry(), "ticket", workflow_id="ticket-1",
                                         clock=Clock(), latest_finish=deadline, timeout_seconds=20)
    assert sent == [(3.0, 3.0)]
    assert attempt.actual_start == NOW + timedelta(seconds=1)
    assert attempt.actual_finish == NOW + timedelta(seconds=2)
    assert attempt.completion_reason == "stop"


@pytest.mark.parametrize("deadline", [datetime(2026, 10, 19), "2026-10-19T00:00:00Z", 1])
def test_deadline_requires_aware_datetime_before_dispatch(deadline):
    with pytest.raises(ValueError, match="latest_finish"):
        dispatch_planned_candidate(plan(), registry(), "ticket", workflow_id="ticket-1",
                                   clock=Clock(), latest_finish=deadline)


@pytest.mark.parametrize("timeout", [0, True, 301, float("nan"), float("inf")])
def test_deadline_clipping_does_not_hide_invalid_original_timeout(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        dispatch_planned_candidate(plan(), registry(), "ticket", workflow_id="ticket-1",
                                   clock=Clock(), latest_finish=NOW, timeout_seconds=timeout)


def test_unrepresentable_finish_fails_before_inference(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Invalid finish reached network"))
    candidate = plan()
    estimates = replace(candidate.estimates, latency=MetricEstimate(1e300, "s", "estimated", "bad-profile"))
    with pytest.raises(ValueError, match="timestamp range"):
        dispatch_planned_candidate(replace(candidate, estimates=estimates), registry(), "ticket",
                                   workflow_id="ticket-1", clock=Clock())


@pytest.mark.parametrize("field,value", [
    ("workload_id", "different-input"), ("workload_version", "tickets-v2"),
    ("configuration_id", "different-limits"), ("deployment_snapshot_id", "another-endpoint"),
    ("calibration_id", "profiles-v2"), ("policy_version", "weighted-v2"),
    ("reference_time", NOW + timedelta(seconds=1)), ("experiment_mode", "simulation"),
])
def test_changed_reference_conditions_are_not_comparable(field, value):
    original = context()
    assert comparison_compatible(original, ComparisonContext.from_dict(original.to_dict()))
    assert not comparison_compatible(original, replace(original, **{field: value}))


def test_legacy_context_is_not_implicitly_promoted():
    with pytest.raises(ValueError, match="schema-3"):
        comparison_compatible(context(), {"schema_version": 2, "workload_id": "input-content-hash"})


@pytest.mark.parametrize("malformed", [
    '{"schema_version":3,"schema_version":2,"record_type":"ComparisonContext"}',
    '{"schema_version":3,"record_type":"ComparisonContext","extra":NaN}',
    '{"schema_version":3,"record_type":"ComparisonContext","extra":Infinity}',
    '{"schema_version":3,"record_type":"UnrecognizedFutureRecord"}',
    '{"schema_version":2,"record_type":"ComparisonContext"}',
    '{"schema_version":1,"record_type":"ComparisonContext"}',
    '{"schema_version":4,"record_type":"ComparisonContext"}',
    '[{"record_type":"ComparisonContext"}]',
    '{"schema_version":3,"record_type":null}',
])
def test_invalid_or_legacy_evidence_fails_closed(tmp_path, malformed):
    path = tmp_path / "invalid.json"
    path.write_text(malformed, encoding="utf-8")
    with pytest.raises(ValueError):
        load_record(path)


def test_unknown_sensitive_field_is_rejected(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({**context().to_dict(), "api_key": "must-not-serialize"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_record(path)
