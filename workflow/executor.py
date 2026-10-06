"""
workflow/executor.py
─────────────────────────────────────────────────────────────────────────────
Runs the 4-step support-ticket triage pipeline end-to-end:
    classify -> retrieve -> draft -> summarize

Models:
  - Local (always): qwen3.5:4b and llama3.2:3b via Ollama
  - Cloud (hybrid mode, when API keys are set): Gemini and/or NVIDIA NIM
    (see shared/availability.py)

Execution loop per step:
  1. Call schedule_step() to get a Candidate (model + deployment config + eco metrics).
  2. Log time_offset (for display/analysis only; no real deferral).
  3. Dispatch to the correct model_clients wrapper based on candidate.model.
  4. Write a JSONL record to results/<run_id>.jsonl (actual + scheduled data).

Run full pipeline:
    python -m workflow.executor --policy eco
    python -m workflow.executor --policy speed
    python -m workflow.executor --policy balanced
    python -m workflow.executor --policy baseline
    python -m workflow.executor --policy custom --weights '{"w_carbon":0.7,"w_cost":0.2,"w_latency":0.1,"w_energy":0.0,"w_accuracy":0.0}'

Run a single step (iterative dev mode):
    python -m workflow.executor --step classify --policy eco
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

# ── SCHEDULER INTEGRATION ─────────────────────────────────────────────────────
# Using the real scheduler from Person B (scheduler/optimizer.py).
from scheduler.optimizer import schedule_step
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from data.candidate_builder import LOCAL_LOCATIONS, LOCAL_MODELS
from data.model_table import MODEL_TABLE
from scheduler.candidate_gen import BASELINE_OFFSET, baseline_choice
from scheduler.data_adapter import build_candidate
from shared import availability
from shared.schema import Candidate, STEP_DEFINITIONS
from workflow.dag import STEP_IDS, get_next_steps
from workflow import model_clients
from workflow.model_clients import AuthError


# ── Results directory (project root, shared with Person B per doc line 61) ───
RESULTS_DIR = root_dir / "results"


# ── Replay mode helper ──────────────────────────────────────────────────────────

def _get_replay_data(step_id: str, model_name: str) -> Optional[dict]:
    """
    Look up the latest recorded actual_latency_ms and tokens for a step+model
    from existing results/ JSONL files.
    
    Returns a dict with latency_ms, input_tokens, output_tokens, text (empty),
    or None if no recording found.
    """
    if not RESULTS_DIR.exists():
        return None
    
    # Search through all JSONL files, newest first
    files = sorted(RESULTS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    
                    # Match step_id and model
                    if record.get("step_id") == step_id and record.get("model") == model_name:
                        # Found a matching record, return the recorded metrics
                        return {
                            "latency_ms": record.get("actual_latency_ms", 0),
                            "input_tokens": record.get("input_tokens", 0),
                            "output_tokens": record.get("output_tokens", 0),
                            "text": "",  # No text in replay mode
                            "model": model_name,
                        }
        except Exception:
            continue
    
    return None


# ── Per-step prompts: support-ticket triage pipeline ─────────────────────────
# Minimal but realistic prompts that produce meaningful output for each step.
# Keep short to save tokens during development.
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


# ── Model name mapping (Person A's IDs → Real model names) ──────────────────
# Person A's data layer uses abstract model IDs (haiku, sonnet, opus).
# Person C's model_clients use real model names.
MODEL_NAME_MAP = {
    # ── LOCAL MODELS ─────────────────────────────────────────────────────────
    "local-qwen3.5": "qwen3.5:4b",
    "local-llama3.2": "llama3.2:3b",
    "qwen3.5:4b": "qwen3.5:4b",
    "llama3.2:3b": "llama3.2:3b",
    
    # ── CLOUD MODELS (hybrid mode) ───────────────────────────────────────────
    "haiku": "gemini-3-flash",
    "sonnet": "nemotron-ultra-3",
    "opus": "gemini-3-flash",  # Was gemini-3-pro, now using flash (free tier)
}

# ── Model dispatcher ──────────────────────────────────────────────────────────

def _dispatch(candidate: Candidate, prompt: str, step_context: dict) -> dict:
    """
    Route to the correct model_clients wrapper based on candidate.model.
    Translates Person A's abstract model IDs to real API client names.
    Returns {text, latency_ms, input_tokens, output_tokens, model}.
    Raises RuntimeError with a clear message for unknown models.
    """
    # Translate abstract model ID to real API name
    abstract_model = candidate.model.strip().lower()
    real_model = MODEL_NAME_MAP.get(abstract_model, abstract_model)
    
    fn = model_clients.MODEL_REGISTRY.get(real_model)
    if fn is None:
        raise RuntimeError(
            f"[executor] No model client registered for '{candidate.model}' (mapped to '{real_model}'). "
            f"Registered models: {list(model_clients.MODEL_REGISTRY.keys())}"
        )
    return fn(prompt, step_context)


def _fallback_candidate(candidate: Candidate) -> Candidate:
    """Local model to retry with when a call fails.

    The other local model for a local pick; the most accurate local model for a cloud pick.
    Estimated exactly like a scheduled candidate, so logged metrics stay comparable.
    """
    model = max(
        (m for m in LOCAL_MODELS if m != candidate.model),
        key=lambda m: MODEL_TABLE[m].base_accuracy,
    )
    return build_candidate(candidate.step_id, model, LOCAL_LOCATIONS[0], candidate.time_offset_hr)


# ── Results logging ───────────────────────────────────────────────────────────

def _write_result(
    run_id: str,
    candidate: Candidate,
    model_result: dict,
    *,
    fallback_used: bool = False,
    original_model: Optional[str] = None,
    replay_used: bool = False,
) -> None:
    """
    Append one JSON record to results/<run_id>.jsonl after each step.

    Record schema:
      Scheduling decision fields  — from Candidate (scheduled estimates)
      Real measurement fields     — from the model API call (wall-clock truth)
      Run metadata                — run_id, timestamp_utc
      Resilience metadata         — fallback_used, original_model, replay_used
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        # ── Scheduling decision ───────────────────────────────────────────
        "step_id":           candidate.step_id,
        "model":             candidate.model,
        "location":          candidate.location,
        "time_offset":       candidate.time_offset_hr * 3600,  # convert hours to seconds for backward compat
        "cost":              candidate.est_cost_usd,
        "energy":            candidate.est_energy_kwh * 1000,  # convert kWh to Wh for backward compat
        "carbon":            candidate.est_carbon_g,
        "latency_ms":        candidate.est_latency_ms,         # scheduler estimate
        "accuracy_estimate": candidate.est_accuracy,
        # policy and custom_weights not in new schema
        # ── Real measurements from model call ────────────────────────────
        "actual_latency_ms": model_result["latency_ms"],  # wall-clock truth
        "input_tokens":      model_result["input_tokens"],
        "output_tokens":     model_result["output_tokens"],
        # ── Metadata ─────────────────────────────────────────────────────
        "run_id":            run_id,
        "timestamp":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timestamp_utc":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # ── Resilience metadata ──────────────────────────────────────────
        "fallback_used":     fallback_used,
        "original_model":    original_model,
        "replay_used":       replay_used,
    }
    out_path = RESULTS_DIR / f"{run_id}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  [logged] results/{run_id}.jsonl "
          f"actual_latency={record['actual_latency_ms']}ms  "
          f"tokens(in={record['input_tokens']} out={record['output_tokens']})"
          f"{' [FALLBACK]' if fallback_used else ''}"
          f"{' [REPLAY]' if replay_used else ''}")


