"""
Baseline vs scheduled comparison.
Logs results to results/ directory.
"""
import csv
import json
from pathlib import Path

from shared.schema import Candidate
from scheduler.candidate_gen import STEPS, BASELINE_OFFSET, baseline_choice
from scheduler.data_adapter import build_candidate
from scheduler.optimizer import schedule_step


RESULTS_DIR = Path(__file__).parent.parent / "results"


def create_baseline_candidate(step_id: str) -> Candidate:
    """Create the baseline candidate: biggest available model, fixed location, no deferral."""
    model, region = baseline_choice()
    return build_candidate(step_id, model, region, BASELINE_OFFSET)


def compute_totals(candidates: list[Candidate]) -> dict:
    """Compute totals across a list of candidates."""
    return {
        "latency_ms": sum(c.est_latency_ms for c in candidates),
        "accuracy": sum(c.est_accuracy for c in candidates) / len(candidates) if candidates else 0.0,
        "cost_usd": sum(c.est_cost_usd for c in candidates),
        "energy_kwh": sum(c.est_energy_kwh for c in candidates),
        "carbon_g": sum(c.est_carbon_g for c in candidates),
    }


def percent_diff(baseline: float, scheduled: float) -> float:
    """Compute percent difference: (scheduled - baseline) / baseline * 100."""
    if baseline == 0:
        return 0.0
    return (scheduled - baseline) / baseline * 100.0


def run_baseline_comparison():
    """Run baseline vs scheduled comparison for all steps and policies."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    policies = ["speed", "balanced", "eco"]
    all_choices = []
    per_policy_scheduled = {p: [] for p in policies}
    baselines = []

    for step in STEPS:
        step_id = step["step_id"]
        baseline = create_baseline_candidate(step_id)
        baselines.append(baseline)

        for policy in policies:
            scheduled = schedule_step(step_id, policy)
            per_policy_scheduled[policy].append(scheduled)

            all_choices.append({
                "step": step_id,
                "policy": policy,
                "model": scheduled.model,
                "location": scheduled.location,
                "offset": scheduled.time_offset_hr,
                "est_latency_ms": round(scheduled.est_latency_ms, 2),
                "est_accuracy": round(scheduled.est_accuracy, 4),
                "est_cost_usd": round(scheduled.est_cost_usd, 6),
                "est_energy_kwh": round(scheduled.est_energy_kwh, 6),
                "est_carbon_g": round(scheduled.est_carbon_g, 4),
            })

    baseline_totals = compute_totals(baselines)

    csv_path = RESULTS_DIR / "scheduler_choices.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_choices[0].keys())
        writer.writeheader()
        writer.writerows(all_choices)

    comparison = {}
    for policy in policies:
        scheduled_totals = compute_totals(per_policy_scheduled[policy])
        comparison[policy] = {
            "baseline": {k: round(v, 4) for k, v in baseline_totals.items()},
            "scheduled": {k: round(v, 4) for k, v in scheduled_totals.items()},
            "percent_diff": {
                "latency_ms": round(percent_diff(baseline_totals["latency_ms"], scheduled_totals["latency_ms"]), 2),
                "accuracy": round(percent_diff(baseline_totals["accuracy"], scheduled_totals["accuracy"]), 2),
                "cost_usd": round(percent_diff(baseline_totals["cost_usd"], scheduled_totals["cost_usd"]), 2),
                "energy_kwh": round(percent_diff(baseline_totals["energy_kwh"], scheduled_totals["energy_kwh"]), 2),
                "carbon_g": round(percent_diff(baseline_totals["carbon_g"], scheduled_totals["carbon_g"]), 2),
            }
        }

    json_path = RESULTS_DIR / "baseline_vs_scheduled.json"
    with open(json_path, "w") as f:
        json.dump(comparison, f, indent=2)

    print_summary(baseline_totals, comparison)

    return comparison


def print_summary(baseline_totals: dict, comparison: dict):
    """Print a compact summary table to stdout."""
    print("\n" + "=" * 80)
    print("BASELINE vs SCHEDULED COMPARISON")
    print("=" * 80)
    model, region = baseline_choice()
    print(f"Baseline (model={model}, region={region}, offset={BASELINE_OFFSET}):")
    print(f"  Latency: {baseline_totals['latency_ms']:.2f} ms")
    print(f"  Accuracy: {baseline_totals['accuracy']:.4f}")
    print(f"  Cost: ${baseline_totals['cost_usd']:.6f}")
    print(f"  Energy: {baseline_totals['energy_kwh']:.6f} kWh")
    print(f"  Carbon: {baseline_totals['carbon_g']:.4f} g CO2e")
    print("-" * 80)

    for policy in ["speed", "balanced", "eco"]:
        c = comparison[policy]
        s = c["scheduled"]
        d = c["percent_diff"]
        print(f"\n{policy.upper()} Policy:")
        print(f"  Latency: {s['latency_ms']:.2f} ms ({d['latency_ms']:+.2f}%)")
        print(f"  Accuracy: {s['accuracy']:.4f} ({d['accuracy']:+.2f}%)")
        print(f"  Cost: ${s['cost_usd']:.6f} ({d['cost_usd']:+.2f}%)")
        print(f"  Energy: {s['energy_kwh']:.6f} kWh ({d['energy_kwh']:+.2f}%)")
        print(f"  Carbon: {s['carbon_g']:.4f} g CO2e ({d['carbon_g']:+.2f}%)")

    print("\n" + "=" * 80)
    print(f"Results written to {RESULTS_DIR}/scheduler_choices.csv")
    print(f"Results written to {RESULTS_DIR}/baseline_vs_scheduled.json")


if __name__ == "__main__":
    run_baseline_comparison()