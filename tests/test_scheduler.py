"""
Comprehensive test suite for the scheduler module.

Tests cover:
- Policy-specific winners (speed, eco, balanced)
- Hard constraints (accuracy floor, latency SLA, fallback logic)
- Zero-range normalization
- Custom weights validation and normalization
- Time offset carbon sensitivity
- End-to-end schedule_step for all steps and policies
"""

from datetime import datetime, timezone

import pytest
from shared.schema import Candidate
from data.candidate_builder import candidate_pool
from scheduler.candidate_gen import (
    generate_candidates,
    get_step,
    STEPS,
    TIME_OFFSETS,
)
from scheduler import optimizer, scorer
from scheduler.scorer import score_candidates, _normalize
from scheduler.policies import get_policy_weights, POLICIES
from scheduler.optimizer import schedule_step
from scheduler.data_adapter import get_carbon


FIXED_BASE_TIME = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestNormalization:
    """Tests for the _normalize helper function."""

    def test_normalize_typical_range(self):
        """Normalize spreads values to [0, 1] range."""
        values = [10.0, 20.0, 30.0, 40.0]
        result = _normalize(values)
        assert result == [0.0, 1/3, 2/3, 1.0]

    def test_normalize_zero_range(self):
        """Identical values don't divide by zero; all become 0.0."""
        values = [5.0, 5.0, 5.0]
        result = _normalize(values)
        assert result == [0.0, 0.0, 0.0]

    def test_normalize_single_value(self):
        """Single value normalizes to 0.0."""
        values = [42.0]
        result = _normalize(values)
        assert result == [0.0]

    def test_normalize_negative_values(self):
        """Negative values normalize correctly."""
        values = [-10.0, 0.0, 10.0]
        result = _normalize(values)
        assert result == [0.0, 0.5, 1.0]


