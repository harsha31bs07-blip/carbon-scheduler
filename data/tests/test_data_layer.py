"""Run from the repo root:  python -m pytest data/tests -q"""

from datetime import datetime, timezone

import pytest

from data import candidate_builder as cb
from data.eval import run_eval
from data.latency_model import PAIRWISE_LATENCY_MS, get_inter_region_latency_ms
from data.model_table import (
    get_model_accuracy,
    get_model_cost_per_1k_tokens,
    get_model_detailed_cost,
    normalize_model_name,
)
from data.region_carbon import REGION_PROFILES, normalize_location
from data.time_curve import get_24h_carbon_forecast

T0 = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)


def test_model_aliases():
    assert normalize_model_name("claude-haiku-4-5-20251001") == "haiku"
    assert normalize_model_name("claude-sonnet-5") == "sonnet"
    assert normalize_model_name("CLAUDE-OPUS-5") == "opus"
    with pytest.raises(ValueError):
        normalize_model_name("gpt-4")


def test_pricing_order_and_blend():
    h, s, o = (get_model_cost_per_1k_tokens(m) for m in ("haiku", "sonnet", "opus"))
    assert h < s < o
    assert get_model_detailed_cost("haiku", 1000, 0) == pytest.approx(0.001)
    assert get_model_detailed_cost("sonnet", 0, 1000) == pytest.approx(0.010)
    assert get_model_detailed_cost("opus", 1000, 1000) == pytest.approx(0.030)


def test_region_aliases_and_latency_symmetry():
    assert normalize_location("ap-south-2") == "in-south"
    assert normalize_location("us-east-1") == "us-east"
    regs = list(REGION_PROFILES)
    for a in regs:
        for b in regs:
            assert (a, b) in PAIRWISE_LATENCY_MS
            assert get_inter_region_latency_ms(a, b) == get_inter_region_latency_ms(b, a)


def test_carbon_forecast_is_deterministic_and_varies():
    f1 = get_24h_carbon_forecast("in-south", T0)
    f2 = get_24h_carbon_forecast("in-south", T0)
    assert f1 == f2
    vals = [x["carbon_intensity_g_per_kwh"] for x in f1]
    assert max(vals) / min(vals) > 1.3


def test_scoring_rejects_substring_of_reference():
    item = {
        "reference_answer": "P1",
        "rubric": {"format_constraint": "single_label", "expected_keywords": ["P1", "Critical"]},
    }
    assert run_eval.score_response("P", item) < 0.5  # used to be 1.0
    assert run_eval.score_response("P1 (Critical)", item) > 0.9


def test_live_failure_does_not_silently_fall_back(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def boom(*a, **k):
        raise RuntimeError("model not found")

    monkeypatch.setattr(run_eval, "call_anthropic_api", boom)
    with pytest.raises(RuntimeError):
        run_eval.run_evaluation(models=["haiku"], verbose=False)
    res = run_eval.run_evaluation(models=["haiku"], verbose=False, allow_fallback=True)
    assert res["is_measured"] is False and res["mode"] == "live_api_partial"


def test_mock_results_are_not_treated_as_measured(tmp_path):
    res = run_eval.run_evaluation(force_mock=True, verbose=False)
    assert res["is_measured"] is False
    path = tmp_path / "r.json"
    run_eval.save_results(res, path)
    assert get_model_accuracy("sonnet", eval_results_file=path) == 0.88  # assumed base
    assert get_model_accuracy("sonnet", eval_results_file=path, allow_simulated=True) != 0.88


def test_candidate_tradeoffs():
    cands = cb.enumerate_candidates("s1", 1200, 300, base_utc_time=T0)
    assert len(cands) == 3 * 3 * 5
    at = lambda m, loc, off: next(
        c for c in cands if (c.model, c.location, c.time_offset_hr) == (m, loc, off)
    )
    h, s, o = (at(m, "us-east", 0) for m in ("haiku", "sonnet", "opus"))
    assert h.est_cost_usd < s.est_cost_usd < o.est_cost_usd
    assert h.est_latency_ms < s.est_latency_ms < o.est_latency_ms  # model latency now matters
    assert h.est_energy_kwh < s.est_energy_kwh < o.est_energy_kwh
    assert h.est_accuracy < s.est_accuracy < o.est_accuracy
    # cleaner grid => less carbon for identical work
    assert at("sonnet", "eu-west", 0).est_carbon_g < at("sonnet", "in-south", 0).est_carbon_g


def test_candidate_roundtrip_and_validation():
    c = cb.estimate_candidate("s1", "claude-sonnet-5", "Dublin", 2, 500, 200, "qa", T0)
    assert (c.model, c.location) == ("sonnet", "eu-west")
    from shared.schema import Candidate

    assert Candidate.from_dict(c.to_dict()) == c
    with pytest.raises(ValueError):
        cb.estimate_candidate("s1", "haiku", "us-east", -1)
    with pytest.raises(ValueError):
        cb.estimate_candidate("s1", "haiku", "mars")


def test_tokenizer_factor_applied_to_new_tokenizer_models_only():
    kw = dict(input_tokens=1000, output_tokens=1000, base_utc_time=T0)
    for model, factor in (("haiku", 1.0), ("sonnet", 1.3), ("opus", 1.3)):
        with_f = cb.estimate_candidate("s", model, "us-east", **kw)
        without = cb.estimate_candidate("s", model, "us-east", apply_tokenizer_factor=False, **kw)
        assert with_f.est_cost_usd == pytest.approx(without.est_cost_usd * factor)
        assert with_f.est_energy_kwh == pytest.approx(without.est_energy_kwh * factor)


def test_enumerate_uses_one_reference_time():
    a = cb.enumerate_candidates("s", base_utc_time=T0)
    b = cb.enumerate_candidates("s", base_utc_time=T0)
    assert a == b  # fully deterministic when base_utc_time is fixed
