"""Frozen protocols bind fair trial conditions without provider access."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

import pytest

from data.quality_reviews import fingerprint
from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry
from shared.scheduling import MetricEstimate, PlannedCandidate, StepEstimates
from workflow.evaluation_protocol import (
    schedule_trials, seal_protocol, validate_protocol, validate_trial_snapshot,
)
from workflow.execution_accounting import make_accounting_context
from workflow.planning_records import decode_knowledge_base
from workflow.queue_store import QueueJob, WorkflowSnapshot
from workflow.workload import KnowledgeBase, STEP_ORDER, build_workflow_request

from test_planned_replay import planned_inputs
from test_staged_planning import inputs


NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
CONTROLS = {"search_limit": 100000, "health_max_age_seconds": 300., "minimum_improvement": .01,
            "replan_interval_seconds": 10., "probe_timeout_seconds": 3., "max_decision_age_seconds": 5.}


def kb_snapshot(kb):
    return {"schema_version": 1, "kb_version": kb.version, "description": kb.description,
            "categories": list(kb.categories), "articles": [article.to_dict() for article in kb.articles]}


def fixed_arm(arm_id, model_id="small", deployment_id="local"):
    return {"arm_id": arm_id, "kind": "fixed", "policy": "fixed", "custom_weights": None,
        "allowed_pairs": {step: [{"deployment_id": deployment_id, "model_id": model_id}] for step in STEP_ORDER},
        "allow_summary_deferral": False, "start_offsets_seconds": [0], "selection_source": "explicit-test-baseline"}


@pytest.fixture
def protocol():
    registry = DeploymentRegistry([Deployment("local", "ollama", "http://localhost:11434",
        {"small": "small:tag", "large": "large:tag"})])
    return seal_protocol({"schema_version": 1, "record_type": "EvaluationProtocol", "protocol_id": "test-evaluation",
        "experiment_mode": "simulation", "created_at": NOW.isoformat(),
        "dataset": {"dataset_id": "test-pilot", "split": "pilot", "provenance": "offline test fixtures",
            "cases": [{"case_id": "account", "ticket": "My CarbonDesk account is locked after five failed login attempts."},
                      {"case_id": "billing", "ticket": "Please help me download a CarbonDesk invoice."}]},
        "registry": registry.to_dict(), "knowledge_base": kb_snapshot(KnowledgeBase.load()), "profiles": None,
        "forecasts": {"account": [], "billing": []}, "carbon": None, "tariffs": None,
        "energy_mode": "none", "energy_country": None,
        "arms": [fixed_arm("baseline", "large"), fixed_arm("small")],
        "response_seconds": 300, "completion_seconds": 600, "timeout_seconds": 120., "quality_floor": .8,
        "repeats": 2, "seed": 42, "baseline_arm_id": "baseline", "poll_seconds": .1,
        "planning_controls": dict(CONTROLS), "source_manifest": {"workflow/evaluation_protocol.py": "a" * 64}})


def fixed_snapshot(protocol, trial):
    registry = DeploymentRegistry.from_dict(protocol["registry"])
    kb = decode_knowledge_base(protocol["knowledge_base"])
    case = next(case for case in protocol["dataset"]["cases"] if case["case_id"] == trial["case_id"])
    arm = next(arm for arm in protocol["arms"] if arm["arm_id"] == trial["arm_id"])
    request = build_workflow_request(case["ticket"], workflow_id=trial["workflow_id"], arrived_at=NOW, kb=kb,
        response_budget_seconds=protocol["response_seconds"], completion_budget_seconds=protocol["completion_seconds"],
        policy="fixed", enable_thinking=False)
    request = replace(request, steps=tuple(replace(step, allow_deferral=False) for step in request.steps))
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    estimates = StepEstimates(*(unknown(unit) for unit in ("s", "ratio", "USD", "kWh", "gCO2e")))
    candidates = tuple(PlannedCandidate("plan-" + step.step_id, step.step_id,
        arm["allowed_pairs"][step.step_id][0]["model_id"], arm["allowed_pairs"][step.step_id][0]["deployment_id"],
        NOW, estimates) for step in request.steps)
    jobs = tuple(QueueJob(step, candidate, "scheduled", 0, None, None, None, None, None, (), None, 0., None, None)
                 for step, candidate in zip(request.steps, candidates))
    from workflow.evaluation_protocol import _workload_fingerprint
    config = {"registry_snapshot": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint(),
        "kb_fingerprint": kb.fingerprint(), "workload_fingerprint": _workload_fingerprint(request, kb),
        "worker_version": "sqlite-worker-v1", "policy_version": "fixed-explicit-v1",
        "accounting_version": "execution-accounting.v1", "experiment_mode": protocol["experiment_mode"],
        "timeout_seconds": protocol["timeout_seconds"], "max_retries": 3}
    return WorkflowSnapshot(request, config, "scheduled", jobs, (), None, candidates, (),
        accounting_context=make_accounting_context(registry, request))


@pytest.fixture
def planned_protocol(protocol, planned_inputs):
    args, *_ = planned_inputs
    value = deepcopy(protocol)
    value.update(registry=args["registry"].to_dict(), knowledge_base=kb_snapshot(args["kb"]),
        profiles=args["profiles"], carbon=args["carbon_provider"].to_dict(),
        created_at=args["request"].arrived_at.isoformat())
    value["dataset"]["cases"] = [{"case_id": "account", "ticket": args["request"].input_text}]
    value["arms"] = [fixed_arm("baseline"), {**fixed_arm("planned"), "kind": "planned", "policy": "speed",
        "allow_summary_deferral": True, "start_offsets_seconds": [0, 10]}]
    value["forecasts"] = {"account": [item.to_dict() for item in args["forecasts"].values()]}
    return seal_protocol(value)


def test_protocol_roundtrip_is_independent_and_exact(protocol):
    validated = validate_protocol(json.loads(json.dumps(protocol)))
    assert validated == protocol
    validated["dataset"]["cases"][0]["ticket"] = "caller changed it"
    assert protocol["dataset"]["cases"][0]["ticket"] != "caller changed it"
    assert seal_protocol({key: value for key, value in protocol.items() if key != "fingerprint"}) == protocol


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema_version=True),
    lambda p: p.update(record_type="Other"),
    lambda p: p.update(extra="unsupported"),
    lambda p: p.update(protocol_id="../unsafe"),
    lambda p: p.update(experiment_mode="replay"),
    lambda p: p.update(created_at="2026-11-01T12:00:00"),
    lambda p: p.update(dataset={}),
    lambda p: p["dataset"].update(split="calibration"),
    lambda p: p["dataset"]["cases"].append(p["dataset"]["cases"][0]),
    lambda p: p["dataset"]["cases"][0].update(answer="do not leak reference answers"),
    lambda p: p["dataset"]["cases"][1].update(ticket=p["dataset"]["cases"][0]["ticket"]),
    lambda p: p.update(response_seconds=True),
    lambda p: p.update(completion_seconds=1),
    lambda p: p.update(timeout_seconds=float("inf")),
    lambda p: p.update(quality_floor=-.1),
    lambda p: p.update(repeats=0),
    lambda p: p.update(seed=-1),
    lambda p: p.update(poll_seconds=.001),
    lambda p: p.update(baseline_arm_id="missing"),
    lambda p: p.update(arms=p["arms"][:1]),
    lambda p: p["arms"][1].update(arm_id="baseline"),
    lambda p: p["arms"][1].update(policy="eco"),
    lambda p: p["arms"][1].update(allow_summary_deferral=True),
    lambda p: p["arms"][1].update(start_offsets_seconds=[0, 10]),
    lambda p: p["arms"][1]["allowed_pairs"]["draft"].append({"deployment_id": "local", "model_id": "large"}),
    lambda p: p["arms"][1]["allowed_pairs"]["draft"][0].update(model_id="unavailable"),
    lambda p: p["arms"][1]["allowed_pairs"].pop("summarize"),
    lambda p: p["arms"][1].update(kind="random"),
    lambda p: p["planning_controls"].update(search_limit=100001),
    lambda p: p["planning_controls"].update(probe_timeout_seconds=0),
    lambda p: p["planning_controls"].update(max_decision_age_seconds=False),
    lambda p: p["forecasts"].pop("billing"),
    lambda p: p.update(energy_mode="codecarbon", energy_country=None),
    lambda p: p.update(tariffs={}),
    lambda p: p.update(carbon={}),
])
def test_malformed_protocol_remains_invalid_after_rehash(protocol, mutate):
    changed = deepcopy(protocol)
    mutate(changed)
    with pytest.raises(ValueError):
        seal_protocol(changed)


@pytest.mark.parametrize("path", ["/etc/passwd", "C:/private", "../outside.py", "workflow/../outside.py",
    "workflow\\worker.py", "workflow//worker.py", "./workflow/worker.py", ".env", "config/deployments.local.json",
    "credentials.json", "secret/secrets.json", "id_rsa", "", "workflow/CON.py", "NUL", "folder/file.py.",
    "folder /file.py", "folder/line\nname.py", ".env.local"])
def test_source_paths_refuse_private_or_escaping_entries(protocol, path):
    with pytest.raises(ValueError):
        seal_protocol({**protocol, "source_manifest": {path: "a" * 64}})


def test_protocol_content_change_requires_a_new_fingerprint(protocol):
    changed = deepcopy(protocol)
    changed["quality_floor"] = .7
    with pytest.raises(ValueError, match="fingerprint"):
        validate_protocol(changed)
    assert seal_protocol(changed)["fingerprint"] != protocol["fingerprint"]


def test_source_paths_cannot_alias_on_windows(protocol):
    with pytest.raises(ValueError, match="case-insensitive"):
        seal_protocol({**protocol, "source_manifest": {"workflow/worker.py": "a" * 64,
                                                       "WORKFLOW/worker.py": "b" * 64}})


def test_planned_protocol_keeps_forecasts_and_unreviewed_quality_gate(planned_protocol):
    assert validate_protocol(planned_protocol) == planned_protocol
    unknown = deepcopy(planned_protocol)
    # Valid zero review coverage is still a valid protocol input: dispatch is
    # responsible for retaining infeasibility, rather than inventing quality.
    unknown["forecasts"]["account"] = []
    assert seal_protocol(unknown)["forecasts"]["account"] == []


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(profiles=None),
    lambda p: p["forecasts"]["account"][0].update(workflow_fingerprint="a" * 64),
    lambda p: p["forecasts"]["account"][0].update(profile_fingerprint="a" * 64),
    lambda p: p["forecasts"]["account"][0].update(workload_version="other-v1"),
    lambda p: p["forecasts"]["account"].append(p["forecasts"]["account"][0]),
    lambda p: p["arms"][1].update(custom_weights={"latency": 1}),
    lambda p: p["arms"][1].update(policy="custom", custom_weights=dict.fromkeys(
        ("latency", "accuracy", "cost", "energy", "carbon"), 0.)),
    lambda p: p["arms"][1]["allowed_pairs"]["draft"].append({"deployment_id": "local", "model_id": "small"}),
    lambda p: p["arms"][1].update(start_offsets_seconds=[0, True]),
])
def test_planned_bindings_stay_strict_after_rehash(planned_protocol, mutate):
    value = deepcopy(planned_protocol)
    mutate(value)
    with pytest.raises(ValueError):
        seal_protocol(value)


def test_valid_custom_policy_uses_all_five_frozen_weights(planned_protocol):
    value = deepcopy(planned_protocol)
    value["arms"][1].update(policy="custom", custom_weights={"latency": .1, "accuracy": .1,
        "cost": .1, "energy": .3, "carbon": .4})
    assert seal_protocol(value)["arms"][1]["policy"] == "custom"


def calibrated_arm(protocol, *, metric="latency"):
    from workflow.planning_constraints import calibrated_fixed_arm
    kb = decode_knowledge_base(protocol["knowledge_base"])
    preview = build_workflow_request(protocol["dataset"]["cases"][0]["ticket"], workflow_id="preview",
        arrived_at=datetime.fromisoformat(protocol["created_at"]), kb=kb,
        response_budget_seconds=protocol["response_seconds"], completion_budget_seconds=protocol["completion_seconds"],
        enable_thinking=False)
    return calibrated_fixed_arm(protocol["profiles"], DeploymentRegistry.from_dict(protocol["registry"]), preview,
        arm_id="calibrated", metric=metric, quality_floor=protocol["quality_floor"])


@pytest.mark.parametrize("metric", ["latency", "cost", "energy"])
def test_calibrated_baseline_attests_exact_frozen_selector(planned_protocol, metric):
    arm = calibrated_arm(planned_protocol, metric=metric)
    value = seal_protocol({**planned_protocol, "arms": [*planned_protocol["arms"], arm]})
    assert value["arms"][-1] == arm


@pytest.mark.parametrize("source", [
    "calibration-median-accuracy-v1:unknown", "calibration-median-latency-v2:unknown",
    "calibration-median-latency-v1:" + "a" * 64 + ":quality-floor=0.8",
    "calibration-median-latency-v1:{fingerprint}:quality-floor=0.7",
    "calibration-median-latency-v1:{fingerprint}:quality-floor=0.8000000000000001",
    "calibration-median-latency-v1:{fingerprint}:quality-floor=nan",
])
def test_falsely_labeled_calibration_baseline_cannot_be_rehashed_into_protocol(planned_protocol, source):
    arm = calibrated_arm(planned_protocol)
    arm["selection_source"] = source.replace("{fingerprint}", planned_protocol["profiles"]["fingerprint"])
    with pytest.raises(ValueError, match="baseline"):
        seal_protocol({**planned_protocol, "arms": [*planned_protocol["arms"], arm]})


def test_calibration_attestation_requires_profiles_and_same_quality_floor(protocol, planned_protocol):
    arm = deepcopy(protocol["arms"][0])
    arm["selection_source"] = "calibration-median-latency-v1:" + "a" * 64 + ":quality-floor=0.8"
    with pytest.raises(ValueError, match="baseline"):
        seal_protocol({**protocol, "arms": [arm, protocol["arms"][1]]})
    arm = calibrated_arm(planned_protocol)
    with pytest.raises(ValueError, match="quality floor"):
        seal_protocol({**planned_protocol, "arms": [*planned_protocol["arms"], arm], "quality_floor": .85})


def test_calibration_attestation_cannot_choose_an_arbitrary_supported_pair(planned_protocol):
    value = deepcopy(planned_protocol)
    registry = DeploymentRegistry.from_dict(value["registry"])
    original = next(iter(registry))
    other = replace(original, deployment_id="second", endpoint="http://second-fixture.invalid:11434")
    value["registry"] = DeploymentRegistry((original, other)).to_dict()
    arm = calibrated_arm(value)
    arm["allowed_pairs"]["draft"] = [{"deployment_id": "second", "model_id": "small"}]
    with pytest.raises(ValueError, match="baseline"):
        seal_protocol({**value, "arms": [*value["arms"], arm]})


def test_planned_snapshot_binds_every_frozen_input_and_constraint(planned_protocol, tmp_path):
    from workflow.planned_worker import submit_planned
    from workflow.planning_records import decode_carbon
    from workflow.queue_store import QueueStore
    from shared.scheduling import WorkloadSizeForecast

    protocol = planned_protocol
    trial = next(item for item in schedule_trials(protocol) if item["arm_id"] == "planned")
    arm = next(item for item in protocol["arms"] if item["arm_id"] == "planned")
    registry = DeploymentRegistry.from_dict(protocol["registry"])
    kb = decode_knowledge_base(protocol["knowledge_base"])
    instant = datetime.fromisoformat(protocol["created_at"])
    class Clock:
        def now(self):
            return instant
    def probe(deployment, **settings):
        return DeploymentHealth(deployment.deployment_id, instant, True, tuple(deployment.models), "offline-test")
    store = QueueStore(tmp_path / "planned.sqlite3")
    try:
        submit_planned(store, registry, kb, protocol["dataset"]["cases"][0]["ticket"], protocol["profiles"],
            forecasts=[WorkloadSizeForecast.from_dict(item) for item in protocol["forecasts"]["account"]],
            carbon_provider=decode_carbon(protocol["carbon"]), policy=arm["policy"], clock=Clock(),
            workflow_id=trial["workflow_id"], response_seconds=protocol["response_seconds"],
            completion_seconds=protocol["completion_seconds"], timeout_seconds=protocol["timeout_seconds"],
            health_probe=probe, allowed_pairs=arm["allowed_pairs"],
            start_offsets_seconds=arm["start_offsets_seconds"], allow_summary_deferral=arm["allow_summary_deferral"],
            experiment_mode="simulation", **protocol["planning_controls"])
        snapshot = store.read(trial["workflow_id"])
        assert validate_trial_snapshot(protocol, trial, snapshot) == trial
        for key, replacement in (("allowed_pairs", None), ("quality_floor", .5), ("forecasts", []),
                                 ("start_offsets_seconds", [0]), ("replan_interval_seconds", 1.)):
            context = {**snapshot.planning_context, key: replacement}
            with pytest.raises(ValueError, match="context"):
                validate_trial_snapshot(protocol, trial, replace(snapshot, planning_context=context))
    finally:
        store.close()


def test_schedule_preserves_paired_blocks_and_has_stable_unique_identities(protocol):
    schedule = schedule_trials(protocol)
    assert schedule == schedule_trials(json.loads(json.dumps(protocol)))
    assert len(schedule) == 8 and len({trial["trial_id"] for trial in schedule}) == 8
    assert all(trial["trial_id"] == trial["workflow_id"] for trial in schedule)
    assert {(trial["case_id"], trial["repeat"], trial["arm_id"]) for trial in schedule} == {
        (case, repeat, arm) for case in ("account", "billing") for repeat in range(2) for arm in ("baseline", "small")}
    for offset in range(0, len(schedule), 2):
        block = schedule[offset:offset + 2]
        assert len({(trial["case_id"], trial["repeat"]) for trial in block}) == 1
        assert {trial["arm_id"] for trial in block} == {"baseline", "small"}
    other = schedule_trials(seal_protocol({**protocol, "seed": 43}))
    assert [(t["case_id"], t["repeat"], t["arm_id"]) for t in schedule] != [
        (t["case_id"], t["repeat"], t["arm_id"]) for t in other]


def test_exact_trial_snapshot_matches_protocol(protocol):
    trial = schedule_trials(protocol)[0]
    state = fixed_snapshot(protocol, trial)
    assert validate_trial_snapshot(protocol, trial, state) == trial
    assert validate_trial_snapshot(protocol, {**trial, "state": "submitted", "bundle": None}, state) == trial


@pytest.mark.parametrize("mutate", [
    lambda s: replace(s, request=replace(s.request, workflow_id="other")),
    lambda s: replace(s, request=replace(s.request, input_text="different actual ticket")),
    lambda s: replace(s, request=replace(s.request, response_deadline=s.request.response_deadline + timedelta(seconds=1))),
    lambda s: replace(s, request=replace(s.request, steps=tuple(replace(step, enable_thinking=None) for step in s.request.steps))),
    lambda s: replace(s, request=replace(s.request, steps=tuple(replace(step, allow_deferral=True)
        if step.step_id == "summarize" else step for step in s.request.steps))),
    lambda s: replace(s, configuration={**s.configuration, "timeout_seconds": 20.}),
    lambda s: replace(s, configuration={**s.configuration, "experiment_mode": "live"}),
    lambda s: replace(s, configuration={**s.configuration, "kb_fingerprint": "a" * 64}),
    lambda s: replace(s, configuration={**s.configuration, "policy_version": "joint-calibrated-v1"}),
    lambda s: replace(s, accounting_context=None),
    lambda s: replace(s, planning_context={}),
    lambda s: replace(s, plan_history=(replace(s.plan_history[0], model_id="excluded"), *s.plan_history[1:])),
    lambda s: replace(s, plan_history=(replace(s.plan_history[0], planned_start=NOW + timedelta(seconds=1)), *s.plan_history[1:])),
])
def test_incompatible_snapshot_cannot_join_frozen_trial(protocol, mutate):
    trial = schedule_trials(protocol)[0]
    state = fixed_snapshot(protocol, trial)
    with pytest.raises(ValueError):
        validate_trial_snapshot(protocol, trial, mutate(state))


def test_trial_identity_and_creation_time_are_bound(protocol):
    trial = schedule_trials(protocol)[0]
    state = fixed_snapshot(protocol, trial)
    with pytest.raises(ValueError):
        validate_trial_snapshot(protocol, {**trial, "repeat": 99}, state)
    with pytest.raises(ValueError):
        validate_trial_snapshot(protocol, {**trial, "repeat": bool(trial["repeat"])}, state)
    newer = seal_protocol({**protocol, "created_at": (NOW + timedelta(seconds=1)).isoformat()})
    newer_trial = schedule_trials(newer)[0]
    state = replace(state, request=replace(state.request, workflow_id=newer_trial["workflow_id"]))
    with pytest.raises(ValueError, match="precede"):
        validate_trial_snapshot(newer, newer_trial, state)


def test_protocol_import_and_schedule_are_transport_free(protocol, tmp_path):
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol), encoding="utf-8")
    script = """
import builtins, json, socket, sys, time
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'workflow.worker','workflow.planned_worker','workflow.dispatch','workflow.connected',
                'workflow.deployment_clients','workflow.model_clients','requests','codecarbon'}:
        raise AssertionError('forbidden transport import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
socket.socket = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
time.sleep = lambda *a, **k: (_ for _ in ()).throw(AssertionError('wait'))
from workflow.evaluation_protocol import schedule_trials
with open(sys.argv[1]) as handle:
    protocol = json.load(handle)
print(len(schedule_trials(protocol)))
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "8"