# ── execute_step: single step, fully end-to-end ───────────────────────────────

def execute_step(
    step_id: str,
    run_id: str,
    policy: str,
    custom_weights: Optional[dict] = None,
    replay_mode: bool = False,
) -> dict:
    """
    Execute one pipeline step:
      1. Schedule  → get Candidate from schedule_step()
      2. Log       → record time_offset (simulation only, no real wait)
      3. Dispatch  → call the model (on failure, retry once with a local model)
      4. Log       → write JSONL record to results/<run_id>.jsonl

    Returns {"candidate": Candidate, "model_result": dict, "output_text": str}
    """
    step_def = next(s for s in STEP_DEFINITIONS if s["step_id"] == step_id)
    step_context = {
        "step_id":   step_id,
        "task_type": step_def["task_type"],
        "sla_ms":    step_def["sla_ms"],
        "policy":    policy,
        "run_id":    run_id,
    }
    prompt = _STEP_PROMPTS[step_id]

    # 1. Schedule
    print(f"\n[{step_id}] Scheduling  policy={policy} ...")
    candidate = schedule_step(step_id, policy, custom_weights)
    sla_met = candidate.est_latency_ms <= step_def["sla_ms"]
    print(
        f"  -> model={candidate.model}  region={candidate.location}  "
        f"time_offset={candidate.time_offset_hr}h\n"
        f"     est: cost=${candidate.est_cost_usd:.5f}  energy={candidate.est_energy_kwh*1000:.2f}Wh  "
        f"carbon={candidate.est_carbon_g:.2f}gCO2e  "
        f"latency={candidate.est_latency_ms:.0f}ms  "
        f"accuracy={candidate.est_accuracy:.3f}  sla={'OK' if sla_met else 'MISS'}"
    )

    # 2. Log time offset (simulation only — no real sleep for live demo)
    if candidate.time_offset_hr > 0:
        print(f"  [scheduled with {candidate.time_offset_hr}h offset for lower-carbon grid window (simulation)]")

    # 3. Dispatch with fallback
    original_model = candidate.model
    fallback_used = False
    replay_used = False
    model_result = None

    def _try_dispatch(cand: Candidate) -> dict:
        """Try to dispatch to a model, returning result or raising."""
        return _dispatch(cand, prompt, step_context)

    # In replay mode, try to get recorded data first
    if replay_mode:
        replay_result = _get_replay_data(step_id, candidate.model)
        if replay_result is not None:
            print(f"  [REPLAY] Using recorded data for {candidate.model} on step {step_id}")
            model_result = replay_result
            replay_used = True
        else:
            print(f"  [REPLAY] No recording found for {candidate.model} on step {step_id}, falling back to real call")

    # If not in replay mode or no replay data, make real API call
    if model_result is None:
        try:
            print(f"  Calling {candidate.model} ...")
            model_result = _try_dispatch(candidate)
        except (RuntimeError, AuthError) as exc:
            # Check if it's an auth error - fail fast
            if isinstance(exc, AuthError) or "authentication" in str(exc).lower() or "authorization" in str(exc).lower() or "401" in str(exc) or "403" in str(exc):
                print(f"  [ERROR] Authentication failed: {exc}")
                raise
            
            print(f"  [ERROR] Model call failed: {exc}")
            fallback_candidate = _fallback_candidate(candidate)
            print(f"  [FALLBACK] Retrying step {step_id} with {fallback_candidate.model}")
            fallback_used = True
            
            try:
                model_result = _try_dispatch(fallback_candidate)
                # Update candidate to reflect fallback model for logging
                candidate = fallback_candidate
            except Exception as fallback_exc:
                print(f"  [ERROR] Fallback also failed: {fallback_exc}")
                raise RuntimeError(f"Both primary model ({original_model}) and fallback ({fallback_candidate.model}) failed") from fallback_exc

    print(
        f"  [done]  actual_latency={model_result['latency_ms']}ms  "
        f"tokens(in={model_result['input_tokens']} out={model_result['output_tokens']})"
        f"{' [FALLBACK]' if fallback_used else ''}"
        f"{' [REPLAY]' if replay_used else ''}"
    )

    # 4. Log
    _write_result(
        run_id, 
        candidate, 
        model_result,
        fallback_used=fallback_used,
        original_model=original_model if fallback_used else None,
        replay_used=replay_used,
    )

    return {
        "candidate":    candidate,
        "model_result": model_result,
        "output_text":  model_result["text"],
    }


