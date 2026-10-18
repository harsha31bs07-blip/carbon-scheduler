"""Execute the support-ticket demo with versioned evidence and strict replay."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import time
import uuid

from data.candidate_builder import LOCAL_LOCATIONS, LOCAL_MODELS
from data.model_table import MODEL_TABLE
from scheduler.candidate_gen import baseline_choice
from scheduler.data_adapter import build_candidate
from scheduler.optimizer import schedule_step
from scheduler.policies import get_policy_weights
from shared import availability
from shared.run_records import (
    RunContext, calibration_id, code_revision, reference_time, utc_now,
    validate_id, workload_id, write_json,
)
from shared.schema import Candidate, STEP_DEFINITIONS
from workflow.dag import STEP_IDS, get_next_steps
from workflow import model_clients
from workflow.model_clients import AuthError
from workflow.replay import ReplayError, load_replay_run

root_dir = Path(__file__).resolve().parent.parent
RESULTS_DIR = root_dir / "results" / "runs"
REPLAY_DIR = root_dir / "results" / "demo"

_STEP_PROMPTS: dict[str, str] = {
    "classify": (
        "Classify this support ticket into exactly one category.\n"
        "Ticket: 'My account is locked and I cannot reset my password.'\n"
        "Categories: billing | account_access | technical_issue | feature_request | other\n"
        "Reply with ONLY the category label, nothing else."
    ),
    "retrieve": (
        "You are a support knowledge-base retrieval engine.\n"
        "Query: 'locked account password reset steps'\n"
        "Return exactly 3 numbered troubleshooting steps a support agent should follow. "
        "Be concise — one sentence each."
    ),
    "draft": (
        "You are a customer support agent.\n"
        "Customer issue: 'My account is locked and I cannot reset my password.'\n"
        "Relevant KB steps: "
        "1. Verify the email on file. "
        "2. Use the 'Forgot Password' flow. "
        "3. Check spam folder for the reset link.\n"
        "Draft a friendly, professional reply in under 80 words."
    ),
    "summarize": (
        "Summarize this support interaction for the CRM log in exactly one sentence.\n"
        "Interaction: Customer reported a locked account. "
        "Category: account_access. "
        "KB steps retrieved for password reset. "
        "Agent drafted reply directing customer to the Forgot Password flow."
    ),
}


MODEL_NAME_MAP = {
    "local-qwen3.5": "qwen3.5:4b",
    "local-llama3.2": "llama3.2:3b",
    "qwen3.5:4b": "qwen3.5:4b",
    "llama3.2:3b": "llama3.2:3b",
}


def _context(policy, custom_weights=None, *, comparison_id=None, base_utc_time=None,
             replay=None):
    if policy != "baseline":
        get_policy_weights(policy, custom_weights)  # validate before model calls
    if comparison_id is not None:
        validate_id(comparison_id)
    ref = reference_time(base_utc_time).isoformat().replace("+00:00", "Z")
    context = RunContext(
        policy, custom_weights, workload_id(_STEP_PROMPTS), comparison_id,
        availability.mode_label(), ref, calibration_id(), "live", code_revision(),
    )
    if replay is not None:
        source = replay["manifest"]
        context = replace(
            context, provider_mode=source["provider_mode"],
            reference_utc_time=source["reference_utc_time"],
            calibration_id=source["calibration_id"], execution_mode="replay",
        )
    return context


def _dispatch(candidate: Candidate, prompt: str, step_context: dict) -> dict:
    abstract = candidate.model.strip().lower()
    model = MODEL_NAME_MAP.get(abstract, abstract)
    client = model_clients.MODEL_REGISTRY.get(model)
    if client is None:
        raise RuntimeError(f"No model client registered for '{candidate.model}'")
    return client(prompt, step_context)


def _fallback_candidate(candidate: Candidate, base_utc_time=None) -> Candidate:
    model = max((m for m in LOCAL_MODELS if m != candidate.model),
                key=lambda m: MODEL_TABLE[m].base_accuracy)
    return build_candidate(candidate.step_id, model, LOCAL_LOCATIONS[0],
                           candidate.time_offset_hr, base_utc_time)


def _make_baseline_candidate(step_id: str, base_utc_time=None) -> Candidate:
    model, region = baseline_choice()
    return build_candidate(step_id, model, region, 0, base_utc_time)


def _result_from_record(record: dict) -> dict:
    result = {
        "text": record["output_text"], "latency_ms": record["actual_latency_ms"],
        "input_tokens": record["input_tokens"], "output_tokens": record["output_tokens"],
        "model": record.get("executed_model_id", record["model"]),
        "token_source": record.get("provenance", {}).get("tokens", "unknown"),
    }
    if record.get("execution_energy_wh") is not None:
        result.update(energy_wh=record["execution_energy_wh"],
                      energy_measured=record.get("energy_measured", False),
                      energy_method=record.get("energy_method", "unknown"))
    return result


def _get_replay_data(step_id: str, model_name: str, *, replay_dir=None,
                     policy="balanced", custom_weights=None) -> dict | None:
    """Look up an explicitly pinned recording; never search generated runs."""
    directory = Path(replay_dir) if replay_dir is not None else REPLAY_DIR
    if not (directory / "manifest.json").exists():
        return None
    replay = load_replay_run(directory, policy, custom_weights, workload_id(_STEP_PROMPTS))
    record = replay["records"].get(step_id)
    if record is None or record["model"] != model_name:
        return None
    return _result_from_record(record)


def _write_result(run_id, candidate, model_result, *, context=None,
                  scheduled_candidate=None, fallback_used=False, original_model=None,
                  replay_used=False, source_record=None, attempts=None,
                  execution_latency_ms=None):
    validate_id(run_id)
    context = context or replace(_context("balanced"), policy="unknown")
    scheduled_candidate = scheduled_candidate or candidate
    step = next(s for s in STEP_DEFINITIONS if s["step_id"] == candidate.step_id)
    provenance = {
        "latency": "replayed" if replay_used else "measured",
        "tokens": model_result.get("token_source", "unknown"),
        "cost": "estimated", "energy": "estimated",
        "carbon": "estimated" if candidate.model in LOCAL_MODELS else "projected",
        "accuracy": "estimated",
        "execution_energy": ("client_reported_measurement" if model_result.get("energy_measured")
                             else "estimated") if "energy_wh" in model_result else "unavailable",
    }
    record = {
        **context.to_dict(), "run_id": run_id, "status": "completed",
        "timestamp": utc_now(), "timestamp_utc": utc_now(),
        "step_id": candidate.step_id, "model": candidate.model,
        "location": candidate.location, "time_offset": candidate.time_offset_hr * 3600,
        "cost": candidate.est_cost_usd, "energy": candidate.est_energy_kwh * 1000,
        "carbon": candidate.est_carbon_g, "latency_ms": candidate.est_latency_ms,
        "accuracy_estimate": candidate.est_accuracy,
        "scheduled_candidate": scheduled_candidate.to_dict(),
        "executed_candidate": candidate.to_dict(),
        "executed_model_id": model_result.get("model", MODEL_NAME_MAP.get(candidate.model, candidate.model)),
        "executed_location": "local-gpu" if candidate.model in LOCAL_MODELS else None,
        "location_is_assumed": candidate.model not in LOCAL_MODELS,
        "actual_deferral_seconds": 0,
        "actual_latency_ms": model_result["latency_ms"],
        "execution_latency_ms": (execution_latency_ms if execution_latency_ms is not None
                                 else model_result["latency_ms"]),
        "input_tokens": model_result["input_tokens"], "output_tokens": model_result["output_tokens"],
        "output_text": model_result["text"],
        "execution_energy_wh": model_result.get("energy_wh"),
        "energy_measured": model_result.get("energy_measured", False),
        "energy_method": model_result.get("energy_method"),
        "provenance": provenance, "fallback_used": fallback_used,
        "original_model": original_model, "replay_used": replay_used,
        "attempts": attempts or [],
        "estimated_sla_met": candidate.est_latency_ms <= step["sla_ms"],
        "observed_sla_met": (execution_latency_ms if execution_latency_ms is not None
                             else model_result["latency_ms"]) <= step["sla_ms"],
        "estimated_accuracy_met": candidate.est_accuracy >= step["min_accuracy"],
    }
    if source_record:
        record["replay_source_run_id"] = source_record["run_id"]
        record["replay_source_timestamp"] = source_record["timestamp_utc"]
    payload = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with (RESULTS_DIR / f"{run_id}.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
    print(f"  [{candidate.step_id}] {candidate.model}: {record['actual_latency_ms']:.0f} ms"
          f"{' [REPLAY]' if replay_used else ''}{' [FALLBACK]' if fallback_used else ''}")
    return record


def _run_candidate(candidate, run_id, context, *, source_record=None):
    """One dispatch/fallback/logging path for baseline and scheduled steps."""
    original = candidate
    step = next(s for s in STEP_DEFINITIONS if s["step_id"] == candidate.step_id)
    step_context = {"step_id": candidate.step_id, "task_type": step["task_type"],
                    "sla_ms": step["sla_ms"], "policy": context.policy, "run_id": run_id}
    attempts = []
    if source_record is not None:
        candidate = Candidate.from_dict(source_record["executed_candidate"])
        result = _result_from_record(source_record)
        elapsed = source_record["execution_latency_ms"]
        attempts = source_record.get("attempts", [])
        fallback_used = source_record.get("fallback_used", False)
        original_model = source_record.get("original_model")
    else:
        if candidate.time_offset_hr != 0:
            raise ValueError("Live execution cannot defer; use the scheduler scenario preview")
        started = time.perf_counter()
        try:
            result = _dispatch(candidate, _STEP_PROMPTS[candidate.step_id], step_context)
            attempts.append({"model": candidate.model, "status": "completed"})
        except AuthError:
            raise
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            attempts.append({"model": candidate.model, "status": "failed",
                             "error_type": type(exc).__name__})
            candidate = _fallback_candidate(candidate, reference_time(context.reference_utc_time))
            try:
                result = _dispatch(candidate, _STEP_PROMPTS[candidate.step_id], step_context)
            except AuthError:
                raise
            except (RuntimeError, ConnectionError, TimeoutError) as fallback_exc:
                raise RuntimeError(
                    f"Primary {original.model} and fallback {candidate.model} both failed"
                ) from fallback_exc
            attempts.append({"model": candidate.model, "status": "completed"})
        elapsed = (time.perf_counter() - started) * 1000
        fallback_used = candidate.model != original.model
        original_model = original.model if fallback_used else None
    _write_result(
        run_id, candidate, result, context=context, scheduled_candidate=original,
        fallback_used=fallback_used, original_model=original_model,
        replay_used=source_record is not None, source_record=source_record,
        attempts=attempts, execution_latency_ms=elapsed,
    )
    return {"candidate": candidate, "model_result": result, "output_text": result["text"]}


def execute_step(step_id, run_id, policy, custom_weights=None, replay_mode=False, *,
                 replay_dir=None, context=None, replay=None):
    validate_id(run_id)
    if step_id not in STEP_IDS:
        raise ValueError(f"Unknown step_id: {step_id}")
    if replay_mode and replay is None:
        replay = load_replay_run(Path(replay_dir) if replay_dir else REPLAY_DIR,
                                 policy, custom_weights, workload_id(_STEP_PROMPTS))
    context = context or _context(policy, custom_weights, replay=replay)
    if replay is not None:
        record = replay["records"][step_id]
        candidate = Candidate.from_dict(record["scheduled_candidate"])
        return _run_candidate(candidate, run_id, context, source_record=record)
    ref = reference_time(context.reference_utc_time)
    candidate = (_make_baseline_candidate(step_id, ref) if policy == "baseline" else
                 schedule_step(step_id, policy, custom_weights, base_utc_time=ref, execution_only=True))
    return _run_candidate(candidate, run_id, context)


def _execute_baseline_step(step_id, run_id, replay_mode=False, **kwargs):
    return execute_step(step_id, run_id, "baseline", replay_mode=replay_mode, **kwargs)


def run_pipeline(policy="balanced", custom_weights=None, replay_mode=False, *,
                 replay_dir=None, comparison_id=None, base_utc_time=None):
    replay = (load_replay_run(Path(replay_dir) if replay_dir else REPLAY_DIR,
                              policy, custom_weights, workload_id(_STEP_PROMPTS))
              if replay_mode else None)
    context = _context(policy, custom_weights, comparison_id=comparison_id,
                       base_utc_time=base_utc_time, replay=replay)
    run_id = ("baseline_" if policy == "baseline" else "") + uuid.uuid4().hex[:12]
    manifest_path = RESULTS_DIR / f"{run_id}.manifest.json"
    manifest = {**context.to_dict(), "run_id": run_id, "status": "running",
                "expected_steps": STEP_IDS, "started_at": utc_now()}
    write_json(manifest_path, manifest)
    print(f"run_pipeline {run_id} / {policy} / {context.execution_mode} / {context.provider_mode}")
    completed = set()
    try:
        while len(completed) < len(STEP_IDS):
            ready = get_next_steps(completed)
            if not ready:
                raise RuntimeError("DAG stalled")
            for step_id in ready:
                execute_step(step_id, run_id, policy, custom_weights, replay_mode,
                             replay_dir=replay_dir, context=context, replay=replay)
                completed.add(step_id)
    except BaseException as exc:
        write_json(manifest_path, {**manifest, "status": "failed", "finished_at": utc_now(),
                                  "completed_steps": sorted(completed),
                                  "error_type": type(exc).__name__})
        raise
    write_json(manifest_path, {**manifest, "status": "completed", "finished_at": utc_now(),
                              "completed_steps": STEP_IDS})
    print(f"Results: {RESULTS_DIR / (run_id + '.jsonl')}")
    return run_id


def run_comparison(policy="balanced", custom_weights=None, replay_mode=False, *, replay_dir=None):
    """Run one explicit baseline/scheduled pair with matching experiment context."""
    if policy == "baseline":
        raise ValueError("Choose a scheduled policy to compare with baseline")
    get_policy_weights(policy, custom_weights)
    comparison_id = uuid.uuid4().hex[:12]
    ref = reference_time()
    if replay_mode:
        directory = Path(replay_dir) if replay_dir else REPLAY_DIR
        baseline = load_replay_run(directory, "baseline", None, workload_id(_STEP_PROMPTS))
        scheduled = load_replay_run(directory, policy, custom_weights, workload_id(_STEP_PROMPTS))
        for key in ("comparison_id", "workload_id", "provider_mode", "reference_utc_time", "calibration_id"):
            if not baseline["manifest"].get(key) or baseline["manifest"][key] != scheduled["manifest"].get(key):
                raise ReplayError(f"Replay comparison has incompatible {key}")
    baseline_id = run_pipeline("baseline", replay_mode=replay_mode, replay_dir=replay_dir,
                               comparison_id=comparison_id, base_utc_time=ref)
    scheduled_id = run_pipeline(policy, custom_weights, replay_mode, replay_dir=replay_dir,
                                comparison_id=comparison_id, base_utc_time=ref)
    return {"baseline_run_id": baseline_id, "scheduled_run_id": scheduled_id,
            "comparison_id": comparison_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", choices=["eco", "balanced", "speed", "custom", "baseline"],
                        default="balanced")
    parser.add_argument("--weights", help="JSON object with w_latency/w_cost/w_energy/w_carbon/w_accuracy")
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--replay", action="store_true", help="Strict replay: never makes model calls")
    parser.add_argument("--replay-dir", type=Path, default=REPLAY_DIR)
    parser.add_argument("--compare", action="store_true", help="Run a matched baseline and scheduled pair")
    args = parser.parse_args()
    weights = json.loads(args.weights) if args.weights else None
    replay = args.replay or os.environ.get("DEMO_REPLAY") == "1"
    if args.compare and args.step:
        parser.error("--compare requires the full workflow")
    if args.compare:
        run_comparison(args.policy, weights, replay, replay_dir=args.replay_dir)
    elif args.step:
        execute_step(args.step, "dev_" + uuid.uuid4().hex[:8], args.policy, weights, replay,
                     replay_dir=args.replay_dir)
    else:
        run_pipeline(args.policy, weights, replay, replay_dir=args.replay_dir)


if __name__ == "__main__":
    main()
