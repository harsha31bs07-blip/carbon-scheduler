# Person A — Data & Cost Modeling

**You own:** `data/` folder — exclusively. No one else touches it. You don't touch `scheduler/` or `workflow/`.

**Git branch:** `feature/data`

**Your job:** produce every number the scheduler needs to make a decision — cost, energy, carbon, latency-offset, accuracy — so Person B never has to guess or hardcode inline.

---

## The contract you must satisfy (frozen at Hour 1)

Everyone agrees on this shape before building. Paste this into `shared/schema.py` together as a team, then don't change it without a group sync.

```python
# shared/schema.py
from dataclasses import dataclass

@dataclass
class Candidate:
    step_id: str
    model: str            # e.g. "haiku", "sonnet", "opus", "local-llama"
    location: str         # e.g. "us-east", "eu-west", "in-south"
    time_offset_hr: int   # 0 = now, 2 = delay 2 hours, etc.
    est_latency_ms: float
    est_accuracy: float   # 0-1, from your eval harness
    est_cost_usd: float
    est_energy_kwh: float
    est_carbon_g: float   # = est_energy_kwh * carbon_intensity(location, time)
```

Your entire job is: **given (model, location, time_offset), return the last 5 fields.** Person B's candidate generator will call your functions directly — you are the only source of truth for these numbers.

## Files you will create

```
data/
├── model_table.py       # cost + energy per model, per 1K tokens
├── region_carbon.py     # gCO2/kWh per region, real published figures
├── time_curve.py        # carbon intensity as function of hour-of-day
├── latency_model.py     # network latency offset per region
└── eval/
    ├── eval_set.json    # 15-20 test prompts + reference answers
    └── run_eval.py      # scores each real model against the eval set
```

## Function signatures Person B will import — lock these early

```python
# model_table.py
def get_model_cost_per_1k_tokens(model: str) -> float: ...
def get_model_energy_per_1k_tokens_kwh(model: str) -> float: ...
def get_model_base_accuracy(model: str) -> float: ...  # filled in from eval later

# region_carbon.py
def get_base_carbon_intensity(location: str) -> float: ...  # gCO2/kWh

# time_curve.py
def get_carbon_intensity_at_hour(location: str, hour_offset: int) -> float: ...

# latency_model.py
def get_network_latency_ms(location: str) -> float: ...
```

Tell Person B the moment these exist, even as stubs returning fixed numbers — they can start integrating immediately and swap in real values later without changing their code.

---

## Hour-by-hour

**Hour 0–1 (whole team):** Agree on `shared/schema.py`, the 3 models, the 3 regions, and the demo workflow. Don't skip this — it's what prevents collisions later.

**Hour 1–2:** Build `model_table.py` and `region_carbon.py` with real numbers.
- Model costs: pull actual per-token pricing for your 3 chosen models.
- Model energy: use published per-token energy estimates (order-of-magnitude is fine — cite the source, don't fabricate precision).
- Region carbon: use real grid carbon-intensity figures (e.g., France ~40-60 gCO2/kWh, India ~600-700 gCO2/kWh, US average ~380 gCO2/kWh). These are real and defensible.
- Ship stub versions of all functions above by end of Hour 2, even if hardcoded — message Person B immediately.

**Hour 2–3:** Build `latency_model.py` (simple fixed offset per region is fine, e.g. same-region = 20ms, cross-continent = 150-250ms) and `time_curve.py` (a sine-wave approximation of solar generation is enough: carbon dips midday, peaks at night, per region).

**Hour 3–4: Checkpoint.** Confirm with Person B that your 4 functions return sane values and their candidate generator can call them without errors. Fix signatures now, not later.

**Hour 4–6:** Build the accuracy eval harness.
- Write 15-20 prompts matching your actual demo workflow's step types (classification, retrieval-style QA, generation, summarization).
- Write reference answers or a scoring rubric.
- `run_eval.py` calls the real models (coordinate with Person C, who owns the actual model client code — you can reuse their client or a minimal standalone call) and scores each.
- Replace the hardcoded `get_model_base_accuracy()` stub with real scored numbers.

**Hour 6–7.5:** Sanity-check the full data pipeline: for every (model, location, hour_offset) combination in the demo, print the 5 output fields and eyeball them for absurd values (negative numbers, zero energy, etc.). This is what makes your trade-off story credible.

**Hour 7.5–9:** Polish + support the team.
- Write `data/README.md` documenting every number's source (cite it — "estimated from published figures, not live-metered" is fine and expected).
- Be on call to help Person B/C debug if their integration hits a data issue.

**Hour 9–10:** Join the whole-team demo prep. You're the one who can explain *why* the carbon numbers are credible if judges push on it — prepare a one-line source justification for each table.

## Definition of done
- [ ] All 6 functions above exist, are imported successfully by `scheduler/`, and return real (not placeholder) numbers by Hour 6
- [ ] `eval/run_eval.py` has actually been run against the 3 real models, not simulated
- [ ] `data/README.md` cites a source or estimation method for every hardcoded number
- [ ] Zero edits made outside the `data/` folder except the one-time shared `schema.py` agreement in Hour 1
