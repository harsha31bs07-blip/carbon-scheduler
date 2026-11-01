"""Deterministic M4 scenarios; two destinations here are simulated fixtures."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import itertools
import math

import pytest

from scheduler.joint import (
    CommittedAssignment, NormalizationSnapshot, PlanResult, Reservation,
    plan_workflow, replan_workflow,
)
from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry
from shared.scheduling import MetricEstimate, PlannedCandidate, StepEstimates, StepRequest, WorkflowRequest


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
METRICS = ("latency", "accuracy", "cost", "energy", "carbon")


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def step(key="a", *, dependencies=(), deferrable=False, response=True, earliest=0, deadline=100):
    return StepRequest(key, "simulated-task", "simulated input", None, 100,
                       at(earliest), at(deadline), dependencies, deferrable, response)


def request(*steps, response_deadline=100, completion_deadline=200, policy="test"):
    return WorkflowRequest("simulated-workflow", "simulated input", NOW, policy,
                           at(response_deadline), at(completion_deadline), "simulation-v1",
                           "constraints-v1", steps or (step(),))


def candidate(key="a1", *, step_id="a", model="small", deployment="one", start=0,
              latency=2, accuracy=.8, cost=1, energy=.01, carbon=1, region=None,
              carbon_unit="gCO2e"):
    values = dict(latency=latency, accuracy=accuracy, cost=cost, energy=energy, carbon=carbon)
    units = dict(latency="s", accuracy="ratio", cost="USD", energy="kWh", carbon=carbon_unit)
    estimates = StepEstimates(**{
        name: MetricEstimate(value, units[name], "simulated" if value is not None else "unknown",
                             "simulated-fixture" if value is not None else None)
        for name, value in values.items()
    })
    return PlannedCandidate(key, step_id, model, deployment, at(start), estimates, region)


def weights(metric="latency"):
    return {key: float(key == metric) for key in METRICS}


def selected(result):
    assert result.winner is not None
    return tuple(item.candidate.candidate_id for item in result.winner.assignments)


def test_model_choice_changes_with_quality_weight_and_uses_same_reference():
    req = request()
    options = {"a": [candidate("fast", model="small", latency=2, accuracy=.7),
                     candidate("accurate", model="large", latency=8, accuracy=.95)]}
    fast = plan_workflow(req, options, weights(), now=NOW)
    accurate = plan_workflow(replace(req, policy="quality"), options, weights("accuracy"),
                             now=NOW, normalization=fast.normalization)
    assert selected(fast) == ("fast",)
    assert selected(accurate) == ("accurate",)
    assert fast.normalization == accurate.normalization
    assert fast.normalization.bounds["latency"] == (2, 8)
    assert accurate.winner.weighted_contributions["accuracy"] == -1
    assert "not measured end-to-end" in accurate.winner.metrics.quality_semantics
    assert any("simulated" in text for text in accurate.notes)


def test_run_identity_does_not_change_matched_policy_normalization():
    req = request()
    options = {"a": [candidate("fast", latency=2), candidate("clean", latency=5, carbon=.1)]}
    first = plan_workflow(req, options, weights(), now=NOW)
    second = plan_workflow(replace(req, workflow_id="a-different-run", policy="eco"), options,
                           weights("carbon"), now=NOW, normalization=first.normalization)
    assert first.workflow_id != second.workflow_id
    assert first.normalization == second.normalization
    assert selected(first) == ("fast",)
    assert selected(second) == ("clean",)


def test_location_choice_independent_of_model_changes_with_carbon_weight():
    options = {"a": [candidate("near", deployment="one", latency=2, carbon=10),
                     candidate("clean", deployment="two", latency=3, carbon=1)]}
    fast = plan_workflow(request(), options, weights(), now=NOW)
    clean = plan_workflow(request(), options, weights("carbon"), now=NOW)
    assert selected(fast) == ("near",)
    assert selected(clean) == ("clean",)
    assert fast.winner.assignments[0].candidate.model_id == clean.winner.assignments[0].candidate.model_id
    assert fast.winner.assignments[0].candidate.deployment_id != clean.winner.assignments[0].candidate.deployment_id


def test_absolute_time_choice_independent_of_model_and_location():
    req = request(step(deferrable=True))
    options = {"a": [candidate("now", carbon=10), candidate("later", start=10, carbon=1)]}
    fast = plan_workflow(req, options, weights(), now=NOW)
    clean = plan_workflow(req, options, weights("carbon"), now=NOW)
    assert selected(fast) == ("now",)
    assert selected(clean) == ("later",)
    assert clean.winner.metrics.latency == 12
    assert clean.winner.completion_at == at(12)


def test_urgent_step_cannot_deliberately_wait_for_lower_carbon():
    options = {"a": [candidate("now", carbon=10), candidate("later", start=10, carbon=1)]}
    result = plan_workflow(request(), options, weights("carbon"), now=NOW)
    assert selected(result) == ("now",)
    assert result.rejection_counts["deferral-not-permitted"] == 1


def test_deadline_excludes_deferral_even_when_allowed():
    options = {"a": [candidate("now", carbon=10), candidate("late", start=9, latency=2, carbon=.1)]}
    result = plan_workflow(request(step(deferrable=True, deadline=10), response_deadline=10),
                           options, weights("carbon"), now=NOW)
    assert selected(result) == ("now",)
    assert result.rejected_candidates["late"] == ("step-deadline",)


def test_dependencies_and_remaining_downstream_duration_eliminate_greedy_choice():
    req = request(step("a", deadline=10), step("b", dependencies=("a",), deadline=10),
                  response_deadline=10)
    options = {"a": [candidate("cheap-slow", latency=8, cost=.1), candidate("fast", latency=3, cost=2)],
               "b": [candidate("b-early", step_id="b", start=3, latency=5),
                     candidate("b-late", step_id="b", start=8, latency=5)]}
    result = plan_workflow(req, options, weights("cost"), now=NOW)
    assert selected(result) == ("fast", "b-early")
    assert result.winner.completion_at == at(8)
    assert result.rejection_counts["dependency-or-earliest-start"] >= 1


def test_response_and_background_completion_deadlines_are_distinct():
    req = request(step("a", deadline=5),
                  step("b", dependencies=("a",), deferrable=True, response=False, deadline=20),
                  response_deadline=5, completion_deadline=20)
    result = plan_workflow(req, {"a": [candidate(latency=4)],
                                 "b": [candidate("background", step_id="b", start=10, latency=7)]},
                           weights(), now=NOW)
    assert result.winner.response_at == at(4)
    assert result.winner.completion_at == at(17)
    assert result.winner.metrics.latency == 17


def test_branching_critical_path_includes_waits_but_does_not_sum_parallel_durations():
    req = request(step("a"), step("b"), step("c", dependencies=("a", "b")))
    result = plan_workflow(req, {
        "a": [candidate(latency=5, deployment="one", accuracy=.6)],
        "b": [candidate("b", step_id="b", latency=7, deployment="two", accuracy=.9)],
        "c": [candidate("c", step_id="c", latency=2, start=7, deployment="one", accuracy=.9)],
    }, weights(), now=NOW)
    assert result.winner.metrics.latency == 9  # Not 5 + 7 + 2.
    assert result.winner.metrics.cost == 3
    assert result.winner.metrics.energy == pytest.approx(.03)
    assert result.winner.metrics.accuracy == pytest.approx(.8)


def test_capacity_conflict_is_explicit_and_queue_wait_is_allowed_for_urgent_steps():
    req = request(step("a"), step("b"))
    options = {"a": [candidate(latency=5)],
               "b": [candidate("parallel", step_id="b", latency=7),
                     candidate("queued", step_id="b", start=5, latency=7)]}
    one = plan_workflow(req, options, weights(), now=NOW)
    two = plan_workflow(req, options, weights(), now=NOW, capacities={"one": 2})
    assert selected(one) == ("a1", "queued")
    assert one.winner.metrics.latency == 12
    assert one.rejection_counts["deployment-capacity"] == 1
    assert selected(two) == ("a1", "parallel")
    assert two.winner.metrics.latency == 7


@pytest.mark.parametrize("slots,capacity,expected", [(1, 1, "queued"), (1, 2, "now"), (2, 2, "queued")])
def test_existing_capacity_reservations_have_half_open_boundaries(slots, capacity, expected):
    result = plan_workflow(request(), {"a": [candidate("now"), candidate("queued", start=4)]},
                           weights(), now=NOW, capacities={"one": capacity},
                           reservations=(Reservation("one", at(0), at(4), slots),))
    assert selected(result) == (expected,)
    assert result.winner.assignments[0].finish == at(2 if expected == "now" else 6)


def test_short_idle_gap_before_reservation_is_not_a_feasible_nonpreemptive_slot():
    result = plan_workflow(request(), {"a": [candidate("fits", start=7, latency=5)]},
                           weights(), now=NOW,
                           reservations=(Reservation("one", at(3), at(7)),))
    assert selected(result) == ("fits",)


def test_unnecessary_capacity_delay_is_rejected():
    result = plan_workflow(request(), {"a": [candidate("too-late", start=8)]}, weights(), now=NOW,
                           reservations=(Reservation("one", at(0), at(4)),))
    assert result.winner is None
    assert result.rejection_counts["deferral-not-permitted"] == 1


def test_actual_elapsed_time_counts_against_arrival_to_completion():
    result = plan_workflow(request(), {"a": [candidate("resume", start=5)]}, weights(), now=at(5))
    assert result.winner.metrics.latency == 7


@pytest.mark.parametrize("missing", METRICS)
def test_any_unknown_metric_is_infeasible_even_when_its_weight_is_zero(missing):
    result = plan_workflow(request(), {"a": [candidate(**{missing: None})]}, weights(), now=NOW)
    assert result.winner is None
    assert result.normalization is None
    assert f"unknown-{missing}" in result.rejected_candidates["a1"]
    assert result.feasible_count == result.examined_count == result.total_combinations == 0
    assert not result.globally_optimal_within_candidates


def test_quality_floor_is_hard_constraint_and_unknown_is_not_zero_quality():
    result = plan_workflow(request(), {"a": [candidate("poor", accuracy=.5),
                                              candidate("good", accuracy=.85, latency=5)]},
                           weights(), now=NOW, quality_floor={"a": .8})
    assert selected(result) == ("good",)
    assert result.rejected_candidates["poor"] == ("minimum-quality",)


def test_empty_and_outage_pools_return_explicit_infeasibility():
    empty = plan_workflow(request(), {}, weights(), now=NOW)
    assert empty.winner is None and empty.rejection_counts == {"empty-step-pool": 1}
    health = {"one": DeploymentHealth("one", NOW, False, (), "simulated-health")}
    outage = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW, health=health)
    assert outage.winner is None
    assert outage.rejected_candidates["a1"] == ("deployment-unavailable-or-unknown",)


@pytest.mark.parametrize("checked,models,expected", [
    (-301, ("small",), "deployment-health-stale-or-future"),
    (1, ("small",), "deployment-health-stale-or-future"),
    (0, ("other",), "model-unavailable"),
])
def test_availability_observation_requires_freshness_and_capability(checked, models, expected):
    health = {"one": DeploymentHealth("one", at(checked), True, models, "simulated-health")}
    result = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW, health=health)
    assert result.rejected_candidates["a1"] == (expected,)


def registry(concurrency=1, region="simulated-region"):
    return DeploymentRegistry([Deployment("one", "ollama", "http://simulated.invalid:11434",
                                          {"small": "small:1"}, region=region,
                                          location_source="simulated-fixture" if region else None,
                                          max_concurrency=concurrency)])


def test_registry_separates_supported_model_from_physical_region():
    health = {"one": DeploymentHealth("one", NOW, True, ("small",), "simulated-health")}
    options = {"a": [candidate("good", region="simulated-region"),
                     candidate("wrong-region", region="other"),
                     candidate("unknown-region"), candidate("wrong-model", model="large"),
                     candidate("wrong-deployment", deployment="other")]}
    result = plan_workflow(request(), options, weights(), now=NOW, registry=registry(), health=health)
    assert selected(result) == ("good",)
    assert result.rejected_candidates["wrong-region"] == ("deployment-region-mismatch",)
    assert result.rejected_candidates["unknown-region"] == ("deployment-region-mismatch",)
    assert "unsupported-deployment-model" in result.rejected_candidates["wrong-model"]
    assert "unsupported-deployment-model" in result.rejected_candidates["wrong-deployment"]


def test_unknown_geography_remains_eligible_but_does_not_become_known():
    result = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW, registry=registry(region=None))
    assert selected(result) == ("a1",)
    assert result.winner.assignments[0].candidate.region is None


def test_carbon_co2_and_co2e_are_never_compared_or_summed_together():
    options = {"a": [candidate("co2", carbon_unit="gCO2"),
                     candidate("co2e", carbon_unit="gCO2e")]}
    with pytest.raises(ValueError, match="Mixed carbon scopes"):
        plan_workflow(request(), options, weights("carbon"), now=NOW)
    result = plan_workflow(request(), options, weights("carbon"), now=NOW, carbon_unit="gCO2")
    assert selected(result) == ("co2",)
    assert result.winner.metrics.carbon_unit == "gCO2"
    assert result.rejected_candidates["co2e"] == ("carbon-scope",)


def test_ties_and_enumeration_are_deterministic_under_input_order():
    a, b = candidate("a"), candidate("b")
    first = plan_workflow(request(), {"a": [b, a]}, weights(), now=NOW)
    second = plan_workflow(request(), {"a": [a, b]}, weights(), now=NOW)
    assert selected(first) == selected(second) == ("a",)
    assert first.normalization == second.normalization
    assert first.winner.normalized_metrics == {key: 0 for key in METRICS}


def test_search_limit_retains_counts_bounded_normalization_and_no_global_claim():
    options = {"a": [candidate("a-slow", latency=10), candidate("b-fast", latency=1)]}
    limited = plan_workflow(request(), options, weights(), now=NOW, search_limit=1)
    full = plan_workflow(request(), options, weights(), now=NOW)
    assert selected(limited) == ("a-slow",)
    assert limited.truncated and not limited.globally_optimal_within_candidates
    assert limited.examined_count == limited.feasible_count == 1
    assert limited.total_combinations == 2
    assert not limited.normalization.complete_reference
    assert selected(full) == ("b-fast",)
    assert full.globally_optimal_within_candidates and not full.truncated
    assert full.examined_count == full.feasible_count == 2
    assert full.normalization.reference_id != limited.normalization.reference_id
    assert math.isfinite(limited.planner_seconds) and limited.planner_seconds >= 0


def test_search_budget_counts_infeasible_complete_assignments_too():
    req = request(step("a"), step("b", dependencies=("a",)))
    options = {"a": [candidate("a", latency=3)],
               "b": [candidate("a-invalid", step_id="b", start=0),
                     candidate("b-valid", step_id="b", start=3)]}
    result = plan_workflow(req, options, weights(), now=NOW, search_limit=1)
    assert result.winner is None
    assert result.examined_count == 1 and result.feasible_count == 0
    assert result.truncated and result.total_combinations == 2


def test_reference_cannot_be_reused_after_changed_constraints_or_candidate_measurements():
    req = request()
    options = {"a": [candidate()]}
    result = plan_workflow(req, options, weights(), now=NOW)
    with pytest.raises(ValueError, match="Normalization snapshot"):
        plan_workflow(req, {"a": [candidate(latency=3)]}, weights(), now=NOW,
                      normalization=result.normalization)
    with pytest.raises(ValueError, match="Normalization snapshot"):
        plan_workflow(replace(req, constraint_version="changed"), options, weights(), now=NOW,
                      normalization=result.normalization)


def test_replanning_preserves_dispatched_identity_and_deducts_actual_overrun():
    req = request(step("a", deadline=20), step("b", dependencies=("a",), deadline=12), response_deadline=20)
    initial = candidate("fixed", latency=3)
    committed = CommittedAssignment(initial, at(0), at(8))
    result = replan_workflow(req, {
        "b": [candidate("old-too-early", step_id="b", start=3, latency=3),
              candidate("fits", step_id="b", start=8, latency=3),
              candidate("too-slow", step_id="b", start=8, latency=5)],
    }, weights(), now=at(8), fixed_assignments=(committed,))
    assert selected(result) == ("fixed", "fits")
    assert result.winner.assignments[0].candidate is initial
    assert result.winner.assignments[0].start == at(0)
    assert result.winner.assignments[0].finish == at(8)
    assert result.winner.assignments[0].committed
    assert result.winner.metrics.latency == 11
    assert result.rejected_candidates["old-too-early"] == ("past-or-before-earliest-start",)
    assert result.rejected_candidates["too-slow"] == ("step-deadline",)


def test_completed_endpoint_outage_does_not_erase_already_performed_work():
    req = request(step("a"), step("b", dependencies=("a",)))
    fixed = CommittedAssignment(candidate("done", latency=3), at(0), at(3))
    health = {"one": DeploymentHealth("one", at(3), False, (), "simulated-health"),
              "two": DeploymentHealth("two", at(3), True, ("small",), "simulated-health")}
    result = replan_workflow(req, {"b": [candidate("next", step_id="b", deployment="two", start=3)]},
                             weights(), now=at(3), fixed_assignments=(fixed,), health=health)
    assert selected(result) == ("done", "next")


def test_running_assignment_requires_explicit_finish_and_blocks_capacity():
    req = request(step("a"), step("b"))
    fixed = CommittedAssignment(candidate("running", latency=2), at(0), at(8), "running")
    result = replan_workflow(req, {"b": [candidate("blocked", step_id="b", start=5),
                                         candidate("queued", step_id="b", start=8)]},
                             weights(), now=at(5), fixed_assignments=(fixed,))
    assert selected(result) == ("running", "queued")
    assert result.winner.completion_at == at(10)


def test_replanning_refuses_to_replace_a_committed_model():
    fixed = CommittedAssignment(candidate("done"), NOW, at(2))
    with pytest.raises(ValueError, match="already-dispatched"):
        replan_workflow(request(), {"a": [candidate("replacement", model="large", start=2)]},
                        weights(), now=at(2), fixed_assignments=(fixed,))


def test_replanning_threshold_retains_feasible_incumbent_but_never_invalid_one():
    req = request(step(deferrable=True))
    a = candidate("a", latency=5, cost=1)
    b = candidate("b", latency=4, cost=1.1)
    options = {"a": [a, b]}
    prior = plan_workflow(req, options, weights("cost"), now=NOW).winner
    current_weights = {**weights(), "latency": .55, "cost": .45}
    retained = replan_workflow(req, options, current_weights, now=NOW,
                               fixed_assignments=(), previous_plan=prior, minimum_improvement=.2)
    changed = replan_workflow(req, options, current_weights, now=NOW,
                              fixed_assignments=(), previous_plan=prior, minimum_improvement=.01)
    assert selected(retained) == ("a",)
    assert selected(changed) == ("b",)
    assert not retained.globally_optimal_within_candidates
    assert any("incumbent" in note for note in retained.notes)
    unavailable_old = replan_workflow(req, {"a": [b]}, current_weights, now=NOW,
                                      fixed_assignments=(), previous_plan=prior, minimum_improvement=100)
    assert selected(unavailable_old) == ("b",)


def test_truncated_replan_cannot_promote_unexamined_incumbent_inside_metric_bounds():
    req = request(step(deferrable=True))
    options = {"a": [candidate("a", latency=1, cost=10),
                     candidate("b", latency=10, cost=1),
                     candidate("z-compromise", latency=5, cost=5)]}
    balanced = {**weights(), "latency": .5, "cost": .5}
    prior = plan_workflow(req, options, balanced, now=NOW).winner
    assert prior.assignments[0].candidate.candidate_id == "z-compromise"
    bounded = replan_workflow(req, options, weights(), now=NOW, fixed_assignments=(),
                              previous_plan=prior, minimum_improvement=100, search_limit=2)
    assert selected(bounded) == ("a",)
    assert bounded.truncated and bounded.feasible_count == bounded.examined_count == 2


def test_frozen_late_work_returns_infeasible_without_rerunning_it():
    req = request(step(deadline=5), response_deadline=5)
    fixed = CommittedAssignment(candidate("late", latency=2), at(0), at(6))
    result = replan_workflow(req, {}, weights(), now=at(6), fixed_assignments=(fixed,))
    assert result.winner is None
    assert result.rejection_counts["step-deadline"] == 1
    assert result.rejection_counts["workflow-deadline"] == 1


def test_small_joint_problem_matches_an_independent_exhaustive_oracle():
    # This oracle deliberately does not call the planner's feasibility/scoring
    # helpers. It checks two serial tasks, one-slot destinations and a time grid.
    req = request(step("a", deferrable=True, deadline=12),
                  step("b", dependencies=("a",), deferrable=True, deadline=12), response_deadline=12)
    options = {
        "a": [candidate(f"a-{model}-{destination}-{start}", model=model, deployment=destination,
                         start=start, latency=duration, accuracy=quality, cost=cost,
                         carbon=carbon * (1 if start == 0 else .5))
              for model, duration, quality, cost in [("small", 2, .7, 1), ("large", 4, .95, 2)]
              for destination, carbon in [("one", 5), ("two", 2)] for start in (0, 2)],
        "b": [candidate(f"b-{model}-{destination}-{start}", step_id="b", model=model,
                         deployment=destination, start=start, latency=duration, accuracy=quality,
                         cost=cost, carbon=carbon * (1 if start == 2 else .5))
              for model, duration, quality, cost in [("small", 2, .7, 1), ("large", 4, .95, 2)]
              for destination, carbon in [("one", 5), ("two", 2)] for start in (2, 4, 6)],
    }
    quality_floor = .7
    feasible = []
    for a, b in itertools.product(options["a"], options["b"]):
        af = a.planned_start + timedelta(seconds=a.estimates.latency.value)
        bf = b.planned_start + timedelta(seconds=b.estimates.latency.value)
        if af > b.planned_start or bf > at(12):
            continue
        if min(a.estimates.accuracy.value, b.estimates.accuracy.value) < quality_floor:
            continue
        values = {
            "latency": (bf - NOW).total_seconds(),
            "accuracy": (a.estimates.accuracy.value + b.estimates.accuracy.value) / 2,
            "cost": a.estimates.cost.value + b.estimates.cost.value,
            "energy": a.estimates.energy.value + b.estimates.energy.value,
            "carbon": a.estimates.carbon.value + b.estimates.carbon.value,
        }
        feasible.append(((a.candidate_id, b.candidate_id), values))
    bounds = {metric: (min(values[metric] for _, values in feasible),
                       max(values[metric] for _, values in feasible)) for metric in METRICS}
    w = dict(latency=3, accuracy=2, cost=1, energy=0, carbon=4)
    def score(item):
        ids, values = item
        total = 0
        for metric, (low, high) in bounds.items():
            norm = 0 if high == low else (values[metric] - low) / (high - low)
            total += w[metric] / sum(w.values()) * norm * (-1 if metric == "accuracy" else 1)
        return total, ids
    expected = min(feasible, key=score)
    result = plan_workflow(req, options, w, now=NOW, quality_floor=quality_floor)
    assert result.feasible_count == len(feasible)
    assert selected(result) == expected[0]
    assert result.winner.score == pytest.approx(score(expected)[0])
    assert dict(result.normalization.bounds) == bounds
    assert result.globally_optimal_within_candidates


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "1"])
def test_invalid_weights_are_rejected(bad):
    invalid = weights()
    invalid["energy"] = bad
    with pytest.raises(ValueError):
        plan_workflow(request(), {"a": [candidate()]}, invalid, now=NOW)


@pytest.mark.parametrize("bad", [{}, {"latency": 1}, {key: 0 for key in METRICS},
                                   {**weights(), "unsupported": 1},
                                   {key: 1e308 for key in METRICS}])
def test_missing_zero_extra_or_overflowing_weights_are_rejected(bad):
    with pytest.raises(ValueError):
        plan_workflow(request(), {"a": [candidate()]}, bad, now=NOW)


@pytest.mark.parametrize("kwargs", [
    {"search_limit": 0}, {"search_limit": True}, {"capacities": {"one": 0}},
    {"quality_floor": float("nan")}, {"quality_floor": 1.1},
    {"quality_floor": {"unknown": .5}}, {"health_max_age_seconds": -1},
    {"carbon_unit": "tons"}, {"now": NOW.replace(tzinfo=None)}, {"now": at(-1)},
])
def test_invalid_planner_inputs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        plan_workflow(request(), {"a": [candidate()]}, weights(), **{"now": NOW, **kwargs})


def test_invalid_candidate_groups_and_registry_capacity_are_rejected():
    req = request(step("a"), step("b"))
    for options in ({"unknown": []}, {"a": [candidate(step_id="b")]},
                    {"a": [candidate()], "b": [candidate(step_id="b")]}):
        with pytest.raises(ValueError):
            plan_workflow(req, options, weights(), now=NOW)
    with pytest.raises(ValueError, match="exceed"):
        plan_workflow(request(), {"a": [candidate(region="simulated-region")]}, weights(),
                      now=NOW, registry=registry(), capacities={"one": 2})


def test_result_and_normalization_are_json_safe_typed_roundtrips():
    import json
    result = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW)
    payload = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    restored = PlanResult.from_dict(payload)
    assert restored == result
    assert isinstance(restored.normalization, NormalizationSnapshot)
    with pytest.raises(TypeError):
        result.weights["cost"] = 99
    with pytest.raises(TypeError):
        result.normalization.bounds["cost"] = (0, 99)


def test_joint_record_types_use_the_atomic_schema3_file_boundary(tmp_path):
    from workflow.scheduling_records import load_record, save_record
    result = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW)
    records = [result, result.winner, result.normalization, result.winner.metrics,
               result.winner.assignments[0], Reservation("one", NOW, at(3)),
               CommittedAssignment(candidate(), NOW, at(2))]
    for record in records:
        path = tmp_path / f"{type(record).__name__}.json"
        save_record(path, record)
        assert load_record(path) == record


@pytest.mark.parametrize("changes", [
    {"workflow_id": ""}, {"planner_version": "future-unknown"},
    {"weights": {"latency": 1}}, {"weights": {key: 0 for key in METRICS}},
    {"feasible_count": -1}, {"feasible_count": True}, {"feasible_count": 2},
    {"examined_count": False}, {"examined_count": 0}, {"total_combinations": -1},
    {"search_limit": 0}, {"search_limit": True}, {"truncated": 1}, {"truncated": True},
    {"globally_optimal_within_candidates": 1}, {"planner_seconds": -1},
    {"planner_seconds": True}, {"planner_seconds": float("nan")},
    {"winner": None}, {"normalization": None}, {"notes": []}, {"notes": [""]},
    {"rejected_candidates": {"x": []}}, {"rejected_candidates": {"x": ["reason", "reason"]}},
    {"rejected_candidates": {"x": [{"bad": "type"}]}},
    {"rejected_candidates": {"x": ["missing-from-counts"]}},
    {"rejection_counts": {"reason": True}}, {"rejection_counts": {"reason": -1}},
    {"api_key": "must-not-be-accepted"},
])
def test_persisted_plan_results_reject_malformed_counts_flags_and_explanations(tmp_path, changes):
    import json
    from workflow.scheduling_records import load_record
    result = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW)
    payload = {**result.to_dict(), **changes}
    path = tmp_path / "malformed-plan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_record(path)


@pytest.mark.parametrize("path,value", [
    (("winner", "score"), .5),
    (("winner", "metrics", "cost"), 99),
    (("winner", "metrics", "latency"), .1),
    (("winner", "metrics", "carbon_unit"), "gCO2"),
    (("winner", "assignments"), []),
    (("winner", "assignments", 0, "finish"), at(99).isoformat()),
    (("winner", "completion_at"), at(99).isoformat()),
    (("winner", "response_at"), at(99).isoformat()),
    (("winner", "normalized_metrics", "cost"), .5),
    (("winner", "weighted_contributions", "cost"), -.1),
    (("winner", "assignments", 0, "candidate", "estimates", "accuracy"),
     {"value": None, "unit": "ratio", "provenance": "unknown", "source": None, "uncertainty": None}),
    (("normalization", "complete_reference"), False),
    (("normalization", "complete_reference"), 1),
    (("normalization", "reference_id"), "not-a-fingerprint"),
    (("normalization", "carbon_unit"), "gCO2"),
    (("normalization", "bounds", "cost"), [2, 4]),
])
def test_persisted_nested_plans_reject_inconsistent_scores_times_and_scopes(tmp_path, path, value):
    import json
    from workflow.scheduling_records import load_record
    result = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW)
    payload = result.to_dict()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    output = tmp_path / "malformed-nested.json"
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_record(output)


def test_loaded_weights_must_match_saved_winner_contributions(tmp_path):
    import json
    from workflow.scheduling_records import load_record
    options = {"a": [candidate("fast", latency=1, cost=3), candidate("cheap", latency=3, cost=1)]}
    result = plan_workflow(request(), options, {**weights(), "latency": .5, "cost": .5}, now=NOW)
    payload = result.to_dict()
    payload["weights"] = weights()
    output = tmp_path / "wrong-weights.json"
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="saved weights"):
        load_record(output)


def test_loaded_truncated_plan_cannot_claim_global_optimality(tmp_path):
    import json
    from workflow.scheduling_records import load_record
    result = plan_workflow(request(), {"a": [candidate("a"), candidate("b", latency=4)]},
                           weights(), now=NOW, search_limit=1)
    payload = result.to_dict()
    payload["globally_optimal_within_candidates"] = True
    output = tmp_path / "wrong-optimality.json"
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="global optimality"):
        load_record(output)


def test_atomic_save_revalidates_a_tampered_joint_record_before_replacing_file(tmp_path):
    from workflow.scheduling_records import load_record, save_record
    result = plan_workflow(request(), {"a": [candidate()]}, weights(), now=NOW)
    output = tmp_path / "plan.json"
    save_record(output, result)
    original = output.read_bytes()
    object.__setattr__(result, "feasible_count", -1)
    with pytest.raises(ValueError):
        save_record(output, result)
    assert output.read_bytes() == original
    assert load_record(output).feasible_count == 1
