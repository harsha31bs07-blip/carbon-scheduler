# Person B — Scheduler / Optimizer (the core decision engine)

**You own:** `scheduler/` folder — exclusively. No one else touches it. You don't touch `data/` or `workflow/`.

**Git branch:** `feature/scheduler`

**Your job:** the "brain" of the project. For every workflow step, generate all viable candidates, score them against a policy, and pick the winner — subject to hard constraints.

---

## The contract you consume and produce

You **import from** `data/` (Person A's functions) and you **export to** `workflow/` (Person C consumes your output). You never write into either of their folders.

```python
# shared/schema.py — same file everyone reads, frozen at Hour 1
from dataclasses import dataclass

@dataclass
class Candidate:
    step_id: str
    model: str
    location: str
    time_offset_hr: int
    est_latency_ms: float
    est_accuracy: float
    est_cost_usd: float
    est_energy_kwh: float
    est_carbon_g: float
```

**What you receive from Person C:** a list of step definitions (just `step_id` + task type + any SLA constraints). You don't need their execution code — just this list, agreed at Hour 1.

**What you must expose to Person C** (lock this signature early):

```python
# scheduler/optimizer.py
def schedule_step(step_id: str, policy: str, custom_weights: dict = None) -> Candidate:
    """
    policy: one of "eco", "balanced", "speed", or "custom"
    Returns the single winning Candidate for this step.
    """
```

That one function is your entire public interface. Person C calls it once per step and gets back exactly what to execute. Keep everything else — candidate generation, scoring internals — private to your folder.

## Files you will create

```
scheduler/
├── candidate_gen.py   # builds all (model, location, time_offset) combos → Candidate list
├── scorer.py          # normalizes metrics, applies weighted score
├── policies.py         # named weight presets: eco / balanced / speed
└── optimizer.py         # public entrypoint: schedule_step()
```

---

## The scoring approach

Normalize each metric 0-1 across the candidate set for that step, then:

```
score(c) = w_latency·norm_latency(c) + w_cost·norm_cost(c)
         + w_energy·norm_energy(c)   + w_carbon·norm_carbon(c)
         - w_accuracy·norm_accuracy(c)
```

Lower score wins. Apply hard constraints first (e.g., drop any candidate with `est_accuracy < 0.7` or `est_latency_ms > SLA`) — filter before scoring, not after.

Policy presets (a good starting point, tune during testing):

| Policy | w_latency | w_cost | w_energy | w_carbon | w_accuracy |
|---|---|---|---|---|---|
| Speed | 0.6 | 0.1 | 0.1 | 0.1 | 0.1 |
| Eco | 0.1 | 0.1 | 0.3 | 0.4 | 0.1 |
| Balanced | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 |

---

## Hour-by-hour

**Hour 0–1 (whole team):** Agree on `shared/schema.py`, the 3 models × 3 regions × time-offset options, and the step-definition format you'll receive from Person C.

**Hour 1–2:** Build `candidate_gen.py` using **stub data** — don't wait on Person A's real numbers. Call whatever placeholder functions Person A has committed (even fixed-return stubs), and produce a full `List[Candidate]` for a fake step. Getting the shape right matters more than the values right now.

**Hour 2–3:** Build `scorer.py` — the normalization + weighted sum. Test it standalone with synthetic candidates you invent by hand, so you know it picks the "obviously correct" winner before trusting it on real data.

**Hour 3–4: Checkpoint.** Swap Person A's stubs for their real (even if partial) functions. Confirm `candidate_gen.py` produces sane `Candidate` objects. This is also when you confirm the step-definition format with Person C — fix mismatches now.

**Hour 4–6:** Build `policies.py` (the 3 named presets) and finish `optimizer.py`'s public `schedule_step()` function. Add hard-constraint filtering (accuracy floor, latency SLA). Add the "delay to greener time slot" logic: try `time_offset_hr` in `[0, 1, 2, 3]` as additional candidates and let scoring naturally prefer a later, greener slot if the policy weights carbon heavily.

**Hour 6–7.5: The trade-off story is your responsibility to make correct.** Run `schedule_step()` for every step under all 3 policies and log the choices. Compute a **baseline** (always biggest model, always same region, time_offset=0) for comparison — this baseline-vs-scheduled diff is the project's headline result, and Person C's dashboard will display whatever numbers you produce here, so get them right.

**Hour 7.5–9:** Add a custom-weights path so the dashboard can expose a live slider (Person C needs `schedule_step(step_id, "custom", {"w_latency": 0.5, ...})` to work). Support Person C's integration — you'll likely be debugging `integration/run_pipeline.py` together during this window.

**Hour 9–10:** Whole-team demo prep. You should be ready to explain the scoring formula simply and defend why "balanced" isn't just an arbitrary average.

## Definition of done
- [ ] `schedule_step()` works end-to-end against Person A's real data by Hour 6
- [ ] All 3 named policies + a custom-weights path work
- [ ] Baseline-vs-scheduled comparison numbers are computed and logged to `results/`
- [ ] Zero edits made outside `scheduler/` except the one-time shared `schema.py` agreement and paired sessions in `integration/run_pipeline.py`
