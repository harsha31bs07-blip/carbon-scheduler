"""data/model_table.py

Pricing, energy, latency and accuracy specifications for every model the scheduler
can pick: two local models (Ollama) and the cloud models the executor actually calls.

What is sourced and what is assumed
-----------------------------------
* PRICING is published per-token pricing, checked 2026-10-06:
  - gemini-3-flash: Gemini API paid tier, https://ai.google.dev/gemini-api/docs/pricing
    ($0.50 / $3.00 per MTok in/out; a free tier also exists)
  - nemotron-ultra-3: Nemotron 3 Ultra (550B A55B) at hosted providers, $0.50 / $2.20 per MTok
    (https://pricepertoken.com/pricing-page/model/nvidia-nemotron-3-ultra-550b-a55b).
    NVIDIA's own build.nvidia.com catalog is free for prototyping. If NVIDIA_NIM_MODEL points
    at a different model, re-price this row.
  - local models: $0 (on-device).
* LATENCY: cloud TTFT is calibrated from real calls logged in results/ (see each row);
  local rows come from full_benchmark.py.
* ENERGY and BASE ACCURACY are ASSUMPTIONS, not published figures. Neither Google nor
  NVIDIA publishes energy per token or parameter counts for these deployments. Only the
  relative ordering is meant to be meaningful. Change ENERGY_SCALE to rescale all energy.
* Accuracy remains an assumption unless an evaluation score explicitly identifies
  a manual/human-reviewed rubric. Live keyword/format proxy scores and the old
  "is_measured" flag do not establish semantic quality. Reviewed sample scores
  still estimate quality on future workflow inputs.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

# Global knob: multiply every energy value below (e.g. 0.5 or 2.0 for sensitivity analysis).
ENERGY_SCALE: float = 1.0

# Blended-rate assumption for "typical agentic workflow": 75% prompt / 25% completion tokens.
BLEND_INPUT_SHARE: float = 0.75
BLEND_OUTPUT_SHARE: float = 0.25


@dataclass(frozen=True)
class ModelSpec:
    """Economic, energy, latency and accuracy specification for a model tier."""

    canonical_name: str
    display_name: str
    api_model_id: str
    cost_input_per_1k_usd: float  # published
    cost_output_per_1k_usd: float  # published
    energy_in_per_1k_kwh: float  # ASSUMPTION: prefill energy per 1k input tokens (incl. PUE)
    energy_out_per_1k_kwh: float  # ASSUMPTION: decode energy per 1k output tokens (incl. PUE)
    ttft_ms: float  # ASSUMPTION: time to first token
    output_tokens_per_sec: float  # ASSUMPTION: decode speed
    base_accuracy: float  # ASSUMPTION until a reviewed evaluation is supplied [0.0 - 1.0]
    tokenizer_factor: float = 1.0  # tokens produced vs. the reference tokenizer for the same text

    @property
    def cost_blended_per_1k_usd(self) -> float:
        return (
            BLEND_INPUT_SHARE * self.cost_input_per_1k_usd
            + BLEND_OUTPUT_SHARE * self.cost_output_per_1k_usd
        )

    @property
    def energy_per_1k_kwh(self) -> float:
        """Blended (75/25) energy per 1k tokens, for callers without a token split."""
        return (
            BLEND_INPUT_SHARE * self.energy_in_per_1k_kwh
            + BLEND_OUTPUT_SHARE * self.energy_out_per_1k_kwh
        )


# Energy (ASSUMPTION): decode energy per 1k output tokens, relative-size guesses;
# input (prefill) tokens assumed to cost 20% of an output token.
# LOCAL MODELS: Based on GPU measurements (RTX 3050 Laptop, 48W inference power)
_IN_FRACTION = 0.2
_OUT_KWH = {"gemini-3-flash": 0.00010, "nemotron-ultra-3": 0.00060}

# Local model specs from benchmark measurements (2026-09-22)
# Latency: from full_benchmark.py measured averages
# Energy: ESTIMATED (GPU 48W × duration, not measured directly)
_LOCAL_QWEN_LATENCY_MS = {
    "classify": 2203,
    "retrieve": 5087,
    "draft": 4273,      # Using thinking OFF for better performance
    "summarize": 4273,
}
_LOCAL_LLAMA_LATENCY_MS = {
    "classify": 2965,
    "retrieve": 4729,
    "draft": 4118,
    "summarize": 3318,
}

MODEL_TABLE: Dict[str, ModelSpec] = {
    "gemini-3-flash": ModelSpec(
        canonical_name="gemini-3-flash",
        display_name="Gemini 3 Flash",
        api_model_id="gemini-3-flash-preview",
        cost_input_per_1k_usd=0.0005,  # $0.50 / MTok
        cost_output_per_1k_usd=0.003,  # $3.00 / MTok
        energy_in_per_1k_kwh=_OUT_KWH["gemini-3-flash"] * _IN_FRACTION * ENERGY_SCALE,
        energy_out_per_1k_kwh=_OUT_KWH["gemini-3-flash"] * ENERGY_SCALE,
        # CALIBRATED: 29 calls on 2026-09-21, median 1371 ms for 42 output tokens
        ttft_ms=1000.0,
        output_tokens_per_sec=150.0,  # ASSUMPTION
        base_accuracy=0.760,  # ASSUMPTION until a reviewed rubric is supplied
    ),
    "nemotron-ultra-3": ModelSpec(
        canonical_name="nemotron-ultra-3",
        display_name="Nemotron 3 Ultra",
        api_model_id="nvidia/nemotron-3-ultra",  # actual ID comes from NVIDIA_NIM_MODEL
        cost_input_per_1k_usd=0.0005,  # $0.50 / MTok
        cost_output_per_1k_usd=0.0022,  # $2.20 / MTok
        energy_in_per_1k_kwh=_OUT_KWH["nemotron-ultra-3"] * _IN_FRACTION * ENERGY_SCALE,
        energy_out_per_1k_kwh=_OUT_KWH["nemotron-ultra-3"] * ENERGY_SCALE,
        # CALIBRATED (thin sample): 4 calls on 2026-09-21, median 1313 ms for 36 output tokens
        ttft_ms=800.0,
        output_tokens_per_sec=80.0,  # ASSUMPTION
        base_accuracy=0.880,  # ASSUMPTION until a reviewed rubric is supplied
    ),
    # LOCAL MODELS (Ollama via RTX 3050 Laptop GPU)
    "local-qwen3.5": ModelSpec(
        canonical_name="local-qwen3.5",
        display_name="Qwen 3.5 4B (Local)",
        api_model_id="qwen3.5:4b",
        cost_input_per_1k_usd=0.0,    # Local inference, no API cost
        cost_output_per_1k_usd=0.0,   # Local inference, no API cost
        energy_in_per_1k_kwh=0.00013 * ENERGY_SCALE,  # ESTIMATED: GPU 48W, ~2-5s avg
        energy_out_per_1k_kwh=0.00013 * ENERGY_SCALE,  # ESTIMATED: same (no prefill/decode split)
        ttft_ms=500.0,    # ESTIMATED: local model load time
        output_tokens_per_sec=30.0,  # MEASURED: ~60-120 tokens in 2-5s
        base_accuracy=0.650,  # Benchmark-based proxy (Qwen published scores)
    ),
    "local-llama3.2": ModelSpec(
        canonical_name="local-llama3.2",
        display_name="Llama 3.2 3B (Local)",
        api_model_id="llama3.2:3b",
        cost_input_per_1k_usd=0.0,
        cost_output_per_1k_usd=0.0,
        energy_in_per_1k_kwh=0.00011 * ENERGY_SCALE,  # ESTIMATED: faster model, less energy
        energy_out_per_1k_kwh=0.00011 * ENERGY_SCALE,
        ttft_ms=450.0,
        output_tokens_per_sec=35.0,  # MEASURED: slightly faster than qwen
        base_accuracy=0.630,  # Benchmark-based proxy (Meta MMLU scores)
    ),
}

# Aliases: API IDs, client names, and legacy tier names.
MODEL_ALIASES: Dict[str, str] = {
    "gemini-3-flash-preview": "gemini-3-flash",
    "gemini_3_flash": "gemini-3-flash",
    "nemotron_ultra_3": "nemotron-ultra-3",
    "nemotron": "nemotron-ultra-3",
    # Legacy tier names in results/ before 2026-10: these ran on Gemini Flash / Nemotron
    # (only their estimated cost/energy used Claude figures).
    "haiku": "gemini-3-flash",
    "opus": "gemini-3-flash",
    "sonnet": "nemotron-ultra-3",
    # Local model aliases
    "qwen3.5:4b": "local-qwen3.5",
    "qwen": "local-qwen3.5",
    "local-qwen": "local-qwen3.5",
    "llama3.2:3b": "local-llama3.2",
    "llama": "local-llama3.2",
    "local-llama": "local-llama3.2",
}

# Written by data/eval/run_eval.py
EVAL_RESULTS_PATH = Path(__file__).parent / "eval" / "eval_results.json"


def normalize_model_name(model: str) -> str:
    """Normalize a model string to a canonical MODEL_TABLE key."""
    cleaned = model.strip().lower()
    if cleaned in MODEL_TABLE:
        return cleaned
    if cleaned in MODEL_ALIASES:
        return MODEL_ALIASES[cleaned]
    for key in MODEL_TABLE:  # partial match, e.g. "gemini-3-flash-preview-0925"
        if key in cleaned:
            return key
    raise ValueError(f"Unsupported model '{model}'. Supported models: {list(MODEL_TABLE)}.")


def get_model_spec(model: str) -> ModelSpec:
    return MODEL_TABLE[normalize_model_name(model)]


def get_model_cost_per_1k_tokens(model: str) -> float:
    """Blended USD cost per 1k tokens (75% prompt / 25% completion)."""
    return get_model_spec(model).cost_blended_per_1k_usd


def get_model_detailed_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Exact USD cost for explicit input and output token counts."""
    spec = get_model_spec(model)
    return (input_tokens / 1000.0) * spec.cost_input_per_1k_usd + (
        output_tokens / 1000.0
    ) * spec.cost_output_per_1k_usd


