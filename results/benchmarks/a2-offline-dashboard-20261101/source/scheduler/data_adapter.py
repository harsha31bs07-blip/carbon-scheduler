"""
Thin adapter layer for Person A's data functions.
Delegates to data.candidate_builder for all estimates.
"""

from datetime import datetime, timezone
from typing import Optional

from data.candidate_builder import estimate_candidate
from shared.schema import Candidate, STEP_DEFINITIONS

# Task type to category mapping for accuracy lookup
TASK_TYPE_TO_CATEGORY = {
    "classification": "classification",
    "qa": "qa",
    "generation": "generation",
    "summarization": "summarization",
    "inference": "generation",   # fallback
    "training": "generation",    # fallback
    "batch": "generation",       # fallback
}

# Default token counts per task type (baseline tokens)
TASK_TOKENS = {
    "classification": (500, 100),
    "qa": (1000, 300),
    "generation": (1500, 500),
    "summarization": (1200, 300),
    "inference": (1000, 300),
    "training": (2000, 500),
    "batch": (5000, 1000),
}


def _get_step_category(step_id: str) -> str:
    """Get the accuracy category for a step."""
    step = next((s for s in STEP_DEFINITIONS if s["step_id"] == step_id), None)
    if step:
        return TASK_TYPE_TO_CATEGORY.get(step["task_type"], "generation")
    return "generation"


def _get_task_tokens(task_type: str) -> tuple[int, int]:
    """Get default (input_tokens, output_tokens) for a task type."""
    return TASK_TOKENS.get(task_type, (1000, 300))


def build_candidate(
    step_id: str,
    model: str,
    region: str,
    time_offset_hr: int,
    base_utc_time: Optional[datetime] = None,
) -> Candidate:
    """The one way to estimate a candidate for a pipeline step.

    Token counts and accuracy category come from the step's task type, so the scheduler,
    the baseline and the executor's fallback all price a step identically.
    """
    task_type = next(s["task_type"] for s in STEP_DEFINITIONS if s["step_id"] == step_id)
    input_tokens, output_tokens = _get_task_tokens(task_type)
    return estimate_candidate(
        step_id=step_id,
        model=model,
        location=region,
        time_offset_hr=time_offset_hr,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        category=TASK_TYPE_TO_CATEGORY.get(task_type, "generation"),
        base_utc_time=base_utc_time,
    )


def _estimate(model: str, region: str, time_offset_hr: int, task_type: str,
              base_utc_time: Optional[datetime] = None) -> Candidate:
    input_tokens, output_tokens = _get_task_tokens(task_type)
    return estimate_candidate(
        step_id="dummy",
        model=model,
        location=region,
        time_offset_hr=time_offset_hr,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        category=TASK_TYPE_TO_CATEGORY.get(task_type),
        base_utc_time=base_utc_time,
    )


def get_latency(model: str, region: str, time_offset_hr: int, task_type: str) -> float:
    """Estimated latency in milliseconds (delegates to data layer)."""
    return _estimate(model, region, time_offset_hr, task_type).est_latency_ms


def get_accuracy(model: str, region: str, time_offset_hr: int, task_type: str) -> float:
    """Estimated accuracy (0.0 to 1.0) (delegates to data layer)."""
    return _estimate(model, region, time_offset_hr, task_type).est_accuracy


def get_cost(model: str, region: str, time_offset_hr: int, task_type: str) -> float:
    """Estimated cost in USD (delegates to data layer)."""
    return _estimate(model, region, time_offset_hr, task_type).est_cost_usd


def get_energy(model: str, region: str, time_offset_hr: int, task_type: str) -> float:
    """Estimated energy in kWh (delegates to data layer)."""
    return _estimate(model, region, time_offset_hr, task_type).est_energy_kwh


def get_carbon(model: str, region: str, time_offset_hr: int, task_type: str, base_utc_time: Optional[datetime] = None) -> float:
    """Estimated carbon in grams CO2e (delegates to data layer)."""
    return _estimate(model, region, time_offset_hr, task_type, base_utc_time).est_carbon_g