# ── Baseline Candidate factory ────────────────────────────────────────────────
# "Always biggest model, same location, now" — per the role spec.
# No scheduling logic invoked; estimated exactly like scheduled candidates.

def _make_baseline_candidate(step_id: str) -> Candidate:
    """Baseline: most accurate available model, its fixed location, no deferral."""
    model, region = baseline_choice()
    return build_candidate(step_id, model, region, BASELINE_OFFSET)


# ── run_pipeline: full 4-step pipeline ───────────────────────────────────────

def run_pipeline(
    policy: str = "balanced",
    custom_weights: Optional[dict] = None,
    replay_mode: bool = False,
) -> str:
    """
    Run all 4 steps in DAG order: classify -> retrieve -> draft -> summarize.
    Results written to results/<run_id>.jsonl.
    Returns run_id (use to find the results file or pass to the dashboard).

    policy values:
      "eco"      — scheduler optimized for low-carbon routing
      "speed"    — scheduler optimized for lowest-latency routing
      "balanced" — scheduler optimized for best-quality-per-task routing
      "custom"   — scheduler with custom_weights dict
      "baseline" — NO scheduler: always the most accurate available model,
                   fixed location, time_offset=0
                   (the worst-case reference run for the comparison chart)
    
    replay_mode: If True, skip real API calls and reuse latest recorded
                 actual_latency_ms/tokens for that step+model from results/.
                 If no recording exists, fall back to a real call.
    """
    is_baseline = (policy == "baseline")
    run_id_prefix = "baseline_" if is_baseline else ""
    run_id = run_id_prefix + uuid.uuid4().hex[:12]

    print("=" * 68)
    print(f"run_pipeline  run_id={run_id}  policy={policy}")
    print(f"  [MODE] {availability.mode_label()}")
    if is_baseline:
        model, region = baseline_choice()
        print(f"  [BASELINE] always {model} / {region} / time_offset=0")
    if custom_weights:
        print(f"  custom_weights = {custom_weights}")
    if replay_mode:
        print(f"  [REPLAY MODE] Using recorded data where available")
    print("=" * 68)

    completed: set[str] = set()
    step_results: dict[str, dict] = {}

    while len(completed) < len(STEP_IDS):
        ready = get_next_steps(completed)
        if not ready:
            raise RuntimeError(
                f"DAG stalled — no ready steps. completed={completed}"
            )
        # Linear DAG: one step ready at a time.
        # Loop structure supports future fan-out if DAG is widened.
        for step_id in ready:
            if is_baseline:
                result = _execute_baseline_step(step_id, run_id, replay_mode=replay_mode)
            else:
                result = execute_step(step_id, run_id, policy, custom_weights, replay_mode=replay_mode)
            step_results[step_id] = result
            completed.add(step_id)

    # ── Pipeline summary ──────────────────────────────────────────────────────
    total_cost    = sum(r["candidate"].est_cost_usd          for r in step_results.values())
    total_energy  = sum(r["candidate"].est_energy_kwh * 1000 for r in step_results.values())  # kWh -> Wh
    total_carbon  = sum(r["candidate"].est_carbon_g          for r in step_results.values())
    total_latency = sum(r["model_result"]["latency_ms"]      for r in step_results.values())

    print("\n" + "=" * 68)
    print(f"Pipeline complete  run_id={run_id}  policy={policy}")
    print(f"  Scheduled totals (estimates):")
    print(f"    cost    = ${total_cost:.5f}")
    print(f"    energy  = {total_energy:.3f} Wh")
    print(f"    carbon  = {total_carbon:.3f} gCO2e")
    print(f"  Actual total latency = {total_latency:.0f} ms")
    print(f"  Results file: results/{run_id}.jsonl")
    print("=" * 68)

    return run_id


