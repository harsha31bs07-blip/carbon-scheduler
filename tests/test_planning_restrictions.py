"""Matched-evaluation restrictions narrow choices without inventing eligibility."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from data.carbon_sources import HistoricalCarbonTrace
from data.profiles import build_profiles
from data.quality_reviews import fingerprint
from scheduler.joint import CommittedAssignment
from shared.deployments import DeploymentHealth, DeploymentRegistry
from shared.scheduling import CarbonSample
from workflow.planned_worker import planning_context, submit_planned, work_planned_once
from workflow.planning import prepare_plan
from workflow.planning_constraints import calibrated_fixed_arm, validate_allowed_pairs
from workflow.planning_records import validate_context, validate_report
from workflow.queue_replay import export_workflow, load_bundle
from workflow.queue_store import QueueStore
from workflow.workload import STEP_ORDER

from test_planned_replay import planned_inputs
from test_planned_worker import Clock, probe, provider
from test_staged_planning import NOW, at, inputs


def pairs(deployment="local"):
    return {step: [{"deployment_id": deployment, "model_id": "small"}] for step in STEP_ORDER}


@pytest.fixture
def choices(planned_inputs, inputs):
    args, *_ = planned_inputs
    original = next(iter(args["registry"]))
    remote = replace(original, deployment_id="remote", endpoint="http://remote-fixture.invalid:11434")
    registry = DeploymentRegistry((original, remote))
    records = [{**record, "trial_id": deployment.deployment_id + "-" + record["trial_id"],
        "deployment_id": deployment.deployment_id, "deployment_fingerprint": deployment.config_fingerprint(),
        "quality_review_fingerprint": "a" * 64, "case_id": "test-case",
        "latency_seconds": 1. if deployment.deployment_id == "local" else 2.}
        for deployment in registry for record in inputs["_trials"]]
    profiles = build_profiles(records, calibration_id="test-restrictions", quality_policy=args["profiles"]["quality_policy"])
    forecasts = {key: replace(value, profile_fingerprint=profiles["fingerprint"])
                 for key, value in args["forecasts"].items()}
    health = {deployment.deployment_id: DeploymentHealth(deployment.deployment_id, NOW, True,
        ("small",), "restriction-test-fixture") for deployment in registry}
    return {**args, "profiles": profiles, "registry": registry, "forecasts": forecasts, "health": health}


def context_and_config(args, allowed):
    context = planning_context(args["profiles"], args["kb"], forecasts=args["forecasts"],
        carbon_provider=args["carbon_provider"], minimum_improvement=args.get("minimum_improvement", 0.), allowed_pairs=allowed)
    from workflow.connected import workload_fingerprint
    configuration = {"registry_snapshot": args["registry"].to_dict(),
        "registry_fingerprint": args["registry"].config_fingerprint(),
        "kb_fingerprint": args["kb"].fingerprint(),
        "workload_fingerprint": workload_fingerprint(args["request"], args["kb"]),
        "worker_version": "sqlite-planned-worker-v1", "experiment_mode": "live",
        "calibration_id": args["profiles"]["calibration_id"]}
    return context, configuration


def seal(value):
    value["fingerprint"] = fingerprint({key: item for key, item in value.items() if key != "fingerprint"})
    return value


def test_pair_constraints_are_independent_snapshots_with_legacy_none(choices):
    allowed = pairs()
    validated = validate_allowed_pairs(allowed, choices["registry"])
    assert validated == allowed and validated is not allowed
    validated["draft"][0]["model_id"] = "changed-only-copy"
    assert allowed["draft"][0]["model_id"] == "small"
    assert validate_allowed_pairs(None, choices["registry"]) is None


@pytest.mark.parametrize("mutate", [
    lambda value: value.pop("draft"),
    lambda value: value.update(extra=[]),
    lambda value: value.update(classify=[]),
    lambda value: value.update(classify=[True]),
    lambda value: value["classify"].append(value["classify"][0]),
    lambda value: value["classify"][0].update(model_id="unknown"),
    lambda value: value["classify"][0].update(deployment_id="unknown"),
    lambda value: value["classify"][0].update(endpoint="http://unsafe.invalid"),
])
def test_invalid_pair_constraints_fail_before_preparation(choices, mutate):
    allowed = pairs()
    mutate(allowed)
    with pytest.raises(ValueError):
        prepare_plan(**choices, allowed_pairs=allowed)


def test_restrictions_filter_before_dependency_start_closure_and_survive_validation(choices):
    allowed = pairs()
    allowed["classify"] = pairs("remote")["classify"]
    prepared = prepare_plan(**choices, allowed_pairs=allowed)
    assert prepared.result.winner is not None
    selected = {item.candidate.step_id: item.candidate for item in prepared.result.winner.assignments}
    assert selected["classify"].deployment_id == "remote"
    assert selected["retrieve"].deployment_id == "local"
    assert selected["retrieve"].planned_start == at(2)
    exclusions = prepared.report["mode_exclusions"]
    assert exclusions and set(exclusions.values()) == {"pair-not-permitted"}
    assert all(len(diagnostic["candidates"]) >= 2 for diagnostic in prepared.report["candidate_diagnostics"])
    context, config = context_and_config(choices, allowed)
    validate_context(context, choices["request"], config)
    assert validate_report(prepared.report, choices["request"], config, context) == prepared.report
    assert validate_report(prepared.report) == prepared.report


@pytest.mark.parametrize("mutation", ["strip", "relax", "exclusions", "none"])
def test_rehashed_restriction_tampering_cannot_weaken_a_durable_arm(choices, mutation):
    allowed = pairs("remote")
    prepared = prepare_plan(**choices, allowed_pairs=allowed)
    context, config = context_and_config(choices, allowed)
    changed = deepcopy(prepared.report)
    if mutation == "strip":
        changed.pop("allowed_pairs")
    elif mutation == "relax":
        changed["allowed_pairs"] = pairs()
    elif mutation == "exclusions":
        changed["mode_exclusions"] = {}
    else:
        changed["allowed_pairs"] = None
    with pytest.raises(ValueError):
        validate_report(seal(changed), choices["request"], config, context)


def test_committed_work_cannot_move_outside_restrictions(choices):
    first = prepare_plan(**choices)
    candidate = next(item.candidate for item in first.result.winner.assignments if item.candidate.step_id == "classify")
    fact = CommittedAssignment(candidate, NOW, at(1))
    with pytest.raises(ValueError, match="Committed"):
        prepare_plan(**{**choices, "now": at(1)}, outputs={"classify": "account_access"},
            fixed_assignments=(fact,), allowed_pairs=pairs("remote"))


def test_live_simulated_metrics_exclusion_precedes_pair_restriction(choices):
    sample = CarbonSample("simulated-grid", at(-1), NOW, at(1000), 500., "explicit-simulation", "simulated")
    args = {**choices, "carbon_provider": HistoricalCarbonTrace("simulation", (sample,), timedelta(seconds=1000))}
    prepared = prepare_plan(**args, allowed_pairs=pairs())
    assert prepared.result.winner is None
    assert set(prepared.report["mode_exclusions"].values()) == {"simulation-only-metrics"}
    simulated = prepare_plan(**{**args, "experiment_mode": "simulation"}, allowed_pairs=pairs())
    assert simulated.result.winner is not None
    assert set(simulated.report["mode_exclusions"].values()) == {"pair-not-permitted"}
    context, config = context_and_config(args, pairs())
    config["experiment_mode"] = "simulation"
    assert validate_report(simulated.report, args["request"], config, context) == simulated.report
    config["experiment_mode"] = "live"
    with pytest.raises(ValueError, match="mode"):
        validate_report(simulated.report, args["request"], config, context)


def test_no_time_shifting_enforces_earliest_summary_slot(choices, tmp_path):
    samples = (CarbonSample("simulated-grid", at(-1), NOW, at(60), 800., "test-early", "estimated"),
               CarbonSample("simulated-grid", at(-1), at(60), at(1000), 100., "test-late", "estimated"))
    args = {**choices, "carbon_provider": HistoricalCarbonTrace("test-delay", samples, timedelta(seconds=1000))}
    for allow, expected in ((True, at(60)), (False, at(3))):
        store, clock = QueueStore(tmp_path / (str(allow) + ".sqlite3")), Clock()
        try:
            identity = submit_planned(store, args["registry"], args["kb"], args["request"].input_text,
                args["profiles"], forecasts=args["forecasts"], carbon_provider=args["carbon_provider"],
                clock=clock, policy="eco", allowed_pairs=pairs(), start_offsets_seconds=(0, 60),
                allow_summary_deferral=allow, health_probe=probe(clock))
            state = store.read(identity)
            assert state.request.steps[-1].allow_deferral is allow
            assert state.request.steps[-1].earliest_start == NOW
            assert state.jobs[-1].candidate.planned_start == expected
            assert state.planning_context["allowed_pairs"] == pairs()
            replayed = load_bundle(export_workflow(store.path, identity, tmp_path / ("bundle-" + str(allow))))
            assert replayed == state
        finally:
            store.close()


def test_simulation_submission_and_worker_require_explicit_fixtures(choices, tmp_path):
    store, clock = QueueStore(tmp_path / "simulation.sqlite3"), Clock()
    try:
        with pytest.raises(ValueError, match="health"):
            submit_planned(store, choices["registry"], choices["kb"], choices["request"].input_text,
                choices["profiles"], experiment_mode="simulation", clock=clock)
        identity = submit_planned(store, choices["registry"], choices["kb"], choices["request"].input_text,
            choices["profiles"], forecasts=choices["forecasts"], carbon_provider=choices["carbon_provider"],
            allowed_pairs=pairs(), experiment_mode="simulation", clock=clock, health_probe=probe(clock))
        state = store.read(identity)
        assert state.configuration["experiment_mode"] == "simulation"
        assert state.decisions[0]["report"]["experiment_mode"] == "simulation"
        for kwargs in ({}, {"dispatcher": provider(clock, [])}, {"health_probe": probe(clock)}):
            with pytest.raises(ValueError, match="fixtures"):
                work_planned_once(store, worker_id="simulation-worker", clock=clock, heartbeat=False, **kwargs)
        assert not store.read(identity).attempts
        calls = []
        result = work_planned_once(store, worker_id="simulation-worker", clock=clock, heartbeat=False,
            dispatcher=provider(clock, calls), health_probe=probe(clock))
        assert len(calls) == 1
        assert result["accounting_status"] == "recorded"
        assert store.read(identity).decisions[-1]["report"]["experiment_mode"] == "simulation"
    finally:
        store.close()


def test_calibration_selected_fixed_arm_uses_only_frozen_training_medians(choices):
    arm = calibrated_fixed_arm(choices["profiles"], choices["registry"], choices["request"])
    assert arm["kind"] == arm["policy"] == "fixed"
    assert arm["allowed_pairs"] == pairs()
    assert arm["allow_summary_deferral"] is False and arm["start_offsets_seconds"] == [0]
    assert choices["profiles"]["fingerprint"] in arm["selection_source"]
    # A different real input has no effect on this fixed, pre-evaluation choice.
    request = replace(choices["request"], input_text="A completely different support ticket.")
    assert calibrated_fixed_arm(choices["profiles"], choices["registry"], request) == arm


@pytest.mark.parametrize("changes", [
    {"quality_floor": True}, {"quality_floor": float("nan")}, {"quality_floor": 1.01},
    {"quality_floor": .99}, {"metric": "carbon"}, {"arm_id": "not an identifier"},
])
def test_calibration_selection_rejects_unsupported_or_ineligible_baselines(choices, changes):
    with pytest.raises(ValueError):
        calibrated_fixed_arm(choices["profiles"], choices["registry"], choices["request"], **changes)


def test_calibration_selection_cannot_ignore_review_coverage_or_settings(choices, inputs):
    policy = {**choices["profiles"]["quality_policy"], "minimum_reviewed_trials": 100}
    records = [{**record, "quality_review_fingerprint": "a" * 64, "case_id": "test-case"}
               for record in inputs["_trials"]]
    unreviewed = build_profiles(records, calibration_id="test-insufficient", quality_policy=policy)
    with pytest.raises(ValueError, match="quality-eligible"):
        calibrated_fixed_arm(unreviewed, choices["registry"], choices["request"])
    changed = replace(choices["request"], steps=tuple(replace(step, enable_thinking=True)
                                                     for step in choices["request"].steps))
    with pytest.raises(ValueError, match="quality-eligible"):
        calibrated_fixed_arm(choices["profiles"], choices["registry"], changed)
