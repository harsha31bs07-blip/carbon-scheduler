"""
Test suite for the workflow module.

Tests cover:
- DAG step resolution and dependency checking
- schedule_step integration with the real scheduler
- Model dispatch (name mapping, unknown models)
- Results logging, replay mode, fallback, and auth fail-fast
- End-to-end pipeline execution (model calls faked; no Ollama/API needed)

Scheduler scoring/constraint logic is tested in tests/test_scheduler.py.
"""

import json
import os

import pytest

from data.candidate_builder import available_models, estimate_candidate
from shared.schema import Candidate, STEP_DEFINITIONS
from workflow import executor
from workflow.dag import STEP_IDS, DAG_EDGES, get_next_steps
from workflow.executor import (
    MODEL_NAME_MAP,
    _dispatch,
    _get_replay_data,
    _write_result,
    execute_step,
    run_pipeline,
    schedule_step,
)
from workflow.model_clients import AuthError, MODEL_REGISTRY


def fake_model_result(latency_ms=1234.0, text="ok"):
    return {"text": text, "latency_ms": latency_ms, "input_tokens": 50, "output_tokens": 20, "model": "fake"}


@pytest.fixture
def results_dir(tmp_path, monkeypatch):
    """Redirect executor output to a temp dir so tests never touch results/."""
    monkeypatch.setattr(executor, "RESULTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def dispatch_calls(monkeypatch):
    """Replace real model calls with a recorder that returns a canned result."""
    calls = []

    def fake_dispatch(candidate, prompt, step_context):
        calls.append(candidate)
        return fake_model_result()

    monkeypatch.setattr(executor, "_dispatch", fake_dispatch)
    return calls


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestDAGResolution:
    """Tests for DAG step resolution and dependency checking."""

    def test_step_ids_match_definitions(self):
        """STEP_IDS should match the step_ids in STEP_DEFINITIONS."""
        expected = [step["step_id"] for step in STEP_DEFINITIONS]
        assert STEP_IDS == expected
        assert STEP_IDS == ["classify", "retrieve", "draft", "summarize"]

    def test_dag_edges_correct(self):
        """DAG edges should form a linear chain: classify -> retrieve -> draft -> summarize."""
        assert DAG_EDGES == [
            ("classify", "retrieve"),
            ("retrieve", "draft"),
            ("draft", "summarize"),
        ]

    def test_get_next_steps_initial(self):
        """Initially, only classify should be ready (no prerequisites)."""
        assert get_next_steps(set()) == ["classify"]

    def test_get_next_steps_after_classify(self):
        assert get_next_steps({"classify"}) == ["retrieve"]

    def test_get_next_steps_after_retrieve(self):
        assert get_next_steps({"classify", "retrieve"}) == ["draft"]

    def test_get_next_steps_after_draft(self):
        assert get_next_steps({"classify", "retrieve", "draft"}) == ["summarize"]

    def test_get_next_steps_all_complete(self):
        assert get_next_steps({"classify", "retrieve", "draft", "summarize"}) == []

    def test_get_next_steps_partial_completion(self):
        """Readiness checks direct prerequisites only, so several steps can be ready at once."""
        assert get_next_steps({"classify", "draft"}) == ["retrieve", "summarize"]


class TestScheduleStepIntegration:
    """schedule_step as the executor sees it."""

    def test_schedule_step_returns_valid_candidate(self):
        for step_id in STEP_IDS:
            for policy in ["speed", "eco", "balanced"]:
                c = schedule_step(step_id, policy)
                assert isinstance(c, Candidate)
                assert c.step_id == step_id
                assert c.est_latency_ms > 0
                assert 0 < c.est_accuracy <= 1.0
                assert c.est_cost_usd >= 0
                assert c.est_energy_kwh > 0
                assert c.est_carbon_g > 0

    def test_scheduled_model_is_dispatchable(self):
        """Whatever the scheduler picks must map to a registered model client."""
        for step_id in STEP_IDS:
            for policy in ["speed", "eco", "balanced"]:
                model = schedule_step(step_id, policy).model
                assert MODEL_NAME_MAP.get(model, model) in MODEL_REGISTRY

    def test_every_candidate_model_is_dispatchable(self):
        for model in available_models():
            assert MODEL_NAME_MAP.get(model, model) in MODEL_REGISTRY, model

    def test_custom_policy(self):
        weights = {"w_latency": 0.4, "w_cost": 0.2, "w_energy": 0.1, "w_carbon": 0.2, "w_accuracy": 0.1}
        for step_id in STEP_IDS:
            assert schedule_step(step_id, "custom", weights).step_id == step_id


class TestDispatch:
    def test_maps_abstract_name_to_registered_client(self, monkeypatch):
        seen = {}

        def fake_client(prompt, ctx):
            seen["prompt"] = prompt
            return fake_model_result()

        monkeypatch.setitem(MODEL_REGISTRY, MODEL_NAME_MAP["local-qwen3.5"], fake_client)
        c = estimate_candidate("classify", "local-qwen3.5", "local-gpu", 0)
        assert _dispatch(c, "hello", {})["text"] == "ok"
        assert seen["prompt"] == "hello"

    def test_unknown_model_raises(self):
        c = Candidate("classify", "no-such-model", "local-gpu", 0, 1.0, 0.9, 0.0, 0.001, 0.1)
        with pytest.raises(RuntimeError, match="No model client registered"):
            _dispatch(c, "hello", {})


class TestResultsLogging:
    def test_write_result_appends_jsonl_record(self, results_dir):
        c = estimate_candidate("classify", "local-qwen3.5", "local-gpu", 2)
        _write_result("run1", c, fake_model_result(latency_ms=999))
        _write_result("run1", c, fake_model_result(latency_ms=888))

        records = read_records(results_dir / "run1.jsonl")
        assert [r["actual_latency_ms"] for r in records] == [999, 888]
        r = records[0]
        assert (r["step_id"], r["model"], r["location"]) == ("classify", "local-qwen3.5", "local-gpu")
        assert r["time_offset"] == 2 * 3600                   # stored in seconds
        assert r["energy"] == pytest.approx(c.est_energy_kwh * 1000)  # stored in Wh
        assert r["fallback_used"] is False and r["replay_used"] is False


class TestReplay:
    def _record(self, path, step_id, model, latency):
        path.write_text(
            json.dumps({"step_id": step_id, "model": model, "actual_latency_ms": latency,
                        "input_tokens": 10, "output_tokens": 5}) + "\n",
            encoding="utf-8",
        )

    def test_returns_none_without_recording(self, results_dir):
        assert _get_replay_data("classify", "local-qwen3.5") is None

    def test_newest_matching_file_wins(self, results_dir):
        old, new = results_dir / "old.jsonl", results_dir / "new.jsonl"
        self._record(old, "classify", "local-qwen3.5", 111)
        self._record(new, "classify", "local-qwen3.5", 222)
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        assert _get_replay_data("classify", "local-qwen3.5")["latency_ms"] == 222

    def test_replay_mode_skips_model_call(self, results_dir, dispatch_calls):
        model = schedule_step("classify", "balanced").model
        self._record(results_dir / "rec.jsonl", "classify", model, 4321)

        out = execute_step("classify", "run2", "balanced", replay_mode=True)
        assert dispatch_calls == []
        assert out["model_result"]["latency_ms"] == 4321
        assert read_records(results_dir / "run2.jsonl")[0]["replay_used"] is True


class TestFallback:
    def test_failed_call_falls_back_to_other_local_model(self, results_dir, monkeypatch):
        calls = []

        def flaky_dispatch(candidate, prompt, step_context):
            calls.append(candidate.model)
            if len(calls) == 1:
                raise RuntimeError("ollama timed out")
            return fake_model_result()

        monkeypatch.setattr(executor, "_dispatch", flaky_dispatch)
        out = execute_step("classify", "run3", "balanced")

        assert len(calls) == 2 and calls[0] != calls[1]
        assert out["candidate"].model == calls[1]
        record = read_records(results_dir / "run3.jsonl")[0]
        assert record["fallback_used"] is True
        assert record["original_model"] == calls[0]

    def test_auth_error_fails_fast(self, results_dir, monkeypatch):
        calls = []

        def auth_fail(candidate, prompt, step_context):
            calls.append(candidate.model)
            raise AuthError("bad key")

        monkeypatch.setattr(executor, "_dispatch", auth_fail)
        with pytest.raises(AuthError):
            execute_step("classify", "run4", "balanced")
        assert len(calls) == 1  # no fallback attempt


class TestRunPipeline:
    def test_runs_all_steps_in_dag_order(self, results_dir, dispatch_calls):
        run_id = run_pipeline("eco")
        records = read_records(results_dir / f"{run_id}.jsonl")
        assert [r["step_id"] for r in records] == STEP_IDS
        assert all(r["run_id"] == run_id for r in records)
        assert len(dispatch_calls) == len(STEP_IDS)

    def test_baseline_uses_fixed_model_without_scheduler(self, results_dir, dispatch_calls, monkeypatch):
        def no_scheduler(*a, **k):
            raise AssertionError("baseline must not call the scheduler")

        monkeypatch.setattr(executor, "schedule_step", no_scheduler)
        run_id = run_pipeline("baseline")
        assert run_id.startswith("baseline_")
        records = read_records(results_dir / f"{run_id}.jsonl")
        assert {(r["model"], r["location"], r["time_offset"]) for r in records} == {
            ("local-qwen3.5", "local-gpu", 0)
        }
