"""The durable planned worker executes actual prompts and preserves failure boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import threading
from uuid import uuid4

import pytest

from data.carbon_sources import HistoricalCarbonTrace
from data.profiles import build_profiles
from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.scheduling import CarbonSample, ExecutionAttempt
from workflow.planned_worker import submit_planned
from workflow.queue_replay import export_workflow, load_bundle
from workflow.queue_store import QueueStore
from workflow.worker import main, snapshot_summary, work_once
from workflow.accounting_report import execution_report

from test_staged_planning import inputs, NOW, at
from test_planned_replay import planned_inputs


class Clock:
    def __init__(self):
        self.time = NOW

    def now(self):
        return self.time


def probe(clock, calls=None, available=None, callback=None):
    def observe(deployment, **settings):
        if calls is not None:
            calls.append((deployment.deployment_id, settings))
        if callback:
            callback(deployment)
        healthy = available is None or available.get(deployment.deployment_id, False)
        return DeploymentHealth(deployment.deployment_id, clock.now(), healthy,
            tuple(deployment.models) if healthy else (), "test-probe", None if healthy else "connection-failed")
    return observe


def provider(clock, calls, callback=None):
    def dispatch(candidate, registry, prompt, **settings):
        calls.append((candidate, prompt))
        started = clock.now()
        clock.time += timedelta(seconds=1)
        output = {"classify": "account_access", "retrieve": "Wait 15 minutes. [KB-ACCOUNT-002]",
                  "draft": "Wait 15 minutes; a password reset does not shorten the lockout. [KB-ACCOUNT-002]",
                  "summarize": "The customer was advised to wait for the lockout to expire."}[candidate.step_id]
        if callback:
            callback(candidate)
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "completed",
            actual_start=started, actual_finish=clock.now(), output=output,
            executed_model_id=registry.get(candidate.deployment_id).provider_model(candidate.model_id),
            executed_deployment_id=candidate.deployment_id, input_tokens=32, output_tokens=12,
            output_limit_tokens=settings["output_limit_tokens"], enable_thinking=settings["enable_thinking"],
            completion_reason="stop")
    return dispatch


def submit(store, args, clock, **changes):
    kwargs = dict(forecasts=args["forecasts"], carbon_provider=args["carbon_provider"], policy=args["request"].policy,
                  clock=clock, workflow_id=args["request"].workflow_id, health_probe=probe(clock))
    kwargs.update(changes)
    return submit_planned(store, args["registry"], args["kb"], args["request"].input_text, args["profiles"], **kwargs)


@pytest.fixture
def planned(tmp_path, planned_inputs):
    args, *_ = planned_inputs
    store, clock = QueueStore(tmp_path / "planned.sqlite3"), Clock()
    submit(store, args, clock)
    yield store, args, clock
    store.close()


def run(planned, calls, **changes):
    store, args, clock = planned
    settings = dict(worker_id="worker", clock=clock, health_probe=probe(clock),
                    dispatcher=provider(clock, calls), heartbeat=False)
    settings.update(changes)
    return work_once(store, **settings)


def test_all_steps_use_fresh_plans_and_actual_dependency_outputs(planned, tmp_path):
    store, args, clock = planned
    calls = []
    for _ in range(4):
        run(planned, calls)
    state = store.read(args["request"].workflow_id)
    assert state.status == "completed" and len(state.attempts) == 4
    assert [candidate.step_id for candidate, _ in calls] == ["classify", "retrieve", "draft", "summarize"]
    assert "Wait 15 minutes." in calls[2][1]
    assert "password reset does not shorten" in calls[3][1]
    assert len(state.decisions) == 5
    assert len(state.accounting) == 4
    accounting = execution_report(state)
    assert accounting["resources"]["cost"]["total"] == 0
    assert accounting["timing"]["health_probe_records"] == 5
    assert accounting["timing"]["health_probe_seconds"] >= 0
    assert all(item["report"]["trigger"] == "dispatch-revalidation" for item in state.decisions[1:])
    assert state.attempts[-1].wait_seconds == 0.
    assert snapshot_summary(state)["planning_revision"] == 5
    bundle = export_workflow(store.path, state.request.workflow_id, tmp_path / "bundle")
    replayed = load_bundle(bundle)
    assert replayed.status == "completed" and len(replayed.decisions) == 5 and len(replayed.attempts) == 4
    assert run(planned, calls) == {"status": "idle"}


def test_delayed_background_resumes_using_saved_kb_profiles_and_carbon(planned_inputs, tmp_path, monkeypatch):
    args, *_ = planned_inputs
    store, clock, calls = QueueStore(tmp_path / "restart.sqlite3"), Clock(), []
    try:
        identity = submit(store, args, clock, summary_delay_seconds=60)
        for _ in range(3):
            run((store, args, clock), calls)
        assert store.read(identity).status == "response_ready"
        assert run((store, args, clock), calls)["status"] == "planned-wait"
    finally:
        store.close()
    from workflow.workload import KnowledgeBase
    monkeypatch.setattr(KnowledgeBase, "load", lambda *a, **k: pytest.fail("Restart loaded mutable KB files"))
    clock.time = at(60)
    restarted = QueueStore(tmp_path / "restart.sqlite3")
    try:
        result = run((restarted, args, clock), calls)
        assert result["status"] == "completed"
        assert len(calls) == 4 and calls[-1][0].step_id == "summarize"
        assert restarted.read(identity).attempts[-1].actual_start == at(60)
    finally:
        restarted.close()


def test_eco_policy_chooses_and_executes_a_carbon_window_without_forced_delay(planned_inputs, tmp_path):
    args, *_ = planned_inputs
    samples = (CarbonSample("simulated-grid", at(-1), NOW, at(60), 500., "test-early-carbon", "estimated"),
               CarbonSample("simulated-grid", at(-1), at(60), at(1000), 100., "test-late-carbon", "estimated"))
    args = {**args, "request": replace(args["request"], policy="eco"),
            "carbon_provider": HistoricalCarbonTrace("test-policy-delay", samples, timedelta(seconds=1000))}
    store, clock, calls = QueueStore(tmp_path / "policy-delay.sqlite3"), Clock(), []
    try:
        identity = submit(store, args, clock, start_offsets_seconds=(0, 60))
        state = store.read(identity)
        assert state.request.steps[-1].earliest_start == NOW  # Delay is a policy decision.
        assert state.jobs[-1].candidate.planned_start == at(60)
        for _ in range(3):
            run((store, args, clock), calls)
        assert run((store, args, clock), calls)["status"] == "planned-wait"
        assert len(calls) == 3 and store.read(identity).status == "response_ready"
        clock.time = at(60)
        assert run((store, args, clock), calls)["status"] == "completed"
        state = store.read(identity)
        assert state.attempts[-1].actual_start == at(60)
        assert state.attempts[-1].wait_seconds == 57.
        assert state.attempts[-1].planned_candidate.estimates.carbon.value == .1
    finally:
        store.close()


def test_provider_crash_never_automatically_repeats_ambiguous_planned_work(planned):
    store, args, clock = planned
    calls = []
    def crash(*_a, **_k):
        calls.append("called")
        raise RuntimeError("provider accepted request before crash")
    with pytest.raises(RuntimeError):
        run(planned, [], dispatcher=crash, lease_seconds=5)
    clock.time += timedelta(seconds=6)
    assert run(planned, [], dispatcher=crash) == {"status": "idle"}
    assert calls == ["called"]
    assert store.read(args["request"].workflow_id).status == "needs-reconciliation"


def test_cancellation_during_preparation_invalidates_atomic_install(planned):
    store, args, clock = planned
    observe = probe(clock, callback=lambda _dep: store.cancel(args["request"].workflow_id, now=clock.now()))
    result = run(planned, [], health_probe=observe,
                 dispatcher=lambda *a, **k: pytest.fail("Cancelled plan dispatched"))
    assert result["status"] == "planning-conflict"
    assert store.read(args["request"].workflow_id).status == "cancelled"
    assert not store.read(args["request"].workflow_id).attempts


def test_cancellation_during_planned_provider_preserves_usage_and_blocks_downstream(planned, tmp_path):
    store, args, clock = planned
    calls = []
    dispatch = provider(clock, calls, callback=lambda _candidate: store.cancel(args["request"].workflow_id, now=clock.now()))
    result = run(planned, calls, dispatcher=dispatch)
    state = store.read(args["request"].workflow_id)
    assert result["status"] == "cancelled" and len(state.attempts) == 1
    assert state.attempts[0].input_tokens == 32
    assert run(planned, calls) == {"status": "idle"} and len(calls) == 1
    bundle = export_workflow(store.path, state.request.workflow_id, tmp_path / "cancelled-bundle")
    assert load_bundle(bundle).status == "cancelled"


def test_unknown_quality_is_retained_without_jobs_and_replans_are_rate_limited(planned_inputs, inputs, tmp_path):
    args, *_ = planned_inputs
    # Retain all execution measurements, but remove the synthetic fixture judgments.
    # Keep size/resource coverage intact so quality alone blocks the plan.
    records = [{**record, "quality_score": None, "quality_source": "unknown"} for record in inputs["_trials"]]
    profiles = build_profiles(records, calibration_id="unreviewed-test", quality_policy=args["profiles"]["quality_policy"])
    forecasts = {key: replace(value, profile_fingerprint=profiles["fingerprint"]) for key, value in args["forecasts"].items()}
    args = {**args, "profiles": profiles, "forecasts": forecasts}
    store, clock, calls = QueueStore(tmp_path / "infeasible.sqlite3"), Clock(), []
    try:
        identity = submit(store, args, clock)
        state = store.read(identity)
        assert state.status == "infeasible" and state.jobs == () and len(state.decisions) == 1
        assert state.decisions[0]["report"]["result"]["rejection_counts"]["unknown-accuracy"] > 0
        assert "unknown-latency" not in state.decisions[0]["report"]["result"]["rejection_counts"]
        assert run((store, args, clock), calls) == {"status": "idle"}
        clock.time = at(10)
        assert run((store, args, clock), calls)["status"] == "planning-infeasible"
        assert len(store.read(identity).decisions) == 2 and calls == []
        assert run((store, args, clock), calls) == {"status": "idle"}
    finally:
        store.close()


def test_racing_workers_make_one_provider_call(planned, tmp_path):
    store, args, clock = planned
    barrier, calls, call_lock = threading.Barrier(2), [], threading.Lock()
    def dispatch(*arguments, **settings):
        with call_lock:
            return provider(clock, calls)(*arguments, **settings)
    observe = probe(clock, callback=lambda _dep: barrier.wait(timeout=10))
    def contender(name):
        connection = QueueStore(store.path)
        try:
            return work_once(connection, worker_id=name, clock=clock, health_probe=observe,
                             dispatcher=dispatch, heartbeat=False)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ("one", "two")))
    assert len(calls) == 1
    assert any(result["status"] == "planning-conflict" for result in results)
    assert len(store.read(args["request"].workflow_id).attempts) == 1


def test_expired_decision_releases_without_inference_and_replays_failure(planned, tmp_path, monkeypatch):
    store, args, clock = planned
    original = store.install_plan
    def delayed(*a, **kwargs):
        result = original(*a, **kwargs)
        if result is not None:
            clock.time += timedelta(seconds=6)
        return result
    monkeypatch.setattr(store, "install_plan", delayed)
    result = run(planned, [], dispatcher=lambda *a, **k: pytest.fail("Expired decision dispatched"))
    state = store.read(args["request"].workflow_id)
    assert result["status"] == "replan-required" and result["reason"] == "planning-decision-expired"
    assert state.status == "planning-failed" and state.attempts == ()
    assert state.decisions[-1]["report"]["error_code"] == "planning-decision-expired"
    assert any(event["kind"] == "released_undispatched" for event in state.events)
    bundle = export_workflow(store.path, state.request.workflow_id, tmp_path / "expired-bundle")
    assert load_bundle(bundle).status == "planning-failed"


def test_actual_dispatch_carbon_coverage_is_checked_after_install(planned_inputs, tmp_path, monkeypatch):
    args, *_ = planned_inputs
    samples = (CarbonSample("simulated-grid", at(-1), NOW, at(60), 500., "test-early-carbon", "estimated"),
               CarbonSample("simulated-grid", at(-1), at(60), at(1000), 100., "test-late-carbon", "estimated"))
    args = {**args, "carbon_provider": HistoricalCarbonTrace("test-changing-trace", samples, timedelta(seconds=1000))}
    store, clock, calls = QueueStore(tmp_path / "carbon.sqlite3"), Clock(), []
    try:
        identity = submit(store, args, clock, max_decision_age_seconds=120.)
        assert store.read(identity).status == "scheduled"
        original = store.install_plan
        moved = []
        def cross_window(*a, **kwargs):
            result = original(*a, **kwargs)
            if result is not None and not moved:
                moved.append(True)
                clock.time = at(59)  # The estimated inference would cross the carbon boundary.
            return result
        monkeypatch.setattr(store, "install_plan", cross_window)
        result = run((store, args, clock), calls)
        assert result["reason"] == "planning-carbon-changed" and calls == []
        assert store.read(identity).status == "planning-failed"
        clock.time = at(70)
        result = run((store, args, clock), calls)
        assert result["status"] == "scheduled" and len(calls) == 1
        assert calls[0][0].estimates.carbon.value == .1
        assert store.read(identity).attempts[0].actual_start == at(70)
    finally:
        store.close()


def test_probe_elapsed_time_consumes_response_budget_before_submission(planned_inputs, tmp_path):
    args, *_ = planned_inputs
    store, clock = QueueStore(tmp_path / "probe-latency.sqlite3"), Clock()
    def slow(deployment, **settings):
        clock.time += timedelta(seconds=11)
        return probe(clock)(deployment, **settings)
    try:
        identity = submit(store, args, clock, response_seconds=10, health_probe=slow)
        state = store.read(identity)
        assert state.status == "infeasible" and not state.jobs
        assert state.request.arrived_at == NOW
        assert state.decisions[0]["report"]["now"] == at(11).isoformat()
    finally:
        store.close()


def test_stale_forecast_is_a_retained_preparation_failure(planned_inputs, tmp_path):
    args, *_ = planned_inputs
    clock, store = Clock(), QueueStore(tmp_path / "stale-forecast.sqlite3")
    args = {**args, "forecasts": {key: replace(value, issued_at=at(-10), valid_until=NOW)
                                  for key, value in args["forecasts"].items()}}
    try:
        identity = submit(store, args, clock)
        state = store.read(identity)
        assert state.status == "planning-failed" and not state.jobs and not state.attempts
        assert state.decisions[0]["report"]["error_code"] == "planning-input-unavailable"
    finally:
        store.close()


def test_pre_dispatch_outage_replans_to_another_eligible_destination(planned_inputs, inputs, tmp_path):
    args, *_ = planned_inputs
    first = next(iter(args["registry"]))
    second = replace(first, deployment_id="remote", endpoint="http://remote-fixture.invalid:11434",
                     region="test-other-location", grid="other-grid")
    registry = DeploymentRegistry((first, second))
    records = []
    for deployment in registry:
        for record in inputs["_trials"]:
            records.append({**record, "trial_id": deployment.deployment_id + "-" + record["trial_id"],
                "deployment_id": deployment.deployment_id, "deployment_fingerprint": deployment.config_fingerprint(),
                "latency_seconds": record["latency_seconds"] * (2 if deployment.deployment_id == "remote" else 1),
                "quality_review_fingerprint": "a" * 64, "case_id": "test-case"})
    profiles = build_profiles(records, calibration_id="test-two-destinations", quality_policy=args["profiles"]["quality_policy"])
    forecasts = {key: replace(value, profile_fingerprint=profiles["fingerprint"]) for key, value in args["forecasts"].items()}
    samples = tuple(CarbonSample(grid, at(-1), NOW, at(1000), 500., "test-grid-estimate", "estimated")
                    for grid in ("simulated-grid", "other-grid"))
    args = {**args, "registry": registry, "profiles": profiles, "forecasts": forecasts,
            "carbon_provider": HistoricalCarbonTrace("test-two-grids", samples, timedelta(seconds=1000))}
    clock, store, calls = Clock(), QueueStore(tmp_path / "two-destinations.sqlite3"), []
    available = {"local": True, "remote": True}
    try:
        identity = submit(store, args, clock, health_probe=probe(clock, available=available))
        assert store.read(identity).jobs[0].candidate.deployment_id == "local"
        available["local"] = False
        result = run((store, args, clock), calls, health_probe=probe(clock, available=available))
        assert result["status"] == "scheduled" and len(calls) == 1
        assert calls[0][0].deployment_id == "remote"
        state = store.read(identity)
        assert state.attempts[0].executed_deployment_id == "remote"
        assert state.decisions[-1]["report"]["health_snapshot"][0]["reachable"] is False
    finally:
        store.close()


def test_explicit_retry_after_known_preflight_failure_replans_without_duplicate_ack(planned):
    store, args, clock = planned
    def rejected(candidate, registry, prompt, **settings):
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "failed", error="missing-credential",
            output_limit_tokens=settings["output_limit_tokens"], enable_thinking=settings["enable_thinking"])
    assert run(planned, [], dispatcher=rejected)["status"] == "failed"
    store.retry(args["request"].workflow_id, "classify", now=clock.now())
    calls = []
    run(planned, calls)
    state = store.read(args["request"].workflow_id)
    assert [item.retry_index for item in state.attempts] == [0, 1]
    assert state.attempts[0].actual_start is None and len(calls) == 1


def test_planned_cli_records_and_inspects_infeasibility_without_dispatch(planned_inputs, tmp_path, monkeypatch, capsys):
    args, *_ = planned_inputs
    import workflow.planned_worker as module
    from shared.scheduling import SystemClock
    monkeypatch.setattr(module, "probe_deployment", probe(SystemClock()))
    registry, profiles, database = tmp_path / "registry.json", tmp_path / "profiles.json", tmp_path / "cli.sqlite3"
    registry.write_text(json.dumps(args["registry"].to_dict()), encoding="utf-8")
    profiles.write_text(json.dumps(args["profiles"]), encoding="utf-8")
    assert main(["--db", str(database), "submit-planned", "--ticket", args["request"].input_text,
                 "--config", str(registry), "--profiles", str(profiles)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "infeasible" and summary["jobs"] == [] and summary["planning_revision"] == 1
    assert main(["--db", str(database), "status", summary["workflow_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "infeasible"
    assert main(["--db", str(database), "run", "--once"]) == 0
    assert capsys.readouterr().out == ""


def test_invalid_custom_weights_never_probe_or_enqueue(planned_inputs, tmp_path):
    args, *_ = planned_inputs
    store, clock = QueueStore(tmp_path / "invalid.sqlite3"), Clock()
    try:
        with pytest.raises(ValueError):
            submit(store, args, clock, policy="custom", custom_weights={"latency": 1},
                   health_probe=lambda *a, **k: pytest.fail("Invalid request probed a provider"))
        assert store.planned_workflow_ids() == ()
    finally:
        store.close()


def test_slow_tracker_start_rechecks_plan_before_provider_dispatch(planned_inputs, inputs, tmp_path):
    from unittest.mock import Mock
    from test_energy import tracker

    args, *_ = planned_inputs
    registry = DeploymentRegistry([replace(deployment, endpoint="http://127.0.0.1:11434")
                                   for deployment in args["registry"]])
    profiles = build_profiles([{**trial,
        "deployment_fingerprint": registry.get(trial["deployment_id"]).config_fingerprint(),
        "quality_review_fingerprint": "a" * 64, "case_id": "test-case"} for trial in inputs["_trials"]],
        calibration_id="test-loopback", quality_policy=args["profiles"]["quality_policy"])
    args = {**args, "registry": registry, "profiles": profiles,
            "forecasts": {key: replace(value, profile_fingerprint=profiles["fingerprint"])
                          for key, value in args["forecasts"].items()}}
    clock, store = Clock(), QueueStore(tmp_path / "slow-telemetry.sqlite3")
    sensor = tracker()
    sensor.start.side_effect = lambda: setattr(clock, "time", clock.now() + timedelta(seconds=10))
    try:
        identity = submit(store, args, clock, energy_mode="codecarbon", energy_country="IND")
        result = run((store, args, clock), [], tracker_factory=Mock(return_value=sensor),
            dispatcher=lambda *a, **k: pytest.fail("Expired plan reached provider"))
        state = store.read(identity)
        assert result["status"] == "replan-required" and result["reason"] == "planning-decision-expired"
        assert not state.attempts and not state.accounting
        assert state.jobs[0].dispatched_at is None
        sensor.stop.assert_called_once_with()
        assert state.decisions[-1]["report"]["error_code"] == "planning-decision-expired"
    finally:
        store.close()
