"""
Demo Preparation Script
Runs all 4 policies + baseline in replay mode to generate demo data
"""
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Force replay mode for demo safety
os.environ['DEMO_REPLAY'] = '1'

from workflow.executor import run_pipeline
import json
from pathlib import Path

print("=" * 80)
print("CARBON SCHEDULER DEMO PREPARATION")
print("=" * 80)
print()

# Track all run results
results = {}

# Run all 5 policies
policies_to_run = [
    ("baseline", None),
    ("eco", None),
    ("balanced", None),
    ("speed", None),
    ("custom", {"w_carbon": 0.7, "w_cost": 0.1, "w_latency": 0.1, "w_energy": 0.05, "w_accuracy": 0.05}),
]

for policy, weights in policies_to_run:
    print(f"\n{'='*80}")
    print(f"Running: {policy.upper()}")
    if weights:
        print(f"Custom weights: {weights}")
    print(f"{'='*80}\n")
    
    try:
        run_id = run_pipeline(
            policy=policy,
            custom_weights=weights,
            replay_mode=True
        )
        results[policy] = run_id
        print(f"\n[OK] {policy} complete: {run_id}")
    except Exception as e:
        print(f"\n[FAIL] {policy} FAILED: {e}")
        results[policy] = f"ERROR: {e}"

print("\n" + "=" * 80)
print("DEMO PREPARATION COMPLETE")
print("=" * 80)

# Load and compare results
print("\n=== POLICY COMPARISON ===\n")
print(f"{'Policy':<12} {'Run ID':<18} {'Model Diversity':<20} {'Region Diversity'}")
print("-" * 80)

for policy, run_id in results.items():
    if "ERROR" in str(run_id):
        print(f"{policy:<12} {run_id}")
        continue
    
    # Load the results file
    results_file = Path("results") / f"{run_id}.jsonl"
    if not results_file.exists():
        print(f"{policy:<12} {run_id:<18} No results file found")
        continue
    
    # Parse results
    models = set()
    regions = set()
    with open(results_file, 'r') as f:
        for line in f:
            record = json.loads(line)
            models.add(record.get('model', 'unknown'))
            regions.add(record.get('location', 'unknown'))
    
    model_str = ", ".join(sorted(models))
    region_str = ", ".join(sorted(regions))
    print(f"{policy:<12} {run_id:<18} {model_str:<20} {region_str}")

print("\n" + "=" * 80)
print("NEXT STEPS")
print("=" * 80)
print("""
1. Dashboard is running at: http://localhost:8501
2. Results files are in: results/
3. To view specific run: cat results/<run_id>.jsonl

Manual verification steps:
- Open dashboard in browser
- Select different policies from dropdown
- Verify custom weight sliders work
- Check baseline vs scheduled comparison
- Confirm all 4 policies show different metrics
""")
