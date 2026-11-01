import sys
from pathlib import Path

# Ensure project root is in sys.path for shared imports
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from shared.schema import STEP_DEFINITIONS

# Linear DAG dependency edges: (prerequisite, dependent)
# classify -> retrieve -> draft -> summarize
DAG_EDGES: list[tuple[str, str]] = [
    ("classify", "retrieve"),
    ("retrieve", "draft"),
    ("draft", "summarize"),
]

# Step IDs in order of definition
STEP_IDS: list[str] = [step["step_id"] for step in STEP_DEFINITIONS]


def get_next_steps(completed: set[str]) -> list[str]:
    """
    Given a set of completed step_ids, returns a list of step_ids
    whose dependencies are fully satisfied and are ready to execute.
    """
    ready_steps: list[str] = []
    for step_id in STEP_IDS:
        if step_id in completed:
            continue

        # Check if all prerequisites for step_id are in completed set
        prereqs = [parent for parent, child in DAG_EDGES if child == step_id]
        if all(p in completed for p in prereqs):
            ready_steps.append(step_id)

    return ready_steps


if __name__ == "__main__":
    print("--- DAG Sanity Check ---")
    print("Step Definitions:")
    for step in STEP_DEFINITIONS:
        print(f"  - {step['step_id']} (type: {step['task_type']}, SLA: {step['sla_ms']}ms)")

    print("\nDependency Edges:")
    for parent, child in DAG_EDGES:
        print(f"  {parent} -> {child}")

    print("\nSimulating Step Resolution:")
    completed: set[str] = set()
    step_num = 1
    while len(completed) < len(STEP_IDS):
        next_steps = get_next_steps(completed)
        print(f"  Step {step_num}: Ready steps with completed={completed} -> {next_steps}")
        if not next_steps:
            print("  [Error] Circular dependency or stuck DAG!")
            break
        for s in next_steps:
            completed.add(s)
        step_num += 1

    print("\nExecution Order Verified successfully!")
