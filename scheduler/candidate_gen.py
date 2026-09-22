from shared.schema import Candidate
from shared.schema import STEP_DEFINITIONS
from data.model_table import MODEL_TABLE
from data.region_carbon import REGION_PROFILES
from scheduler.data_adapter import (
    get_latency,
    get_accuracy,
    get_cost,
    get_energy,
    get_carbon,
)

# LOCAL-ONLY SCOPE: Only use local models and local-gpu deployment
# Filter out cloud models for the local-only scope
MODELS = [m for m in MODEL_TABLE.keys() if m.startswith("local-")]  # ["local-qwen3.5", "local-llama3.2"]

# Only local deployment location
REGIONS = ["local-gpu"]

# Time offsets in hours (reduced for local scope)
TIME_OFFSETS = [0, 2, 4]

# Baseline: largest local model, local-gpu, no delay
BASELINE_MODEL = "local-qwen3.5"  # Larger of the two local models (4B vs 3B)
BASELINE_REGION = "local-gpu"
BASELINE_OFFSET = 0

# Steps now match workflow's STEP_DEFINITIONS
STEPS = STEP_DEFINITIONS


def get_step(step_id: str) -> dict:
    for step in STEPS:
        if step["step_id"] == step_id:
            return step
    raise ValueError(f"Unknown step_id: {step_id}")


def generate_candidates(step_id: str):
    step = get_step(step_id)
    task_type = step["task_type"]
    candidates = []
    for model in MODELS:
        for region in REGIONS:
            for offset in TIME_OFFSETS:
                c = Candidate(
                    step_id=step_id,
                    model=model,
                    location=region,
                    time_offset_hr=offset,
                    est_latency_ms=get_latency(model, region, offset, task_type),
                    est_accuracy=get_accuracy(model, region, offset, task_type),
                    est_cost_usd=get_cost(model, region, offset, task_type),
                    est_energy_kwh=get_energy(model, region, offset, task_type),
                    est_carbon_g=get_carbon(model, region, offset, task_type),
                )
                candidates.append(c)
    return candidates