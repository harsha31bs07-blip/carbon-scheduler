from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Optional


@dataclass
class Candidate:
    """A single scheduling decision returned for one pipeline step.

    HYBRID MODE: Local models (always) + cloud models (when keys present).
    """

    step_id: str
    model: str            # e.g. "local-qwen3.5", "local-llama3.2"
    location: str         # e.g. "local-gpu"
    time_offset_hr: int   # 0 = now, 2 = delay 2 hours, etc.
    est_latency_ms: float
    est_accuracy: float   # 0-1, benchmark-based proxy for local models
    est_cost_usd: float   # $0 for local models
    est_energy_kwh: float # ESTIMATED (GPU power × duration)
    est_carbon_g: float   # ESTIMATED (energy × grid intensity)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Candidate:
        """Build from a dict, ignoring keys that are not Candidate fields (e.g. JSONL extras)."""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# HYBRID MODE: Adjusted thresholds accommodate both local and cloud models
# Local models: qwen3.5:4b accuracy 0.65, llama3.2:3b accuracy 0.63
# Cloud models: gemini-3-flash 0.76, nemotron-ultra-3 0.88 (assumed; when API keys present)
# Latency constraints relaxed to match actual local model performance
STEP_DEFINITIONS: list[dict] = [
    {"step_id": "classify",  "task_type": "classification", "sla_ms": 4000, "min_accuracy": 0.60, "max_latency_ms": 4000},
    {"step_id": "retrieve",  "task_type": "qa",             "sla_ms": 6000, "min_accuracy": 0.60, "max_latency_ms": 6000},
    {"step_id": "draft",     "task_type": "generation",     "sla_ms": 15000, "min_accuracy": 0.60, "max_latency_ms": 15000},
    {"step_id": "summarize", "task_type": "summarization",  "sla_ms": 8000, "min_accuracy": 0.60, "max_latency_ms": 8000},
]


def schedule_step(
    step_id: str,
    policy: str,
    custom_weights: dict | None = None,
) -> Candidate:
    """
    Implemented in scheduler/optimizer.py.
    This stub exists only so the contract is importable for type-checking.
    """
    raise NotImplementedError(
        "schedule_step() is implemented in scheduler/optimizer.py. "
        "Import from scheduler.optimizer instead."
    )
