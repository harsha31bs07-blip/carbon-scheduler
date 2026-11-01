"""data/candidate_builder.py

Single entry point for the scheduler (Person B): turns (step, model, region, delay,
token counts) into a fully populated `shared.schema.Candidate`.

    from data.candidate_builder import estimate_candidate, enumerate_candidates

    c = estimate_candidate("step_1", "gemini-3-flash", "eu-west", time_offset_hr=4,
                           input_tokens=1200, output_tokens=300, category="summarization")
    all_c = enumerate_candidates("step_1", input_tokens=1200, output_tokens=300)

Conventions
-----------
* Token counts are given in "baseline" tokens. A model whose tokenizer yields more tokens
  for the same text sets ModelSpec.tokenizer_factor; it is applied automatically
  (disable with apply_tokenizer_factor=False). All current models use 1.0.
* est_cost_usd    : exact input/output pricing for the given token counts.
* est_energy_kwh  : assumed prefill + decode energy (see model_table.py).
* est_carbon_g    : est_energy_kwh * grid intensity at (location, time_offset_hr).
* est_latency_ms  : network RTT (origin -> location) + model TTFT + decode time.
                    It does NOT include the deliberate wait of `time_offset_hr`;
                    the scheduler adds time_offset_hr * 3_600_000 if it wants that.
* est_accuracy    : per-category if a live eval exists, otherwise the assumed base value.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

from data.latency_model import get_total_latency_ms
from data.model_table import (
    get_model_accuracy,
    get_model_detailed_cost,
    get_model_energy_kwh,
    get_model_spec,
    normalize_model_name,
)
from data.region_carbon import normalize_location
from data.time_curve import get_carbon_intensity_at_hour
from shared import availability
from shared.schema import Candidate

# ── Candidate pool ────────────────────────────────────────────────────────────
# Local models always run on-device; cloud models join when their provider's keys
# are configured (hybrid mode). Names match workflow.model_clients.MODEL_REGISTRY.
# gemini-3-pro has a client but no free tier, so it is not in the pool.
LOCAL_MODELS: Sequence[str] = ("local-qwen3.5", "local-llama3.2")
GEMINI_MODELS: Sequence[str] = ("gemini-3-flash",)
NVIDIA_MODELS: Sequence[str] = ("nemotron-ultra-3",)

LOCAL_LOCATIONS: Sequence[str] = ("local-gpu",)
CLOUD_LOCATIONS: Sequence[str] = ("us-west", "eu-west", "in-south")

DEFAULT_OFFSETS: Sequence[int] = (0, 2, 4)


def available_models() -> tuple[str, ...]:
    """Models usable right now: local always, cloud tiers when their provider is configured."""
    models = list(LOCAL_MODELS)
    if availability.gemini_available():
        models.extend(GEMINI_MODELS)
    if availability.nvidia_available():
        models.extend(NVIDIA_MODELS)
    return tuple(models)


def locations_for(model: str) -> Sequence[str]:
    """Where a model can run: local models on-device only, cloud models in cloud regions only."""
    return LOCAL_LOCATIONS if normalize_model_name(model) in LOCAL_MODELS else CLOUD_LOCATIONS


def candidate_pool() -> list[tuple[str, str]]:
    """Every valid (model, location) pair in the current mode."""
    return [(m, loc) for m in available_models() for loc in locations_for(m)]


def estimate_candidate(
    step_id: str,
    model: str,
    location: str,
    time_offset_hr: int = 0,
    input_tokens: int = 1000,
    output_tokens: int = 300,
    category: Optional[str] = None,
    base_utc_time: Optional[datetime] = None,
    origin: Optional[str] = None,
    apply_tokenizer_factor: bool = True,
) -> Candidate:
    """Build one Candidate. Raises ValueError on unknown model/region or bad arguments."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("input_tokens and output_tokens must be >= 0.")
    if time_offset_hr < 0:
        raise ValueError("time_offset_hr must be >= 0.")

    canonical_model = normalize_model_name(model)
    canonical_loc = normalize_location(location)

    factor = get_model_spec(canonical_model).tokenizer_factor if apply_tokenizer_factor else 1.0
    in_tok = input_tokens * factor
    out_tok = output_tokens * factor

    energy_kwh = get_model_energy_kwh(canonical_model, in_tok, out_tok)
    intensity = get_carbon_intensity_at_hour(canonical_loc, time_offset_hr, base_utc_time)

    return Candidate(
        step_id=step_id,
        model=canonical_model,
        location=canonical_loc,
        time_offset_hr=int(time_offset_hr),
        est_latency_ms=round(
            get_total_latency_ms(canonical_model, canonical_loc, out_tok, origin), 1
        ),
        est_accuracy=get_model_accuracy(canonical_model, category),
        est_cost_usd=get_model_detailed_cost(canonical_model, in_tok, out_tok),
        est_energy_kwh=energy_kwh,
        est_carbon_g=round(energy_kwh * intensity, 6),
    )


def enumerate_candidates(
    step_id: str,
    input_tokens: int = 1000,
    output_tokens: int = 300,
    models: Optional[Iterable[str]] = None,
    locations: Optional[Iterable[str]] = None,
    offsets: Iterable[int] = DEFAULT_OFFSETS,
    category: Optional[str] = None,
    base_utc_time: Optional[datetime] = None,
    origin: Optional[str] = None,
    apply_tokenizer_factor: bool = True,
) -> List[Candidate]:
    """Candidates for one workflow step, one per (model, location, time offset).

    By default uses candidate_pool(): the models available in the current mode, each
    only at locations where it can run. Passing `models` and/or `locations` overrides
    that; explicit `locations` are crossed with every model as given.

    All candidates share ONE reference time, so carbon numbers are comparable to each other.
    """
    if models is None and locations is None:
        pairs = candidate_pool()
    else:
        models = available_models() if models is None else tuple(models)
        locations = None if locations is None else tuple(locations)
        pairs = [(m, loc) for m in models for loc in (locations or locations_for(m))]

    ref_time = base_utc_time or datetime.now(timezone.utc)
    return [
        estimate_candidate(
            step_id, m, loc, off, input_tokens, output_tokens, category, ref_time, origin,
            apply_tokenizer_factor,
        )
        for m, loc in pairs
        for off in offsets
    ]
