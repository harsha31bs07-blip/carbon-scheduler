"""data/candidate_builder.py

Single entry point for the scheduler (Person B): turns (step, model, region, delay,
token counts) into a fully populated `shared.schema.Candidate`.

    from data.candidate_builder import estimate_candidate, enumerate_candidates

    c = estimate_candidate("step_1", "haiku", "eu-west", time_offset_hr=4,
                           input_tokens=1200, output_tokens=300, category="summarization")
    all_c = enumerate_candidates("step_1", input_tokens=1200, output_tokens=300)

Conventions
-----------
* Token counts are given in "baseline" tokens (Haiku-4.5 tokenizer). Sonnet 5 / Opus 5 use a
  newer tokenizer that yields ~30% more tokens for the same text; that factor is applied
  automatically (disable with apply_tokenizer_factor=False).
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
from shared.schema import Candidate

DEFAULT_MODELS: Sequence[str] = ("local-qwen3.5", "local-llama3.2")  # LOCAL-ONLY SCOPE
DEFAULT_LOCATIONS: Sequence[str] = ("local-gpu",)  # Single local deployment
DEFAULT_OFFSETS: Sequence[int] = (0, 2, 4)  # Reduced offsets for local scope


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
    models: Iterable[str] = DEFAULT_MODELS,
    locations: Iterable[str] = DEFAULT_LOCATIONS,
    offsets: Iterable[int] = DEFAULT_OFFSETS,
    category: Optional[str] = None,
    base_utc_time: Optional[datetime] = None,
    origin: Optional[str] = None,
    apply_tokenizer_factor: bool = True,
) -> List[Candidate]:
    """Every model x location x time-offset combination for one workflow step.

    All candidates share ONE reference time, so carbon numbers are comparable to each other.
    """
    ref_time = base_utc_time or datetime.now(timezone.utc)
    return [
        estimate_candidate(
            step_id, m, loc, off, input_tokens, output_tokens, category, ref_time, origin,
            apply_tokenizer_factor,
        )
        for m in models
        for loc in locations
        for off in offsets
    ]
