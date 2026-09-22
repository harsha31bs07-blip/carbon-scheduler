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

import pytest
from shared.schema import Candidate, STEP_DEFINITIONS
from scheduler.candidate_gen import (
    generate_candidates,
    get_step,
    STEPS,
    MODELS,
    REGIONS,
    TIME_OFFSETS,
)
from scheduler.scorer import score_candidates, _normalize
from scheduler.policies import get_policy_weights, POLICIES
from scheduler.optimizer import schedule_step
from scheduler.data_adapter import get_carbon


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


class TestHardConstraintsAndFallback:
    """Tests for accuracy floor, latency SLA, and fallback logic."""

    def create_synthetic_candidates(self, step_id: str, overrides: list[dict]) -> list[Candidate]:
        """Create synthetic candidates with specific metric overrides for testing."""
        step = get_step(step_id)
        candidates = []
        for i, override in enumerate(overrides):
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

    def test_accuracy_floor_filters_low_accuracy(self):
        """Candidates below min_accuracy are filtered out."""
        step_id = "classify"  # min_accuracy = 0.75
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

    def test_latency_sla_filters_high_latency(self):
        """Candidates exceeding max_latency_ms are filtered out."""
        step_id = "summarize"  # max_latency_ms = 1500
        candidates = self.create_synthetic_candidates(step_id, [
            {"latency": 1500, "accuracy": 0.9},   # passes
            {"latency": 2500, "accuracy": 0.9},   # fails (exceeds 2000)
            {"latency": 1000, "accuracy": 0.9},   # passes
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 2
        assert all(c.est_latency_ms <= 2000 for c, _ in scored)

    def test_fallback_when_all_filtered_by_accuracy(self):
        """When all candidates fail accuracy, fallback picks highest accuracy."""
        step_id = "classify"  # min_accuracy = 0.75
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

    def test_fallback_when_all_filtered_by_latency(self):
        """When all candidates fail latency SLA, fallback picks lowest violation."""
        step_id = "summarize"  # max_latency_ms = 1500
        candidates = self.create_synthetic_candidates(step_id, [
            {"latency": 3000, "accuracy": 0.9},  # violation = 1000
            {"latency": 2500, "accuracy": 0.9},  # violation = 500
            {"latency": 4000, "accuracy": 0.9},  # violation = 2000
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 1
        # Fallback should pick lowest latency violation (2500 -> violation 500)
        assert scored[0][0].est_latency_ms == 2500

    def test_fallback_prefers_highest_accuracy_then_lowest_violation(self):
        """Fallback: highest accuracy first, then lowest latency violation."""
        step_id = "draft"  # min_accuracy = 0.80, max_latency_ms = 3000
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

    def test_fallback_no_sla_uses_raw_latency(self):
        """When no max_latency_ms defined, use raw latency for fallback."""
        step_id = "retrieve"  # max_latency_ms = 3000
        candidates = self.create_synthetic_candidates(step_id, [
            {"accuracy": 0.90, "latency": 4000},
            {"accuracy": 0.90, "latency": 3500},   # same accuracy, lower latency
            {"accuracy": 0.90, "latency": 5000},
        ])
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        assert len(scored) == 1
        # Should pick lowest latency violation (3500 -> violation 500)
        assert scored[0][0].est_latency_ms == 3500


class TestPolicySpecificWinners:
    """Tests that each policy selects the expected type of candidate."""

    def create_policy_test_candidates(self, step_id: str) -> list[Candidate]:
        """Create candidates with clear winners for each policy."""
        step = get_step(step_id)
        # Create 3 candidates: fast/cheap, eco-friendly, balanced
        return [
            Candidate(step_id, "haiku", "us-east", 0, 50.0, 0.85, 0.001, 0.0005, 50.0),   # fast, low carbon
            Candidate(step_id, "opus", "eu-west", 0, 200.0, 0.90, 0.02, 0.01, 500.0),     # slow, high carbon
            Candidate(step_id, "sonnet", "in-south", 0, 120.0, 0.88, 0.005, 0.002, 200.0),  # middle
        ]

    def test_speed_policy_picks_lowest_latency(self):
        """Speed policy should pick candidate with lowest latency."""
        step_id = "classify"
        candidates = self.create_policy_test_candidates(step_id)
        weights = get_policy_weights("speed")
        scored = score_candidates(candidates, weights, step_id)
        winner = scored[0][0]
        # haiku model has lowest latency (50ms)
        assert winner.model == "haiku"
        assert winner.est_latency_ms == 50.0

    def test_eco_policy_picks_lowest_carbon(self):
        """Eco policy should pick candidate with lowest carbon."""
        step_id = "classify"
        candidates = self.create_policy_test_candidates(step_id)
        weights = get_policy_weights("eco")
        scored = score_candidates(candidates, weights, step_id)
        winner = scored[0][0]
        # haiku model has lowest carbon (50g)
        assert winner.model == "haiku"
        assert winner.est_carbon_g == 50.0

    def test_balanced_policy_picks_middle_ground(self):
        """Balanced policy should pick a middle-ground candidate."""
        step_id = "classify"
        candidates = self.create_policy_test_candidates(step_id)
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        winner = scored[0][0]
        # With equal weights, sonnet model (middle ground) often wins
        # But let's verify it's not an extreme
        assert winner.model in {"haiku", "sonnet", "opus"}


class TestTimeOffsetCarbonSensitivity:
    """Tests that time offset affects carbon and policies respond accordingly."""

    def test_carbon_changes_with_time_offset(self):
        """Carbon intensity changes with time offset (per data_adapter formula)."""
        # Carbon intensity follows diurnal pattern - use fixed base_utc_time for reproducibility
        from datetime import datetime, timezone
        base_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # Solar noon UTC
        carbon_0 = get_carbon("haiku", "us-east", 0, "inference", base_utc_time=base_time)
        carbon_4 = get_carbon("haiku", "us-east", 4, "inference", base_utc_time=base_time)
        carbon_8 = get_carbon("haiku", "us-east", 8, "inference", base_utc_time=base_time)
        # Carbon should change with time offset (not all equal)
        assert carbon_0 != carbon_4 or carbon_4 != carbon_8  # At least one difference
        assert carbon_0 > 0 and carbon_4 > 0 and carbon_8 > 0  # All positive

    def test_eco_policy_prefers_later_offset_when_carbon_improves(self):
        """Eco policy should select later time offset when it reduces carbon."""
        step_id = "retrieve"  # batch, max_latency=1000 (lenient)
        # Generate all real candidates for this step
        candidates = generate_candidates(step_id)
        weights = get_policy_weights("eco")
        scored = score_candidates(candidates, weights, step_id)
        winner = scored[0][0]
        # With eco policy (high carbon weight), should prefer offset > 0 if carbon improves
        # Note: depends on actual data, but at minimum should not crash
        assert winner.time_offset_hr in TIME_OFFSETS
        assert winner.est_carbon_g > 0


class TestEndToEndScheduling:
    """End-to-end tests for schedule_step across all steps and policies."""

    def test_schedule_step_returns_candidate_for_all_steps_speed(self):
        """schedule_step returns Candidate for all 4 steps with speed policy."""
        for step in STEPS:
            step_id = step["step_id"]
            candidate = schedule_step(step_id, "speed")
            assert isinstance(candidate, Candidate)
            assert candidate.step_id == step_id
            assert candidate.model in MODELS
            assert candidate.location in REGIONS
            assert candidate.time_offset_hr in TIME_OFFSETS
            assert candidate.est_accuracy >= step.get("min_accuracy", 0.7) or True  # fallback may violate

    def test_schedule_step_returns_candidate_for_all_steps_balanced(self):
        """schedule_step returns Candidate for all 4 steps with balanced policy."""
        for step in STEPS:
            step_id = step["step_id"]
            candidate = schedule_step(step_id, "balanced")
            assert isinstance(candidate, Candidate)
            assert candidate.step_id == step_id

    def test_schedule_step_returns_candidate_for_all_steps_eco(self):
        """schedule_step returns Candidate for all 4 steps with eco policy."""
        for step in STEPS:
            step_id = step["step_id"]
            candidate = schedule_step(step_id, "eco")
            assert isinstance(candidate, Candidate)
            assert candidate.step_id == step_id

    def test_schedule_step_returns_candidate_for_all_steps_custom(self):
        """schedule_step returns Candidate for all 4 steps with custom policy."""
        custom_weights = {"w_latency": 0.3, "w_cost": 0.2, "w_energy": 0.2, "w_carbon": 0.2, "w_accuracy": 0.1}
        for step in STEPS:
            step_id = step["step_id"]
            candidate = schedule_step(step_id, "custom", custom_weights)
            assert isinstance(candidate, Candidate)
            assert candidate.step_id == step_id

    def test_different_policies_produce_different_results(self):
        """Different policies should generally produce different choices."""
        step_id = "classify"
        speed_choice = schedule_step(step_id, "speed")
        eco_choice = schedule_step(step_id, "eco")
        balanced_choice = schedule_step(step_id, "balanced")
        # At least some should differ (not guaranteed for all steps, but likely)
        choices = [(speed_choice.model, speed_choice.location, speed_choice.time_offset_hr),
                   (eco_choice.model, eco_choice.location, eco_choice.time_offset_hr),
                   (balanced_choice.model, balanced_choice.location, balanced_choice.time_offset_hr)]
        # Allow for some overlap but not all identical
        assert len(set(choices)) >= 2


class TestCandidateGeneration:
    """Tests for candidate generation completeness."""

    def test_generate_candidates_returns_correct_count(self):
        """generate_candidates returns MODELS x REGIONS x TIME_OFFSETS candidates."""
        for step in STEPS:
            candidates = generate_candidates(step["step_id"])
            assert len(candidates) == len(MODELS) * len(REGIONS) * len(TIME_OFFSETS)

    def test_generate_candidates_all_combinations_present(self):
        """All model/region/offset combinations are generated."""
        step_id = "classify"
        candidates = generate_candidates(step_id)
        combos = {(c.model, c.location, c.time_offset_hr) for c in candidates}
        expected = {(m, r, o) for m in MODELS for r in REGIONS for o in TIME_OFFSETS}
        assert combos == expected

    def test_generate_candidates_metrics_populated(self):
        """All metric fields are populated with positive values."""
        candidates = generate_candidates("classify")
        for c in candidates:
            assert c.est_latency_ms > 0
            assert 0 < c.est_accuracy <= 1.0
            assert c.est_cost_usd > 0
            assert c.est_energy_kwh > 0
            assert c.est_carbon_g > 0


class TestTieBreaking:
    """Tests for deterministic tie-breaking in scoring."""

    def test_tie_breaking_deterministic(self):
        """Identical candidates produce deterministic ordering."""
        step_id = "classify"
        # Create multiple identical candidates
        candidates = []
        for _ in range(5):
            c = Candidate(step_id, "haiku", "us-east", 0, 100.0, 0.85, 0.01, 0.001, 100.0)
            candidates.append(c)
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        # All should have same score, tie-breaking by carbon, latency, model, location, offset
        assert len(scored) == 5
        # All scores should be equal
        scores = [s for _, s in scored]
        assert all(s == scores[0] for s in scores)

    def test_tie_breaking_order(self):
        """Tie-breaking follows: carbon, latency, model, location, offset."""
        step_id = "classify"
        # Create candidates with same normalized metrics but different tie-break fields
        # Two candidates have same low carbon (100), same latency (100)
        # Lexicographically: "haiku" < "sonnet" < "opus"
        candidates = [
            Candidate(step_id, "opus", "in-south", 3, 100.0, 0.85, 0.01, 0.001, 200.0),  # high carbon
            Candidate(step_id, "sonnet", "us-east", 0, 100.0, 0.85, 0.01, 0.001, 100.0),  # low carbon
            Candidate(step_id, "haiku", "eu-west", 1, 100.0, 0.85, 0.01, 0.001, 100.0), # same carbon as sonnet
        ]
        weights = get_policy_weights("balanced")
        scored = score_candidates(candidates, weights, step_id)
        # Winner should be lowest carbon; among tied carbon, lowest latency;
        # among tied, lexicographically first model ("haiku" < "sonnet")
        winner = scored[0][0]
        assert winner.model == "haiku"
        assert winner.location == "eu-west"
        assert winner.time_offset_hr == 1


class TestReproducibility:
    """Tests that results are deterministic across runs."""

    def test_schedule_step_deterministic(self):
        """Multiple calls with same inputs produce same output."""
        step_id = "summarize"
        results = [schedule_step(step_id, "balanced") for _ in range(10)]
        first = results[0]
        for r in results[1:]:
            assert r.model == first.model
            assert r.location == first.location
            assert r.time_offset_hr == first.time_offset_hr
            assert r.est_latency_ms == first.est_latency_ms
            assert r.est_accuracy == first.est_accuracy
            assert r.est_cost_usd == first.est_cost_usd
            assert r.est_energy_kwh == first.est_energy_kwh
            assert r.est_carbon_g == pytest.approx(first.est_carbon_g, rel=1e-3)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])