class TestPolicyWeights:
    """Tests for policy weight retrieval and validation."""

    def test_builtin_policies_exist(self):
        """All three builtin policies are defined."""
        assert set(POLICIES.keys()) == {"speed", "eco", "balanced"}

    def test_speed_policy_weights(self):
        """Speed policy emphasizes latency."""
        w = get_policy_weights("speed")
        assert w["w_latency"] == 0.6
        assert w["w_accuracy"] == 0.1
        assert sum(w.values()) == pytest.approx(1.0)

    def test_eco_policy_weights(self):
        """Eco policy emphasizes carbon and energy."""
        w = get_policy_weights("eco")
        assert w["w_carbon"] == 0.4
        assert w["w_energy"] == 0.3
        assert sum(w.values()) == pytest.approx(1.0)

    def test_balanced_policy_weights(self):
        """Balanced policy has equal weights."""
        w = get_policy_weights("balanced")
        assert all(v == 0.2 for v in w.values())
        assert sum(w.values()) == pytest.approx(1.0)

    def test_custom_weights_valid_dict(self):
        """Valid custom weights dict is accepted and normalized."""
        custom = {"w_latency": 0.5, "w_cost": 0.2, "w_energy": 0.1, "w_carbon": 0.1, "w_accuracy": 0.1}
        w = get_policy_weights("custom", custom)
        assert w["w_latency"] == 0.5
        assert sum(w.values()) == pytest.approx(1.0)

    def test_custom_weights_missing_keys_filled_with_zero(self):
        """Missing keys in custom weights are filled with 0."""
        custom = {"w_latency": 1.0}  # only latency specified
        w = get_policy_weights("custom", custom)
        assert w["w_latency"] == 1.0
        assert w["w_cost"] == 0.0
        assert w["w_energy"] == 0.0
        assert w["w_carbon"] == 0.0
        assert w["w_accuracy"] == 0.0

    def test_custom_weights_negative_rejected(self):
        """Negative weight values raise ValueError."""
        custom = {"w_latency": -0.1, "w_cost": 0.2, "w_energy": 0.2, "w_carbon": 0.2, "w_accuracy": 0.5}
        with pytest.raises(ValueError, match="Weights cannot be negative"):
            get_policy_weights("custom", custom)

    def test_custom_weights_all_zero_rejected(self):
        """All-zero weights raise ValueError."""
        custom = {"w_latency": 0.0, "w_cost": 0.0, "w_energy": 0.0, "w_carbon": 0.0, "w_accuracy": 0.0}
        with pytest.raises(ValueError, match="Weights must not all be zero"):
            get_policy_weights("custom", custom)

    def test_custom_weights_non_sum_to_one_normalized(self):
        """Non-unit-sum weights are normalized to sum to 1."""
        custom = {"w_latency": 2.0, "w_cost": 2.0, "w_energy": 2.0, "w_carbon": 2.0, "w_accuracy": 2.0}
        w = get_policy_weights("custom", custom)
        assert all(v == 0.2 for v in w.values())
        assert sum(w.values()) == pytest.approx(1.0)

    def test_unknown_policy_rejected(self):
        """Unknown policy name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown policy"):
            get_policy_weights("nonexistent")

    def test_custom_policy_requires_weights(self):
        """Custom policy without weights raises ValueError."""
        with pytest.raises(ValueError, match="custom_weights required"):
            get_policy_weights("custom", None)


def pin_step_rules(monkeypatch, min_accuracy=0.75, max_latency_ms=None):
    """Pin a step's constraints so tests don't depend on the production SLAs in STEP_DEFINITIONS."""
    monkeypatch.setattr(
        scorer,
        "get_step",
        lambda step_id: {"step_id": step_id, "min_accuracy": min_accuracy, "max_latency_ms": max_latency_ms},
    )


class TestHardConstraintsAndFallback:
    """Tests for accuracy floor, latency SLA, and fallback logic."""

    def create_synthetic_candidates(self, step_id: str, overrides: list[dict]) -> list[Candidate]:
        """Create synthetic candidates with specific metric overrides for testing."""
        candidates = []
        for override in overrides:
            c = Candidate(
                step_id=step_id,
                model=override.get("model", "haiku"),
                location=override.get("location", "us-east"),
                time_offset_hr=override.get("offset", 0),
                est_latency_ms=override.get("latency", 100.0),
                est_accuracy=override.get("accuracy", 0.9),
                est_cost_usd=override.get("cost", 0.01),
                est_energy_kwh=override.get("energy", 0.001),
                est_carbon_g=override.get("carbon", 100.0),
            )
            candidates.append(c)
        return candidates

    def test_accuracy_floor_filters_low_accuracy(self, monkeypatch):
        """Candidates below min_accuracy are filtered out."""
        step_id = "classify"
        pin_step_rules(monkeypatch, min_accuracy=0.75)
        candidates = self.create_synthetic_candidates(step_id, [
            {"accuracy": 0.80, "latency": 100},  # passes
            {"accuracy": 0.70, "latency": 100},  # fails (below 0.75)
            {"accuracy": 0.90, "latency": 100},  # passes
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        # Only 2 candidates should remain (0.80 and 0.90 accuracy)
        assert len(scored) == 2
        assert all(c.est_accuracy >= 0.75 for c, _ in scored)

    def test_latency_sla_filters_high_latency(self, monkeypatch):
        """Candidates exceeding max_latency_ms are filtered out."""
        step_id = "classify"
        pin_step_rules(monkeypatch, min_accuracy=0.6, max_latency_ms=2000)
        candidates = self.create_synthetic_candidates(step_id, [
            {"latency": 1500, "accuracy": 0.9},   # passes
            {"latency": 2500, "accuracy": 0.9},   # fails (exceeds 2000)
            {"latency": 1000, "accuracy": 0.9},   # passes
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 2
        assert all(c.est_latency_ms <= 2000 for c, _ in scored)

    def test_fallback_when_all_filtered_by_accuracy(self, monkeypatch):
        """When all candidates fail accuracy, fallback picks highest accuracy."""
        step_id = "classify"
        pin_step_rules(monkeypatch, min_accuracy=0.75)
        candidates = self.create_synthetic_candidates(step_id, [
            {"accuracy": 0.70, "latency": 100},  # all below floor
            {"accuracy": 0.72, "latency": 100},
            {"accuracy": 0.74, "latency": 100},
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 1
        # Fallback should pick highest accuracy (0.74)
        assert scored[0][0].est_accuracy == 0.74

    def test_fallback_when_all_filtered_by_latency(self, monkeypatch):
        """When all candidates fail latency SLA, fallback picks lowest violation."""
        step_id = "classify"
        pin_step_rules(monkeypatch, min_accuracy=0.6, max_latency_ms=1500)
        candidates = self.create_synthetic_candidates(step_id, [
            {"latency": 3000, "accuracy": 0.9},  # violation = 1500
            {"latency": 2500, "accuracy": 0.9},  # violation = 1000
            {"latency": 4000, "accuracy": 0.9},  # violation = 2500
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 1
        # Fallback should pick lowest latency violation (2500 -> violation 1000)
        assert scored[0][0].est_latency_ms == 2500

    def test_fallback_prefers_highest_accuracy_then_lowest_violation(self, monkeypatch):
        """Fallback: highest accuracy first, then lowest latency violation."""
        step_id = "classify"
        pin_step_rules(monkeypatch, min_accuracy=0.80, max_latency_ms=3000)
        candidates = self.create_synthetic_candidates(step_id, [
            {"accuracy": 0.75, "latency": 100},  # fails accuracy
            {"accuracy": 0.85, "latency": 4000},  # fails latency
            {"accuracy": 0.78, "latency": 3500},  # fails both
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 1
        # Among failures, highest accuracy (0.85) should win
        assert scored[0][0].est_accuracy == 0.85

    def test_fallback_no_sla_uses_raw_latency(self, monkeypatch):
        """When no max_latency_ms defined, use raw latency for fallback."""
        step_id = "classify"
        pin_step_rules(monkeypatch, min_accuracy=0.95)
        candidates = self.create_synthetic_candidates(step_id, [
            {"accuracy": 0.90, "latency": 4000},
            {"accuracy": 0.90, "latency": 3500},   # same accuracy, lower latency
            {"accuracy": 0.90, "latency": 5000},
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 1
        # All fail the accuracy floor; with no SLA the fallback ranks by raw latency
        assert scored[0][0].est_latency_ms == 3500


class TestPolicySpecificWinners:
    """Tests that each policy selects the expected type of candidate."""

    def create_policy_test_candidates(self, step_id: str) -> list[Candidate]:
        """Every candidate gives up something: speed, resources, or quality."""
        return [
            Candidate(step_id, "fast", "us-east", 0, 100.0, 0.85, 0.03, 0.0030, 300.0),
            Candidate(step_id, "green", "eu-west", 0, 1100.0, 0.80, 0.02, 0.0010, 100.0),
            Candidate(step_id, "compromise", "in-south", 0, 700.0, 0.90, 0.01, 0.0018, 180.0),
        ]

    @pytest.mark.parametrize("policy, expected_model", [
        ("speed", "fast"),
        ("eco", "green"),
        ("balanced", "compromise"),
    ])
    def test_policy_selects_expected_tradeoff(self, monkeypatch, policy, expected_model):
        """All options are feasible, so policy weights decide the winner."""
        pin_step_rules(monkeypatch, min_accuracy=0.75, max_latency_ms=1500)
        step_id = "classify"
        candidates = self.create_policy_test_candidates(step_id)
        scored = score_candidates(candidates, get_policy_weights(policy), step_id)
        assert len(scored) == len(candidates)
        assert scored[0][0].model == expected_model
        assert scored[0][1] < scored[1][1]


class TestTimeOffsetCarbonSensitivity:
    """Tests that time offset affects carbon and policies respond accordingly."""

    def test_carbon_changes_with_time_offset(self):
        """Carbon intensity changes with time offset (per data_adapter formula)."""
        # Carbon intensity follows diurnal pattern - use fixed base_utc_time for reproducibility
        carbon_0 = get_carbon("haiku", "us-east", 0, "inference", base_utc_time=FIXED_BASE_TIME)
        carbon_4 = get_carbon("haiku", "us-east", 4, "inference", base_utc_time=FIXED_BASE_TIME)
        carbon_8 = get_carbon("haiku", "us-east", 8, "inference", base_utc_time=FIXED_BASE_TIME)
        # Carbon should change with time offset (not all equal)
        assert carbon_0 != carbon_4 or carbon_4 != carbon_8  # At least one difference
        assert carbon_0 > 0 and carbon_4 > 0 and carbon_8 > 0  # All positive

    def test_eco_policy_prefers_later_offset_when_carbon_improves(self, monkeypatch):
        """Eco policy should select later time offset when it reduces carbon."""
        pin_step_rules(monkeypatch, min_accuracy=0.75, max_latency_ms=1000)
        step_id = "retrieve"
        candidates = [
            Candidate(step_id, "same-model", "same-region", offset, 500.0, 0.9, 0.01, 0.001, carbon)
            for offset, carbon in [(0, 100.0), (2, 20.0), (4, 90.0)]
        ]
        weights = get_policy_weights("eco")
        scored = score_candidates(candidates, weights, step_id)
        winner = scored[0][0]
        assert len(scored) == 3
        assert winner.time_offset_hr == 2
        assert winner.est_carbon_g == 20.0


class TestDeferral:
    """Deferring a step must cost latency in scoring, but not count against the compute SLA."""

    def same_model_at_offsets(self, carbons):
        # Identical compute; only the start time (and therefore grid carbon) differs.
        return [
            Candidate("classify", "m", "us-east", off, 1500.0, 0.9, 0.001, 0.001, carbon)
            for off, carbon in zip([0, 2, 4], carbons)
        ]

    def test_speed_policy_does_not_defer(self):
        candidates = self.same_model_at_offsets([100.0, 40.0, 30.0])
        winner = score_candidates(candidates, get_policy_weights("speed"), "classify")[0][0]
        assert winner.time_offset_hr == 0

    def test_eco_policy_defers_for_large_carbon_gain(self):
        candidates = self.same_model_at_offsets([100.0, 40.0, 30.0])
        winner = score_candidates(candidates, get_policy_weights("eco"), "classify")[0][0]
        assert winner.time_offset_hr == 4

    def test_eco_policy_does_not_defer_for_no_gain(self):
        candidates = self.same_model_at_offsets([100.0, 100.0, 100.0])
        winner = score_candidates(candidates, get_policy_weights("eco"), "classify")[0][0]
        assert winner.time_offset_hr == 0

    def test_deferred_candidates_pass_compute_sla(self, monkeypatch):
        pin_step_rules(monkeypatch, min_accuracy=0.6, max_latency_ms=2000)
        candidates = self.same_model_at_offsets([100.0, 40.0, 30.0])
        scored = score_candidates(candidates, get_policy_weights("eco"), "classify")
        assert len(scored) == 3


class TestEndToEndScheduling:
    """End-to-end tests for schedule_step across all steps and policies."""

    @pytest.mark.parametrize("step_id", [step["step_id"] for step in STEPS])
    @pytest.mark.parametrize("policy", ["speed", "balanced", "eco", "custom"])
    def test_schedule_step_respects_feasibility_or_legacy_fallback(self, step_id, policy):
        """Real candidate generation must produce a feasible winner or its documented fallback."""
        custom_weights = {"w_latency": 0.3, "w_cost": 0.2, "w_energy": 0.2, "w_carbon": 0.2, "w_accuracy": 0.1}
        step = get_step(step_id)
        pool = generate_candidates(step_id, base_utc_time=FIXED_BASE_TIME)
        candidate = schedule_step(
            step_id, policy, custom_weights if policy == "custom" else None,
            base_utc_time=FIXED_BASE_TIME,
        )
        assert isinstance(candidate, Candidate)
        assert candidate.step_id == step_id
        assert candidate in pool
        feasible = [
            option for option in pool
            if option.est_accuracy >= step.get("min_accuracy", 0.7)
            and (step.get("max_latency_ms") is None or option.est_latency_ms <= step["max_latency_ms"])
        ]
        if feasible:
            assert candidate in feasible
        else:
            # Legacy best effort prioritizes accuracy, then latency violation.
            # Membership alone would let an arbitrary infeasible candidate pass.
            highest_accuracy = max(option.est_accuracy for option in pool)
            assert candidate.est_accuracy == highest_accuracy
            tied = [option for option in pool if option.est_accuracy == highest_accuracy]
            max_latency = step.get("max_latency_ms")
            if max_latency is None:
                expected = min(tied, key=lambda option: option.est_latency_ms)
            else:
                expected = min(tied, key=lambda option: max(0, option.est_latency_ms - max_latency))
            assert candidate == expected

    def test_different_policies_produce_different_results(self, monkeypatch):
        """Given a pool with real trade-offs, speed and eco pick different candidates.

        Uses a fixed pool: the live pool depends on the wall-clock carbon curve and on
        which API keys are configured, so it can't guarantee a trade-off exists.
        """
        pool = [
            Candidate("classify", "fast", "us-east", 0, 100.0, 0.90, 0.030, 0.0030, 300.0),
            Candidate("classify", "mid", "eu-west", 2, 400.0, 0.85, 0.015, 0.0015, 120.0),
            Candidate("classify", "green", "eu-north", 4, 900.0, 0.80, 0.005, 0.0005, 20.0),
        ]
        monkeypatch.setattr(
            optimizer, "generate_candidates",
            lambda step_id, base_utc_time=None, *, execution_only=False: pool,
        )
        pin_step_rules(monkeypatch, min_accuracy=0.6, max_latency_ms=4000)

        assert schedule_step("classify", "speed").model == "fast"
        assert schedule_step("classify", "eco").model == "green"


class TestCandidateGeneration:
    """Tests for candidate generation completeness."""

    def test_generate_candidates_returns_correct_count(self):
        """generate_candidates returns one candidate per valid (model, location) pair per offset."""
        for step in STEPS:
            candidates = generate_candidates(step["step_id"])
            assert len(candidates) == len(candidate_pool()) * len(TIME_OFFSETS)

    def test_generate_candidates_all_combinations_present(self):
        """All valid model/location pairs are generated at every offset."""
        step_id = "classify"
        candidates = generate_candidates(step_id)
        combos = {(c.model, c.location, c.time_offset_hr) for c in candidates}
        expected = {(m, r, o) for m, r in candidate_pool() for o in TIME_OFFSETS}
        assert combos == expected

    def test_generate_candidates_metrics_populated(self):
        """All metric fields are populated with positive values."""
        candidates = generate_candidates("classify")
        for c in candidates:
            assert c.est_latency_ms > 0
            assert 0 < c.est_accuracy <= 1.0
            assert c.est_cost_usd >= 0  # local models are free
            assert c.est_energy_kwh > 0
            assert c.est_carbon_g > 0


class TestTieBreaking:
    """Tests for deterministic tie-breaking in scoring."""

    def test_tie_breaking_deterministic(self):
        """Metric ties produce the same ordering for different input orders."""
        step_id = "classify"
        candidates = [
            Candidate(step_id, model, location, 0, 100.0, 0.85, 0.01, 0.001, 100.0)
            for model, location in [("sonnet", "us-east"), ("haiku", "us-east"), ("haiku", "eu-west")]
        ]
        weights = get_policy_weights("balanced")
        expected = [("haiku", "eu-west"), ("haiku", "us-east"), ("sonnet", "us-east")]
        for ordered in (candidates, list(reversed(candidates)), candidates[1:] + candidates[:1]):
            scored = score_candidates(ordered, weights, step_id)
            assert [(candidate.model, candidate.location) for candidate, _ in scored] == expected
            assert all(score == 0.0 for _, score in scored)

    def test_tie_breaking_order(self):
        """Tie-breaking follows: carbon, latency, model, location, offset."""
        step_id = "classify"
        expected = [
            Candidate(step_id, "haiku", "eu-west", 0, 100.0, 0.85, 0.01, 0.001, 100.0),
            Candidate(step_id, "haiku", "eu-west", 1, 100.0, 0.85, 0.01, 0.001, 100.0),
            Candidate(step_id, "haiku", "us-east", 0, 100.0, 0.85, 0.01, 0.001, 100.0),
            Candidate(step_id, "sonnet", "eu-west", 0, 100.0, 0.85, 0.01, 0.001, 100.0),
            Candidate(step_id, "haiku", "eu-west", 0, 200.0, 0.85, 0.01, 0.001, 100.0),
            Candidate(step_id, "haiku", "eu-west", 0, 100.0, 0.85, 0.01, 0.001, 200.0),
        ]
        # With identical accuracy and every other weight zero, scores are exactly
        # tied. The full output therefore exercises each tie-break field alone.
        weights = get_policy_weights("custom", {"w_accuracy": 1.0})
        scored = score_candidates(list(reversed(expected)), weights, step_id)
        assert all(score == 0.0 for _, score in scored)
        assert [candidate for candidate, _ in scored] == expected


class TestReproducibility:
    """Tests that results are deterministic across runs."""

    def test_schedule_step_deterministic(self):
        """Multiple calls with same inputs produce same output."""
        step_id = "summarize"
        results = [schedule_step(step_id, "balanced", base_utc_time=FIXED_BASE_TIME) for _ in range(10)]
        first = results[0]
        for r in results[1:]:
            assert r.model == first.model
            assert r.location == first.location
            assert r.time_offset_hr == first.time_offset_hr
            assert r.est_latency_ms == first.est_latency_ms
            assert r.est_accuracy == first.est_accuracy
            assert r.est_cost_usd == first.est_cost_usd
            assert r.est_energy_kwh == first.est_energy_kwh
            assert r.est_carbon_g == first.est_carbon_g


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
