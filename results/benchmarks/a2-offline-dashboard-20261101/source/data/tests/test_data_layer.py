"""Run from the repo root:  python -m pytest data/tests -q"""

import dataclasses
from datetime import datetime, timezone

import pytest

from data import candidate_builder as cb
from data.eval import run_eval
from data.latency_model import PAIRWISE_LATENCY_MS, get_inter_region_latency_ms
from data.model_table import (
    MODEL_TABLE,
    get_model_accuracy,
    get_model_cost_per_1k_tokens,
    get_model_detailed_cost,
    normalize_model_name,
)
from data.region_carbon import REGION_PROFILES, normalize_location
from data.time_curve import get_24h_carbon_forecast

T0 = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)


def test_model_aliases():
    assert normalize_model_name("gemini-3-flash-preview") == "gemini-3-flash"
    assert normalize_model_name("NEMOTRON_ULTRA_3") == "nemotron-ultra-3"
    assert normalize_model_name("qwen3.5:4b") == "local-qwen3.5"
    # legacy tier names in old results/ resolve to the models that actually ran
    assert normalize_model_name("haiku") == "gemini-3-flash"
    assert normalize_model_name("sonnet") == "nemotron-ultra-3"
    with pytest.raises(ValueError):
        normalize_model_name("gpt-4")


def test_published_pricing():
    assert get_model_detailed_cost("gemini-3-flash", 1000, 0) == pytest.approx(0.0005)
    assert get_model_detailed_cost("gemini-3-flash", 0, 1000) == pytest.approx(0.003)
    assert get_model_detailed_cost("nemotron-ultra-3", 1000, 1000) == pytest.approx(0.0027)
    for local in ("local-qwen3.5", "local-llama3.2"):
        assert get_model_cost_per_1k_tokens(local) == 0.0


def test_region_aliases_and_latency_symmetry():
    assert normalize_location("ap-south-2") == "in-south"
    assert normalize_location("us-east-1") == "us-east"
    regs = [r for r in REGION_PROFILES if r != "local-gpu"]
    for a in regs:
        for b in regs:
            assert (a, b) in PAIRWISE_LATENCY_MS
            assert get_inter_region_latency_ms(a, b) == get_inter_region_latency_ms(b, a)


def test_local_deployment_has_no_network_latency():
    for origin in list(REGION_PROFILES):
        assert get_inter_region_latency_ms(origin, "local-gpu") == 0.0


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
    def boom(*a, **k):
        raise RuntimeError("model not found")

    monkeypatch.setattr(run_eval, "call_live_model", boom)
    with pytest.raises(RuntimeError):
        run_eval.run_evaluation(models=["gemini-3-flash"], verbose=False)
    res = run_eval.run_evaluation(models=["gemini-3-flash"], verbose=False, allow_fallback=True)
    assert res["is_measured"] is False and res["mode"] == "live_api_partial"


def test_live_responses_keep_proxy_score_estimated(monkeypatch):
    monkeypatch.setattr(run_eval, "call_live_model", lambda model, prompt: "P1 (Critical)")
    res = run_eval.run_evaluation(models=["local-qwen3.5"], verbose=False)
    assert res["is_measured"] is False and res["mode"] == "live_api"
    assert res["responses_live"] is True and res["simulated_items"] == 0
    assert res["score_provenance"] == "estimated"
    assert res["score_scope"] == "keyword-and-format-proxy; semantic-quality-unreviewed"
    details = res["models"]["local-qwen3.5"]["details"]
    assert details and all(item["response_source"] == "live" and item["score_provenance"] == "estimated"
                           for item in details)


def test_mock_results_are_not_treated_as_measured(tmp_path):
    res = run_eval.run_evaluation(models=["nemotron-ultra-3"], force_mock=True, verbose=False)
    assert res["is_measured"] is False
    path = tmp_path / "r.json"
    run_eval.save_results(res, path)
    assert get_model_accuracy("nemotron-ultra-3", eval_results_file=path) == 0.88  # assumed base
    assert get_model_accuracy("nemotron-ultra-3", eval_results_file=path, allow_simulated=True) != 0.88


def test_candidate_tradeoffs():
    cands = cb.enumerate_candidates(
        "s1", 1200, 300, base_utc_time=T0,
        models=("gemini-3-flash", "nemotron-ultra-3"),
        locations=("us-east", "eu-west", "in-south"),
        offsets=(0, 2, 4, 8, 12),
    )
    assert len(cands) == 2 * 3 * 5
    at = lambda m, loc, off: next(
        c for c in cands if (c.model, c.location, c.time_offset_hr) == (m, loc, off)
    )
    flash, nemo = at("gemini-3-flash", "us-east", 0), at("nemotron-ultra-3", "us-east", 0)
    assert flash.est_energy_kwh < nemo.est_energy_kwh
    assert flash.est_accuracy < nemo.est_accuracy
    assert flash.est_cost_usd == pytest.approx(1.2 * 0.0005 + 0.3 * 0.003)
    # cleaner grid => less carbon for identical work
    assert at("nemotron-ultra-3", "eu-west", 0).est_carbon_g < at("nemotron-ultra-3", "in-south", 0).est_carbon_g


def test_local_models_are_free_and_on_device():
    c = cb.estimate_candidate("s1", "local-qwen3.5", "local-gpu", 0, 1200, 300, base_utc_time=T0)
    assert c.est_cost_usd == 0.0
    assert c.est_carbon_g > 0


def test_candidate_roundtrip_and_validation():
    c = cb.estimate_candidate("s1", "gemini-3-flash-preview", "Dublin", 2, 500, 200, "qa", T0)
    assert (c.model, c.location) == ("gemini-3-flash", "eu-west")
    from shared.schema import Candidate

    assert Candidate.from_dict(c.to_dict()) == c
    with pytest.raises(ValueError):
        cb.estimate_candidate("s1", "gemini-3-flash", "us-east", -1)
    with pytest.raises(ValueError):
        cb.estimate_candidate("s1", "gemini-3-flash", "mars")


def test_tokenizer_factor_scales_tokens(monkeypatch):
    kw = dict(input_tokens=1000, output_tokens=1000, base_utc_time=T0)
    base = cb.estimate_candidate("s", "gemini-3-flash", "us-east", **kw)
    spec = MODEL_TABLE["gemini-3-flash"]
    monkeypatch.setitem(MODEL_TABLE, "gemini-3-flash", dataclasses.replace(spec, tokenizer_factor=1.3))
    scaled = cb.estimate_candidate("s", "gemini-3-flash", "us-east", **kw)
    off = cb.estimate_candidate("s", "gemini-3-flash", "us-east", apply_tokenizer_factor=False, **kw)
    assert scaled.est_cost_usd == pytest.approx(base.est_cost_usd * 1.3)
    assert scaled.est_energy_kwh == pytest.approx(base.est_energy_kwh * 1.3)
    assert off.est_cost_usd == pytest.approx(base.est_cost_usd)


def test_enumerate_uses_one_reference_time():
    a = cb.enumerate_candidates("s", base_utc_time=T0)
    b = cb.enumerate_candidates("s", base_utc_time=T0)
    assert a == b  # fully deterministic when base_utc_time is fixed