# ── _execute_baseline_step: baseline path (no scheduler) ─────────────────────

def _execute_baseline_step(step_id: str, run_id: str, replay_mode: bool = False) -> dict:
    """
    Execute one step in baseline mode:
      1. Build the baseline Candidate (see _make_baseline_candidate)
      2. Dispatch to real model client — NO deferral, NO scheduling
      3. Log to results/<run_id>.jsonl
    
    Returns {"candidate": Candidate, "model_result": dict, "output_text": str}
    """
    step_def = next(s for s in STEP_DEFINITIONS if s["step_id"] == step_id)
    step_context = {
        "step_id":   step_id,
        "task_type": step_def["task_type"],
        "sla_ms":    step_def["sla_ms"],
        "policy":    "baseline",
        "run_id":    run_id,
    }
    prompt    = _STEP_PROMPTS[step_id]
    candidate = _make_baseline_candidate(step_id)

    print(
        f"\n[{step_id}] BASELINE: model={candidate.model}  "
        f"region={candidate.location}  time_offset=0h\n"
        f"     est: cost=${candidate.est_cost_usd:.5f}  energy={candidate.est_energy_kwh*1000:.2f}Wh  "
        f"carbon={candidate.est_carbon_g:.2f}gCO2e  latency={candidate.est_latency_ms:.0f}ms  "
        f"accuracy={candidate.est_accuracy:.3f}"
    )

    fallback_used = False
    replay_used = False
    model_result = None
    original_model = candidate.model

    # In replay mode, try to get recorded data first
    if replay_mode:
        replay_result = _get_replay_data(step_id, candidate.model)
        if replay_result is not None:
            print(f"  [REPLAY] Using recorded data for {candidate.model} on step {step_id}")
            model_result = replay_result
            replay_used = True
        else:
            print(f"  [REPLAY] No recording found for {candidate.model} on step {step_id}, falling back to real call")

    # If not in replay mode or no replay data, make real API call
    if model_result is None:
        try:
            print(f"  Calling {candidate.model} ...")
            model_result = _dispatch(candidate, prompt, step_context)
        except (RuntimeError, AuthError) as exc:
            # Check if it's an auth error - fail fast
            if isinstance(exc, AuthError) or "authentication" in str(exc).lower() or "authorization" in str(exc).lower() or "401" in str(exc) or "403" in str(exc):
                print(f"  [ERROR] Authentication failed: {exc}")
                raise
            
            print(f"  [ERROR] Model call failed: {exc}")
            fallback_candidate = _fallback_candidate(candidate)
            print(f"  [FALLBACK] Retrying step {step_id} with {fallback_candidate.model}")
            fallback_used = True
            
            try:
                model_result = _dispatch(fallback_candidate, prompt, step_context)
                candidate = fallback_candidate
            except Exception as fallback_exc:
                print(f"  [ERROR] Fallback also failed: {fallback_exc}")
                raise RuntimeError(f"Both primary model ({original_model}) and fallback ({fallback_candidate.model}) failed") from fallback_exc

    print(
        f"  [done]  actual_latency={model_result['latency_ms']}ms  "
        f"tokens(in={model_result['input_tokens']} out={model_result['output_tokens']})"
        f"{' [FALLBACK]' if fallback_used else ''}"
        f"{' [REPLAY]' if replay_used else ''}"
    )
    _write_result(
        run_id, 
        candidate, 
        model_result,
        fallback_used=fallback_used,
        original_model=original_model if fallback_used else None,
        replay_used=replay_used,
    )

    return {
        "candidate":    candidate,
        "model_result": model_result,
        "output_text":  model_result["text"],
    }





