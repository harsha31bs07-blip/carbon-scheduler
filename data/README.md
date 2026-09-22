# Data & Cost Modeling (`data/`) — Person A

Produces the numbers the scheduler needs for every **Candidate** (model × region × delay):
cost, energy, carbon, latency and accuracy. Contract: `shared/schema.py`.

```
shared/schema.py            Candidate dataclass (shared contract, do not change alone)
data/
  candidate_builder.py      <-- ONE entry point for the scheduler (estimate_candidate / enumerate_candidates)
  model_table.py            pricing, energy, latency profile, accuracy per model tier
  region_carbon.py          annual-average grid carbon intensity + UTC offsets per region
  time_curve.py             synthetic hourly carbon curve (solar dip / evening peak / night trough)
  latency_model.py          network RTT matrix + model inference latency
  eval/eval_set.json        16 benchmark prompts (4 categories x difficulty)
  eval/run_eval.py          eval harness (live API or mock)
  eval/eval_results.json    generated output of run_eval.py
  tests/                    pytest suite
```

## Quickstart

```bash
python -m pytest data/tests -q          # from repo root
python data/eval/run_eval.py --mock     # offline, deterministic, NOT measured
ANTHROPIC_API_KEY=... python data/eval/run_eval.py   # live, measured (16 prompts x 3 models)
```

```python
from datetime import datetime, timezone
from data.candidate_builder import estimate_candidate, enumerate_candidates

now = datetime.now(timezone.utc)                 # pass ONE reference time for comparable carbon numbers
c = estimate_candidate("step_1", "haiku", "eu-west", time_offset_hr=4,
                       input_tokens=1200, output_tokens=300,
                       category="summarization", base_utc_time=now)

all_c = enumerate_candidates("step_1", input_tokens=1200, output_tokens=300, base_utc_time=now)
# 3 models x 3 regions x 5 offsets = 45 Candidates (override models/locations/offsets as needed)
```

### What each field means

| Field | Definition |
|---|---|
| `est_cost_usd` | exact input/output pricing for the given token counts |
| `est_energy_kwh` | assumed prefill + decode energy for those tokens |
| `est_carbon_g` | `est_energy_kwh × grid intensity(location, time_offset_hr)` |
| `est_latency_ms` | network RTT (origin → region) + time-to-first-token + decode time. **Excludes** the deliberate wait of `time_offset_hr`; the scheduler adds `time_offset_hr × 3_600_000` if it wants "wait then run" latency |
| `est_accuracy` | per-category measured accuracy if a live eval exists, otherwise the assumed base value |

Token counts are in *baseline* tokens (Haiku 4.5 tokenizer). Sonnet 5 and Opus 5 use a newer tokenizer that
produces ~30 % more tokens for the same text (official pricing page), so `candidate_builder` multiplies their
token counts by 1.3 (`apply_tokenizer_factor=False` disables this).

## What is sourced vs. assumed (read this before presenting)

| Quantity | Status |
|---|---|
| Token prices | **Published**, checked against the official pricing page on 2026-09-22: Haiku 4.5 $1/$5, Sonnet 5 $2/$10, Opus 5 $5/$25 per MTok (input/output). Re-check before the demo. |
| Energy per token | **Assumption.** Anthropic publishes neither parameter counts nor energy per token. Only the ordering Haiku < Sonnet < Opus is meant to be meaningful (decode energy ratio 1 : 6 : 32; prefill = 20 % of decode). One knob, `ENERGY_SCALE` in `model_table.py`, rescales everything for sensitivity analysis. |
| Model latency (TTFT, tokens/s) | **Assumption.** Live eval runs record average latency per model in `eval_results.json`; use it to recalibrate `ttft_ms` / `output_tokens_per_sec`. |
| Base accuracy (0.76 / 0.88 / 0.94) | **Assumption** until a live eval is run. |
| Grid carbon intensity | Rounded annual averages from the sources named in `region_carbon.py`. Verify exact figures/years before citing them in a report. |
| Hourly carbon curve | **Synthetic**: hand-tuned Gaussians (midday solar dip, evening peak, night trough) on top of the annual average. A plausible shape, not a forecast, and no DST handling. |
| Network RTT | Rounded typical inter-region values, not live measurements. |

Background reading for datacenter energy/PUE context (use for context only; the per-token values above are our assumptions):
Luccioni et al. 2023 *Power Hungry Processing* (arXiv:2311.16863); Patterson et al. 2021 (arXiv:2104.10350); de Vries 2023, *Joule* 7(10).

## Accuracy evaluation (`eval/`)

* `eval_set.json`: 16 prompts across classification, QA, generation, summarization (easy/medium/hard). Small sample: one item moves accuracy by ~6 points.
* **Live mode** (`ANTHROPIC_API_KEY` set): calls the real models (`claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-opus-5`), scores with a keyword/format rubric, records per-item latency. A failed call **aborts the run** unless `--allow-fallback` is passed, in which case simulated scores are substituted and the file is flagged `"is_measured": false`, `"mode": "live_api_partial"`.
* **Mock mode** (`--mock` or no key): deterministic scores drawn from `SIMULATION_PROBABILITIES` plus hash noise. These are *assumptions written as numbers*, not measurements, and the file is flagged `"is_measured": false`.
* `model_table.get_model_accuracy(model, category)` uses eval results **only when `is_measured` is true**; otherwise it returns the assumed base accuracy (`allow_simulated=True` overrides this for experiments).
* Scoring caveat: keyword matching is crude. For the final report, either state that, or add an LLM-as-judge pass.

**Before the final demo:** run the live eval once, commit the resulting `eval_results.json`, and update the accuracy line in your slides from "assumed" to "measured".

## Models (`model_table.py`)

Canonical keys stay `haiku`, `sonnet`, `opus` (part of the contract with Persons B and C); aliases such as
`claude-sonnet-5` or `claude-haiku-4-5-20251001` resolve to them. Legacy names map to the tier, **not** to legacy pricing.

## Carbon time model (`time_curve.py`)

```
local_hour = (utc_hour + utc_offset + offset_hr) mod 24
M(t) = 1 - D_solar·exp(-((t-t_solar)/2.2)²) + H_peak·exp(-((t-t_peak)/2.0)²) - B_night·exp(-((t-3)/2.5)²)     (clamped to 0.5 … 1.6)
intensity = base_intensity(region) × M(local_hour)
```

Regions: `us-east`, `eu-west`, `in-south` (the 3 team regions) plus `us-west`, `eu-north`, `ap-southeast`.
`ap-south-1` is Mumbai (Western grid) and is mapped to `in-south` as the nearest modeled Indian region; `ap-south-2` is Hyderabad (Southern grid).

## Known limitations

* Energy, latency and base accuracy are assumptions (see table above).
* Accuracy varies by model and category only, not by region or time.
* No prompt-caching, batch, or data-residency price modifiers.
* With `time_offset_hr > 0`, carbon uses the projected curve at that hour; nothing here models queueing or SLA deadlines (scheduler's job).
