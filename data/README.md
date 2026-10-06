# Data & Cost Modeling (`data/`) — Person A

Produces the numbers the scheduler needs for every **Candidate** (model × region × delay):
cost, energy, carbon, latency and accuracy. Contract: `shared/schema.py`.

```
shared/schema.py            Candidate dataclass (shared contract, do not change alone)
data/
  candidate_builder.py      <-- ONE entry point for the scheduler (estimate_candidate / enumerate_candidates)
  model_table.py            pricing, energy, latency profile, accuracy per model
  region_carbon.py          annual-average grid carbon intensity + UTC offsets per region
  time_curve.py             synthetic hourly carbon curve (solar dip / evening peak / night trough)
  latency_model.py          network RTT matrix + model inference latency
  eval/eval_set.json        16 benchmark prompts (4 categories x difficulty)
  eval/run_eval.py          eval harness (live calls or mock)
  eval/eval_results.json    generated output of run_eval.py
  tests/                    pytest suite
```

## Quickstart

```bash
python -m pytest data/tests -q          # from repo root
python -m data.eval.run_eval --mock     # deterministic, NOT measured
python -m data.eval.run_eval            # live, measured: every model available in the current mode
```

```python
from datetime import datetime, timezone
from data.candidate_builder import estimate_candidate, enumerate_candidates

now = datetime.now(timezone.utc)                 # pass ONE reference time for comparable carbon numbers
c = estimate_candidate("step_1", "gemini-3-flash", "eu-west", time_offset_hr=4,
                       input_tokens=1200, output_tokens=300,
                       category="summarization", base_utc_time=now)

all_c = enumerate_candidates("step_1", input_tokens=1200, output_tokens=300, base_utc_time=now)
# every valid (model, location) pair in the current mode x 3 offsets
# (local models only on local-gpu, cloud models only in cloud regions; override as needed)
```

### What each field means

| Field | Definition |
|---|---|
| `est_cost_usd` | exact input/output pricing for the given token counts |
| `est_energy_kwh` | assumed prefill + decode energy for those tokens |
| `est_carbon_g` | `est_energy_kwh × grid intensity(location, time_offset_hr)` |
| `est_latency_ms` | network RTT (origin → region) + time-to-first-token + decode time. **Excludes** the deliberate wait of `time_offset_hr`; the scheduler adds `time_offset_hr × 3_600_000` if it wants "wait then run" latency |
| `est_accuracy` | per-category measured accuracy if a live eval exists, otherwise the assumed base value |

Token counts are in *baseline* tokens. A model whose tokenizer yields more tokens for the same text can set
`ModelSpec.tokenizer_factor` (applied automatically; `apply_tokenizer_factor=False` disables it). All current models use 1.0.

## What is sourced vs. assumed (read this before presenting)

| Quantity | Status |
|---|---|
| Token prices | **Published**, checked 2026-10-06: Gemini 3 Flash $0.50/$3.00 per MTok (Gemini API paid tier); Nemotron 3 Ultra $0.50/$2.20 (hosted providers; NVIDIA's build.nvidia.com is free for prototyping). Local models $0. Re-check before the demo. |
| Energy per token | **Assumption.** Neither Google nor NVIDIA publishes energy per token. Only the ordering gemini-3-flash < nemotron-ultra-3 is meant to be meaningful (decode energy ratio 1 : 6; prefill = 20 % of decode). Local models: GPU power × duration estimates. `ENERGY_SCALE` in `model_table.py` rescales everything for sensitivity analysis. |
| Model latency (TTFT, tokens/s) | Cloud TTFT **calibrated** from real calls in `results/` (Gemini: 29 calls; Nemotron: 4, thin). Tokens/s is an assumption. Local rows from `full_benchmark.py`. |
| Base accuracy | **Assumption** until a live eval is run. |
| Grid carbon intensity | Rounded annual averages from the sources named in `region_carbon.py`. Verify exact figures/years before citing them in a report. |
| Hourly carbon curve | **Synthetic**: hand-tuned Gaussians (midday solar dip, evening peak, night trough) on top of the annual average. A plausible shape, not a forecast, and no DST handling. |
| Network RTT | Rounded typical inter-region values, not live measurements. |

Background reading for datacenter energy/PUE context (use for context only; the per-token values above are our assumptions):
Luccioni et al. 2023 *Power Hungry Processing* (arXiv:2311.16863); Patterson et al. 2021 (arXiv:2104.10350); de Vries 2023, *Joule* 7(10).

## Accuracy evaluation (`eval/`)

* `eval_set.json`: 16 prompts across classification, QA, generation, summarization (easy/medium/hard). Small sample: one item moves accuracy by ~6 points.
* **Live mode** (default): calls each model through the pipeline's own client (`workflow.model_clients`: Ollama for local, Gemini / NVIDIA NIM for cloud), scores with a keyword/format rubric, records per-item latency. A failed call **aborts the run** unless `--allow-fallback` is passed, in which case simulated scores are substituted and the file is flagged `"is_measured": false`, `"mode": "live_api_partial"`.
* **Mock mode** (`--mock`): deterministic scores drawn from `SIMULATION_PROBABILITIES` plus hash noise. These are *assumptions written as numbers*, not measurements, and the file is flagged `"is_measured": false`.
* `model_table.get_model_accuracy(model, category)` uses eval results **only when `is_measured` is true**; otherwise it returns the assumed base accuracy (`allow_simulated=True` overrides this for experiments).
* Scoring caveat: keyword matching is crude. For the final report, either state that, or add an LLM-as-judge pass.

**Before the final demo:** run the live eval once, commit the resulting `eval_results.json`, and update the accuracy line in your slides from "assumed" to "measured".

## Models (`model_table.py`)

Canonical keys are the names of the models that actually run, matching `workflow.model_clients.MODEL_REGISTRY`:
`local-qwen3.5`, `local-llama3.2`, `gemini-3-flash`, `nemotron-ultra-3`. The legacy tier names `haiku` / `opus`
(both ran on Gemini 3 Flash) and `sonnet` (Nemotron) still resolve, so older `results/` files load.

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
