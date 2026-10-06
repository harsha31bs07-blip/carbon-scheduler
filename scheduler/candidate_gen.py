from datetime import datetime, timezone
from typing import Optional

from shared.schema import Candidate
from shared.schema import STEP_DEFINITIONS
from data.candidate_builder import DEFAULT_OFFSETS, available_models, candidate_pool, locations_for
from data.model_table import MODEL_TABLE
from scheduler.data_adapter import build_candidate

# Time offsets in hours the scheduler may defer a step by
TIME_OFFSETS = list(DEFAULT_OFFSETS)

# Baseline never defers
BASELINE_OFFSET = 0

# Steps now match workflow's STEP_DEFINITIONS
STEPS = STEP_DEFINITIONS


def get_step(step_id: str) -> dict:
    for step in STEPS:
        if step["step_id"] == step_id:
            return step
    raise ValueError(f"Unknown step_id: {step_id}")


def baseline_choice() -> tuple[str, str]:
    """(model, location) for the baseline: the most accurate available model, at a fixed location.

    Offline that is the larger local model; in hybrid mode, the largest cloud tier.
    """
    model = max(available_models(), key=lambda m: MODEL_TABLE[m].base_accuracy)
    return model, locations_for(model)[0]


def generate_candidates(step_id: str, base_utc_time: Optional[datetime] = None) -> list[Candidate]:
    """Every valid (model, location) pair in the current mode, at every time offset.

    All candidates share one reference time so their carbon numbers are comparable.
    """
    get_step(step_id)  # validate
    ref_time = base_utc_time or datetime.now(timezone.utc)
    return [
        build_candidate(step_id, model, region, offset, ref_time)
        for model, region in candidate_pool()
        for offset in TIME_OFFSETS
    ]