def get_model_energy_per_1k_tokens_kwh(model: str) -> float:
    """Blended (75/25) assumed energy per 1k tokens in kWh."""
    return get_model_spec(model).energy_per_1k_kwh


def get_model_energy_kwh(model: str, input_tokens: int, output_tokens: int) -> float:
    """Assumed energy (kWh) for explicit input and output token counts."""
    spec = get_model_spec(model)
    return (input_tokens / 1000.0) * spec.energy_in_per_1k_kwh + (
        output_tokens / 1000.0
    ) * spec.energy_out_per_1k_kwh


def get_model_latency_profile(model: str) -> Tuple[float, float]:
    """Return (ttft_ms, output_tokens_per_sec) for a model."""
    spec = get_model_spec(model)
    return spec.ttft_ms, spec.output_tokens_per_sec


def _load_eval_results(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None


def _reviewed_accuracy_source(value) -> bool:
    """Match the reviewed-rubric convention used by the A2 profile builder."""
    if not isinstance(value, str):
        return False
    return value in {"manual-review", "human-review"} or (
        value.startswith(("manual-review:", "human-review:"))
        and bool(value.split(":", 1)[1].strip())
    )


def _accuracy_value(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value) and 0 <= value <= 1
    except OverflowError:
        valid = False
    return float(value) if valid else None


def get_model_accuracy(
    model: str,
    category: Optional[str] = None,
    eval_results_file: Optional[Path] = None,
    allow_simulated: bool = False,
) -> float:
    """Accuracy in [0, 1], optionally for one category
    ('classification', 'qa', 'generation', 'summarization').

    Only scores with an explicit ``accuracy_source`` (per-model) or
    ``score_source`` (whole-file) of ``manual-review[:rubric-id]`` or
    ``human-review[:rubric-id]`` are eligible. The legacy ``is_measured`` flag
    alone never promotes live keyword/format checks to semantic accuracy.
    Missing/invalid scores fall back to the documented base assumption.

    ``allow_simulated=True`` is a diagnostic override for old proxy/mock files;
    callers using it must retain that provenance rather than report measurement.
    """
    if type(allow_simulated) is not bool:
        raise ValueError("allow_simulated must be an explicit boolean diagnostic override")
    canonical = normalize_model_name(model)
    data = _load_eval_results(eval_results_file or EVAL_RESULTS_PATH)
    models = data.get("models") if data else None
    entry = models.get(canonical) if isinstance(models, dict) else None
    if isinstance(entry, dict):
        source = entry.get("accuracy_source", data.get("score_source"))
        provenance = entry.get("accuracy_provenance", data.get("score_provenance"))
        if not allow_simulated and (not _reviewed_accuracy_source(source)
                                    or provenance is not None and provenance not in ("estimated", "measured")):
            return MODEL_TABLE[canonical].base_accuracy
        if category:
            categories = entry.get("category_accuracy")
            key = category.strip().lower()
            if isinstance(categories, dict) and key in categories:
                cat_val = _accuracy_value(categories[key])
                return cat_val if cat_val is not None else MODEL_TABLE[canonical].base_accuracy
        accuracy = _accuracy_value(entry.get("accuracy"))
        if accuracy is not None:
            return accuracy
    return MODEL_TABLE[canonical].base_accuracy


def get_model_base_accuracy(model: str, eval_results_file: Optional[Path] = None) -> float:
    """Reviewed sample accuracy if available, else assumed; legacy compatibility."""
    return get_model_accuracy(model, None, eval_results_file)