# ── __main__ CLI ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the eco-aware support-ticket triage pipeline."
    )
    parser.add_argument(
        "--policy",
        default="balanced",
        choices=["eco", "balanced", "speed", "custom", "baseline"],
        help="Scheduling policy (default: balanced). Use 'baseline' for always-pro/us-central1.",
    )
    parser.add_argument(
        "--weights",
        default=None,
        help=(
            'JSON string for custom policy weights, e.g. '
            '\'{"carbon":0.7,"cost":0.2,"latency":0.1,"accuracy":0.0}\''
        ),
    )
    parser.add_argument(
        "--step",
        default=None,
        choices=STEP_IDS,
        help="Run only a single step (default: run full pipeline).",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="Enable replay mode (skip API calls, use recorded data). Can also be set via DEMO_REPLAY=1 env var.",
    )
    args = parser.parse_args()

    custom_weights: Optional[dict] = None
    if args.weights:
        try:
            custom_weights = json.loads(args.weights)
        except json.JSONDecodeError as e:
            print(f"[ERROR] --weights is not valid JSON: {e}")
            sys.exit(1)

    # Check for replay mode from env var or CLI flag
    replay_mode = args.replay or os.environ.get("DEMO_REPLAY") == "1"
    if replay_mode:
        print("[REPLAY MODE] DEMO_REPLAY=1 or --replay flag detected. Will reuse recorded data where available.")

    if args.step:
        print(f"Single-step mode: '{args.step}'  policy={args.policy}")
        test_run_id = "dev_" + uuid.uuid4().hex[:8]
        if args.policy == "baseline":
            _execute_baseline_step(args.step, test_run_id, replay_mode=replay_mode)
        else:
            execute_step(args.step, test_run_id, args.policy, custom_weights, replay_mode=replay_mode)
        print(f"\nResult written to results/{test_run_id}.jsonl")
    else:
        run_pipeline(policy=args.policy, custom_weights=custom_weights, replay_mode=replay_mode)
