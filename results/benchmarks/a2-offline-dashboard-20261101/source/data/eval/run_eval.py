"""data/eval/run_eval.py

Evaluation harness that runs the benchmark prompt suite (eval_set.json) against the
models the scheduler can pick, through the same clients the pipeline uses
(workflow.model_clients: Ollama for local models, Gemini / NVIDIA NIM for cloud).

Live responses receive keyword/format proxy scores, not measured semantic
accuracy. --mock gives deterministic simulated scores. Actual text and rubric
metadata are retained for later human review. data.model_table does not promote
these heuristic scores to scheduling accuracy, even when every response is live.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EVAL_DIR = Path(__file__).parent
EVAL_SET_PATH = EVAL_DIR / "eval_set.json"
RESULTS_OUTPUT_PATH = EVAL_DIR / "eval_results.json"
SCORE_SOURCE = "keyword-format-proxy-v1"
SIMULATION_SCORE_SOURCE = "capability-assumption-simulation-v1"

# Simulation-only probabilities by [model][category][difficulty] (ASSUMPTIONS, used by
# --mock and --allow-fallback). Models without a profile use DEFAULT_SIMULATION_MODEL's.
DEFAULT_SIMULATION_MODEL = "gemini-3-flash"
SIMULATION_PROBABILITIES: Dict[str, Dict[str, Dict[str, float]]] = {
    "gemini-3-flash": {
        "classification": {"easy": 0.95, "medium": 0.85, "hard": 0.65},
        "qa": {"easy": 0.90, "medium": 0.78, "hard": 0.58},
        "generation": {"easy": 0.88, "medium": 0.72, "hard": 0.52},
        "summarization": {"easy": 0.92, "medium": 0.80, "hard": 0.60},
    },
    "nemotron-ultra-3": {
        "classification": {"easy": 0.98, "medium": 0.94, "hard": 0.82},
        "qa": {"easy": 0.96, "medium": 0.90, "hard": 0.78},
        "generation": {"easy": 0.95, "medium": 0.88, "hard": 0.76},
        "summarization": {"easy": 0.96, "medium": 0.91, "hard": 0.80},
    },
}


def load_eval_set(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load benchmark evaluation items from JSON."""
    target = path or EVAL_SET_PATH
    if not target.is_file():
        raise FileNotFoundError(f"Evaluation dataset not found at: {target}")
    with open(target, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Evaluation dataset must be a top-level JSON array.")
    return data


def call_live_model(model: str, prompt: str) -> str:
    """Run one prompt on the model through the pipeline's own client; returns the text."""
    from workflow.model_clients import call_model  # imported lazily: only needed for live runs

    return call_model(model, prompt)["text"]


def simulate_model_response(
    model: str, eval_item: Dict[str, Any]
) -> Tuple[str, float]:
    """Deterministically simulate model response and rubric score when running offline.

    Generates a score based on model capability curve, question difficulty, and prompt hash.
    """
    category = eval_item.get("category", "qa")
    difficulty = eval_item.get("difficulty", "medium")
    item_id = eval_item.get("id", "0")
    rubric = eval_item.get("rubric", {})
    ref_ans = eval_item.get("reference_answer", "")

    # Base target capability probability
    cat_profile = SIMULATION_PROBABILITIES.get(model, SIMULATION_PROBABILITIES[DEFAULT_SIMULATION_MODEL])
    prob = cat_profile.get(category, {}).get(difficulty, 0.75)

    # Deterministic hash variation [-0.04, +0.04] based on prompt ID and model
    seed = f"{model}:{item_id}:{category}"
    hash_val = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    variance = ((hash_val % 100) / 100.0 - 0.5) * 0.08

    final_score = max(0.20, min(1.0, prob + variance))

    # In mock mode, synthesize realistic candidate output text based on score
    if final_score >= 0.70:
        simulated_text = ref_ans
    else:
        # Generate partially correct or truncated response
        keywords = rubric.get("expected_keywords", [])
        retained = keywords[: max(1, len(keywords) // 2)]
        simulated_text = f"Partial response discussing {', '.join(retained)}."

    return simulated_text, round(final_score, 4)


def score_response(
    response_text: str, eval_item: Dict[str, Any]
) -> float:
    """Return a keyword/format proxy; this does not establish semantic quality."""
    rubric = eval_item.get("rubric", {})
    expected_keywords = rubric.get("expected_keywords", [])
    format_constraint = rubric.get("format_constraint", "text")
    if not isinstance(response_text, str) or not response_text:
        return 0.0

    lower_resp = response_text.lower()
    ref_lower = eval_item.get("reference_answer", "").lower()

    # Exact or near-exact match
    # Only "reference appears in response" counts; the reverse check let any substring
    # of the reference (e.g. "P" for "P1") score a perfect match.
    if ref_lower and ref_lower in lower_resp:
        keyword_score = 1.0
    elif expected_keywords:
        hits = sum(1 for kw in expected_keywords if kw.lower() in lower_resp)
        keyword_score = hits / len(expected_keywords)
    else:
        keyword_score = 0.8

    # Format conformance penalty/bonus
    format_score = 1.0
    if format_constraint == "valid_json":
        try:
            # Strip markdown code fences if wrapped
            clean = response_text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            json.loads(clean)
            format_score = 1.0
        except json.JSONDecodeError:
            format_score = 0.3
    elif format_constraint == "single_label":
        # Expect short concise output
        if len(response_text.split()) <= 6:
            format_score = 1.0
        else:
            format_score = 0.7
    elif format_constraint == "markdown_table":
        if "|" in response_text and "---" in response_text:
            format_score = 1.0
        else:
            format_score = 0.5

    composite = (keyword_score * 0.7) + (format_score * 0.3)
    return round(max(0.0, min(1.0, composite)), 4)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _output_validation(response: Any, item: dict) -> dict:
    """Check nonempty text and requested syntax; never assert answer correctness."""
    status = "format-valid"
    if not isinstance(response, str):
        status = "invalid-type"
    elif not response.strip():
        status = "empty"
    else:
        constraint = item.get("rubric", {}).get("format_constraint", "text")
        if constraint == "valid_json":
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            try:
                json.loads(clean)
            except (ValueError, TypeError):
                status = "invalid-format"
        elif constraint == "single_label" and len(response.split()) > 6:
            status = "invalid-format"
        elif constraint == "markdown_table" and not ("|" in response and "---" in response):
            status = "invalid-format"
    return {"output_valid": status == "format-valid", "output_validation_status": status,
            "output_validation_scope": "nonempty-text-and-requested-format",
            "semantic_quality_status": "unreviewed"}


def run_evaluation(
    models: Optional[List[str]] = None,
    eval_set_path: Optional[Path] = None,
    force_mock: bool = False,
    verbose: bool = True,
    allow_fallback: bool = False,
) -> Dict[str, Any]:
    """Execute the full evaluation suite across target models.

    models defaults to every model available in the current mode (local + configured cloud).
    """
    from data.candidate_builder import available_models

    target_models = models or list(available_models())
    items = load_eval_set(eval_set_path)

    is_live = not force_mock
    mode_label = "live_api" if is_live else "mock_simulation"
    fallback_count = 0

    if verbose:
        print("=" * 68)
        print("RUNNING KEYWORD/FORMAT PROXY EVALUATION HARNESS")
        print(f"Mode: {mode_label.upper()} | Prompts: {len(items)} | Models: {target_models}")
        print("=" * 68)

    results: Dict[str, Any] = {
        "schema_version": 2,
        "evaluation_timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode_label,
        "total_test_cases": len(items),
        "eval_set_fingerprint": _fingerprint(items),
        "score_source": SCORE_SOURCE if is_live else SIMULATION_SCORE_SOURCE,
        "score_provenance": "estimated" if is_live else "simulated",
        "score_scope": "keyword-and-format-proxy; semantic-quality-unreviewed",
        "is_measured": False,
        "models": {},
    }

    for model in target_models:
        if verbose:
            print(f"\nEvaluating Model: [{model.upper()}] ...")

        item_scores: List[float] = []
        category_scores: Dict[str, List[float]] = {}
        detail_records: List[Dict[str, Any]] = []

        for item in items:
            cat = item.get("category", "general")
            category_scores.setdefault(cat, [])

            latency_ms: Optional[float] = None
            if is_live:
                try:
                    t0 = time.perf_counter()
                    resp_text = call_live_model(model, item["prompt"])
                    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                    score = score_response(resp_text, item)
                    source = "live"
                except Exception as exc:
                    if not allow_fallback:
                        raise RuntimeError(
                            f"Live call failed for {model}/{item['id']} ({type(exc).__name__}). "
                            "Check Ollama / API keys, or re-run with --allow-fallback to substitute "
                            "simulated scores (results will then be flagged as NOT measured)."
                        ) from exc
                    if verbose:
                        print(f"  [Warn] Live call failed for {item['id']} ({type(exc).__name__}). Using simulated score.")
                    resp_text, score = simulate_model_response(model, item)
                    source = "simulated_fallback"
                    fallback_count += 1
            else:
                resp_text, score = simulate_model_response(model, item)
                source = "simulated"

            item_scores.append(score)
            category_scores[cat].append(score)

            detail_records.append({
                "item_id": item["id"],
                "category": cat,
                "difficulty": item.get("difficulty"),
                "score": score,
                "source": source,
                "response_source": source,
                "score_source": SCORE_SOURCE if source == "live" else SIMULATION_SCORE_SOURCE,
                "score_provenance": "estimated" if source == "live" else "simulated",
                "prompt": item["prompt"],
                "response": resp_text,
                "reference_answer": item.get("reference_answer", ""),
                "rubric": item.get("rubric", {}),
                "rubric_fingerprint": _fingerprint(item.get("rubric", {})),
                **_output_validation(resp_text, item),
                "latency_ms": latency_ms,
            })

        overall_accuracy = sum(item_scores) / max(1, len(item_scores))
        cat_accuracies = {
            c: round(sum(scores) / max(1, len(scores)), 4)
            for c, scores in category_scores.items()
        }

        lat = [d["latency_ms"] for d in detail_records if d["latency_ms"] is not None]
        results["models"][model] = {
            "accuracy": round(overall_accuracy, 4),
            "accuracy_source": SCORE_SOURCE if all(d["source"] == "live" for d in detail_records)
                               else SIMULATION_SCORE_SOURCE if all(d["source"] != "live" for d in detail_records)
                               else "mixed-live-proxy-and-simulation",
            "accuracy_provenance": "estimated" if all(d["source"] == "live" for d in detail_records)
                                   else "simulated" if all(d["source"] != "live" for d in detail_records)
                                   else "mixed",
            "accuracy_scope": "legacy-field-name-for-proxy-score; not-semantic-accuracy",
            "num_evaluated": len(item_scores),
            "category_accuracy": cat_accuracies,
            "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
            "details": detail_records,
        }

        if verbose:
            print(f"  -> Proxy score: {overall_accuracy * 100:.1f}%")
            for cat, acc in cat_accuracies.items():
                print(f"     * {cat:<15}: {acc * 100:.1f}%")

    results["simulated_items"] = fallback_count if is_live else len(items) * len(target_models)
    results["live_items"] = sum(d["source"] == "live" for model in results["models"].values()
                                for d in model["details"])
    results["responses_live"] = bool(results["live_items"] > 0 and results["simulated_items"] == 0)
    if is_live and fallback_count:
        results["mode"] = "live_api_partial"
        results["score_source"] = ("mixed-live-proxy-and-simulation" if results["live_items"]
                                   else SIMULATION_SCORE_SOURCE)
        results["score_provenance"] = "mixed" if results["live_items"] else "simulated"
    return results


def save_results(results: Dict[str, Any], output_path: Optional[Path] = None) -> Path:
    """Persist evaluation results to JSON file."""
    path = output_path or RESULTS_OUTPUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return path


def print_summary_table(results: Dict[str, Any], output_path: Optional[Path] = None) -> None:
    """Print clean ASCII comparison summary table."""
    print("\n" + "=" * 68)
    print(f"{'KEYWORD/FORMAT PROXY SCORE SUMMARY':^68}")
    print("=" * 68)
    print(f"{'Model':<12} | {'Overall':<10} | {'Classification':<15} | {'QA':<8} | {'Gen':<8} | {'Summ':<8}")
    print("-" * 68)

    for model, mdata in results.get("models", {}).items():
        overall = f"{mdata['accuracy'] * 100:.1f}%"
        cats = mdata.get("category_accuracy", {})
        c_acc = f"{cats.get('classification', 0.0) * 100:.1f}%"
        qa_acc = f"{cats.get('qa', 0.0) * 100:.1f}%"
        gen_acc = f"{cats.get('generation', 0.0) * 100:.1f}%"
        sum_acc = f"{cats.get('summarization', 0.0) * 100:.1f}%"
        print(f"{model:<12} | {overall:<10} | {c_acc:<15} | {qa_acc:<8} | {gen_acc:<8} | {sum_acc:<8}")

    print("=" * 68)
    print(f"Results written to: {output_path or RESULTS_OUTPUT_PATH}")
    if results.get("responses_live"):
        print("LIVE RESPONSES; ESTIMATED KEYWORD/FORMAT PROXY. Semantic quality remains unreviewed.")
    else:
        print("SIMULATED OR MIXED RESPONSES; scores do not establish semantic quality.")
    print("model_table keeps its assumed accuracy until an explicit human-reviewed rubric is supplied.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LLM model evaluation suite.")
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Models to evaluate (default: every model available in the current mode)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use deterministic simulated scores instead of calling the models (not treated as measured)",
    )
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=EVAL_SET_PATH,
        help="Custom path to eval_set.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=RESULTS_OUTPUT_PATH,
        help="Path to write output results JSON",
    )

    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="In live mode, substitute simulated scores when an API call fails (flags results as not measured)",
    )

    args = parser.parse_args()

    try:
        results = run_evaluation(
            models=args.models,
            eval_set_path=args.eval_set,
            force_mock=args.mock,
            verbose=True,
            allow_fallback=args.allow_fallback,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    save_results(results, args.output)
    print_summary_table(results, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
