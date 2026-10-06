"""Offline vs. hybrid candidate pools, baseline choice, and executor fallback across modes."""

import pytest

from data.candidate_builder import (
    CLOUD_LOCATIONS,
    LOCAL_MODELS,
    available_models,
    candidate_pool,
)
from scheduler.candidate_gen import TIME_OFFSETS, baseline_choice, generate_candidates
from scheduler.data_adapter import build_candidate
from scheduler.optimizer import schedule_step
from shared import availability
from workflow import executor
from workflow.executor import MODEL_NAME_MAP, _fallback_candidate
from workflow.model_clients import MODEL_REGISTRY


class TestCandidatePool:
    def test_offline_pool_is_local_only(self):
        assert available_models() == tuple(LOCAL_MODELS)
        assert candidate_pool() == [(m, "local-gpu") for m in LOCAL_MODELS]
        assert availability.mode_label() == "Offline"

    def test_gemini_adds_flash_in_cloud_regions(self, hybrid):
        hybrid(gemini=True)
        pool = candidate_pool()
        assert {m for m, _ in pool} == {*LOCAL_MODELS, "gemini-3-flash"}
        assert availability.mode_label() == "Hybrid: Gemini"

    def test_nvidia_adds_nemotron(self, hybrid):
        hybrid(gemini=False, nvidia=True)
        assert set(available_models()) == {*LOCAL_MODELS, "nemotron-ultra-3"}

    def test_no_cross_deployment_pairs(self, hybrid):
        """Local models never land in a cloud region, cloud models never on-device."""
        hybrid(gemini=True, nvidia=True)
        for model, location in candidate_pool():
            if model in LOCAL_MODELS:
                assert location == "local-gpu"
            else:
                assert location in CLOUD_LOCATIONS

    def test_mode_switch_takes_effect_without_reimport(self, hybrid):
        before = len(generate_candidates("classify"))
        hybrid(gemini=True)
        after = len(generate_candidates("classify"))
        assert before == len(LOCAL_MODELS) * len(TIME_OFFSETS)
        assert after == (len(LOCAL_MODELS) + len(CLOUD_LOCATIONS)) * len(TIME_OFFSETS)

    def test_every_hybrid_model_is_dispatchable(self, hybrid):
        hybrid(gemini=True, nvidia=True)
        for model in available_models():
            assert MODEL_NAME_MAP.get(model, model) in MODEL_REGISTRY, model


class TestBaseline:
    def test_offline_baseline_is_larger_local_model(self):
        assert baseline_choice() == ("local-qwen3.5", "local-gpu")

    def test_hybrid_baseline_is_most_accurate_available_model(self, hybrid):
        hybrid(gemini=True)
        assert baseline_choice()[0] == "gemini-3-flash"
        hybrid(gemini=True, nvidia=True)
        model, location = baseline_choice()
        assert model == "nemotron-ultra-3"
        assert location in CLOUD_LOCATIONS

    def test_baseline_priced_like_scheduled_candidates(self):
        """Baseline and scheduler must use the same per-step token counts, or savings are skewed."""
        model, region = baseline_choice()
        for step_id in ["classify", "draft"]:
            baseline = executor._make_baseline_candidate(step_id)
            same_choice = build_candidate(step_id, model, region, 0)
            assert baseline.est_energy_kwh == pytest.approx(same_choice.est_energy_kwh)
            assert baseline.est_latency_ms == pytest.approx(same_choice.est_latency_ms)


class TestHybridScheduling:
    def test_schedule_step_works_in_hybrid(self, hybrid):
        hybrid(gemini=True, nvidia=True)
        pool = candidate_pool()
        for policy in ["speed", "eco", "balanced"]:
            c = schedule_step("draft", policy)
            assert (c.model, c.location) in pool


class TestFallback:
    def test_local_pick_falls_back_to_other_local_model(self):
        c = build_candidate("classify", "local-llama3.2", "local-gpu", 2)
        fb = _fallback_candidate(c)
        assert (fb.model, fb.location, fb.time_offset_hr) == ("local-qwen3.5", "local-gpu", 2)

    def test_cloud_pick_falls_back_to_best_local_model(self):
        c = build_candidate("classify", "gemini-3-flash", "eu-west", 0)
        fb = _fallback_candidate(c)
        assert (fb.model, fb.location) == ("local-qwen3.5", "local-gpu")
