"""data/model_table.py

Pricing, energy, latency and accuracy specifications for the three model tiers
(Haiku, Sonnet, Opus) used by the Candidate modeling pipeline.

What is sourced and what is assumed
-----------------------------------
* PRICING is published API pricing (checked against the official pricing page on
  2026-09-22): https://platform.claude.com/docs/en/about-claude/pricing
  Haiku 4.5 $1/$5, Sonnet 5 $2/$10, Opus 5 $5/$25 per MTok (input/output). Re-check before the demo.
* TOKENIZER: Claude 4.7+ models (Sonnet 5, Opus 5) use a newer tokenizer that yields ~30% more
  tokens for the same text (official pricing page). `tokenizer_factor` models this.
* ENERGY, LATENCY and BASE ACCURACY are ASSUMPTIONS, not published figures.
  Anthropic does not publish parameter counts or energy per token. Only the
  *relative* ordering Haiku < Sonnet < Opus is meant to be meaningful.
  Change ENERGY_SCALE to rescale all energy numbers at once.
* Accuracy only comes from measurement when data/eval/run_eval.py has been run
  against the live API (results file has "is_measured": true).
"""

from __future__ import annotations

import json
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
    base_accuracy: float  # ASSUMPTION until a live eval is run [0.0 - 1.0]
    tokenizer_factor: float = 1.0  # tokens produced vs. the Haiku-4.5 tokenizer for the same text

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


# Energy: output (decode) energy ratio Haiku:Sonnet:Opus = 1:6:32 (kept from the first version);
# input (prefill) tokens assumed to cost 20% of an output token.
# LOCAL MODELS: Based on GPU measurements (RTX 3050 Laptop, 48W inference power)
_IN_FRACTION = 0.2
_OUT_KWH = {"haiku": 0.00010, "sonnet": 0.00060, "opus": 0.00320}

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
    "haiku": ModelSpec(
        canonical_name="haiku",
        display_name="Claude Haiku 4.5",
        api_model_id="claude-haiku-4-5-20251001",
        cost_input_per_1k_usd=0.001,  # $1 / MTok
        cost_output_per_1k_usd=0.005,  # $5 / MTok
        energy_in_per_1k_kwh=_OUT_KWH["haiku"] * _IN_FRACTION * ENERGY_SCALE,
        energy_out_per_1k_kwh=_OUT_KWH["haiku"] * ENERGY_SCALE,
        ttft_ms=400.0,
        output_tokens_per_sec=150.0,
        base_accuracy=0.760,
    ),
    "sonnet": ModelSpec(
        canonical_name="sonnet",
        display_name="Claude Sonnet 5",
        api_model_id="claude-sonnet-5",
        cost_input_per_1k_usd=0.002,  # $2 / MTok (introductory price made permanent)
        cost_output_per_1k_usd=0.010,  # $10 / MTok
        energy_in_per_1k_kwh=_OUT_KWH["sonnet"] * _IN_FRACTION * ENERGY_SCALE,
        energy_out_per_1k_kwh=_OUT_KWH["sonnet"] * ENERGY_SCALE,
        ttft_ms=700.0,
        output_tokens_per_sec=80.0,
        base_accuracy=0.880,
        tokenizer_factor=1.3,
    ),
    "opus": ModelSpec(
        canonical_name="opus",
        display_name="Claude Opus 5",
        api_model_id="claude-opus-5",
        cost_input_per_1k_usd=0.005,  # $5 / MTok
        cost_output_per_1k_usd=0.025,  # $25 / MTok
        energy_in_per_1k_kwh=_OUT_KWH["opus"] * _IN_FRACTION * ENERGY_SCALE,
        energy_out_per_1k_kwh=_OUT_KWH["opus"] * ENERGY_SCALE,
        ttft_ms=1200.0,
        output_tokens_per_sec=45.0,
        base_accuracy=0.940,
        tokenizer_factor=1.3,
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

# Aliases: full API IDs and older names. Legacy names map to the tier, not to legacy pricing.
MODEL_ALIASES: Dict[str, str] = {
    "claude-haiku-4-5": "haiku",
    "claude-haiku-4-5-20251001": "haiku",
    "claude-3-5-haiku": "haiku",
    "claude-3-haiku": "haiku",
    "claude-haiku": "haiku",
    "haiku-4.5": "haiku",
    "haiku-3.5": "haiku",
    "claude-sonnet-5": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "claude-3-5-sonnet": "sonnet",
    "claude-3-sonnet": "sonnet",
    "claude-sonnet": "sonnet",
    "sonnet-5": "sonnet",
    "sonnet-3.5": "sonnet",
    "claude-opus-5": "opus",
    "claude-opus-4-6": "opus",
    "claude-3-opus": "opus",
    "claude-opus": "opus",
    "opus-5": "opus",
    "opus-3": "opus",
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
    """Normalize a model string to a canonical key ('haiku', 'sonnet', 'opus')."""
    cleaned = model.strip().lower()
    if cleaned in MODEL_TABLE:
        return cleaned
    if cleaned in MODEL_ALIASES:
        return MODEL_ALIASES[cleaned]
    for key in MODEL_TABLE:  # partial match, e.g. "claude-sonnet-5-20260601"
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
    except (json.JSONDecodeError, OSError):
        return None


def get_model_accuracy(
    model: str,
    category: Optional[str] = None,
    eval_results_file: Optional[Path] = None,
    allow_simulated: bool = False,
) -> float:
    """Accuracy in [0, 1], optionally for one category
    ('classification', 'qa', 'generation', 'summarization').

    Measured values are used only when the results file was produced by a fully
    live evaluation ("is_measured": true). Otherwise the assumed base_accuracy is
    returned. Set allow_simulated=True to use mock-mode results anyway.
    """
    canonical = normalize_model_name(model)
    data = _load_eval_results(eval_results_file or EVAL_RESULTS_PATH)
    if data and (data.get("is_measured") or allow_simulated):
        entry = data.get("models", {}).get(canonical, {})
        if category:
            cat_val = entry.get("category_accuracy", {}).get(category.strip().lower())
            if cat_val is not None:
                return float(cat_val)
        if "accuracy" in entry:
            return float(entry["accuracy"])
    return MODEL_TABLE[canonical].base_accuracy


def get_model_base_accuracy(model: str, eval_results_file: Optional[Path] = None) -> float:
    """Overall accuracy (measured if available, else assumed). Kept for backward compatibility."""
    return get_model_accuracy(model, None, eval_results_file)
