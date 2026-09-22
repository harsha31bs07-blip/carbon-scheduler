# Person C — Workflow, Execution & Dashboard

**You own:** `workflow/` folder — exclusively. No one else touches it. You don't touch `data/` or `scheduler/`.

**Git branch:** `feature/workflow`

**Your job:** define the actual agent workflow, execute steps against real models using whatever the scheduler picked, and build the dashboard that tells the whole story visually.

---

## The contract you consume

You call Person B's one public function per step:

```python
def schedule_step(step_id: str, policy: str, custom_weights: dict = None) -> Candidate
```

You give Person B, at Hour 1, a fixed list of step definitions — this is the only thing they depend on from you, so lock it early:

```python
# agreed at Hour 1, lives in shared/schema.py alongside Candidate
STEP_DEFINITIONS = [
    {"step_id": "classify",   "task_type": "classification", "sla_ms": 2000},
    {"step_id": "retrieve",   "task_type": "qa",              "sla_ms": 3000},
    {"step_id": "draft",      "task_type": "generation",      "sla_ms": 5000},
    {"step_id": "summarize",  "task_type": "summarization",   "sla_ms": 2000},
]
```

Pick a workflow you can actually demo end-to-end — a support-ticket triage pipeline (classify → retrieve context → draft response → summarize for CRM) is a good default. Keep it to 3-4 steps. Don't overbuild the workflow itself; the scheduling decision is the star, not the agent logic.

## Files you will create

```
workflow/
├── dag.py              # the 4-step pipeline definition + dependencies
├── model_clients.py     # real API wrappers for the 3 chosen models
├── executor.py           # runs each step: calls schedule_step(), then the real model
└── dashboard/
    └── app.py            # Streamlit app
```

---

## Hour-by-hour

**Hour 0–1 (whole team):** Agree on `shared/schema.py`, the demo workflow's 4 steps, and the 3 models/regions. You're the one proposing the workflow — come in with a concrete draft, don't design it live.

**Hour 1–2:** Build `dag.py` (just the step list + order + dependencies — no execution yet) and start `model_clients.py`: get real API calls working against your 3 chosen models with dummy prompts, capturing actual latency and token counts. This is independent of both other people — start immediately.

**Hour 2–3:** Build `executor.py`'s skeleton: for each step in the DAG, call `schedule_step()` (use a hardcoded fake return value if Person B isn't ready yet — a plain `Candidate` object with made-up numbers), then call the appropriate real model client based on the returned `.model` field. Get one step running fully end-to-end (schedule → execute → capture real latency/tokens) before wiring the rest.

**Hour 3–4: Checkpoint.** Swap your fake `schedule_step()` calls for Person B's real function. Confirm the full 4-step DAG runs without errors using real scheduling decisions. This is the first moment the whole pipeline exists — expect some friction, budget time for it.

**Hour 4–6:** Build the dashboard v1 (Streamlit is fastest to ship):
- Show the DAG with each step's chosen `(model, location, time_offset)`
- Running totals: cost, energy, carbon, latency, accuracy
- A dropdown to switch policy (Eco / Balanced / Speed) and re-run

**Hour 6–7.5:** Add the **baseline comparison view** — run the same workflow forced to "always biggest model, same region, now" and show it side-by-side with the scheduled version. Pull these numbers from what Person B logged to `results/`. This chart is the single most important thing judges will look at — prioritize it over any other dashboard polish.

**Hour 7.5–9:** Add live weight sliders that call `schedule_step(step_id, "custom", weights)` and re-render the schedule in real time — this is your strongest live-demo moment, showing the decision visibly shift as you drag "carbon" up. Also add a Pareto-style scatter plot (cost vs. carbon, one dot per candidate, winner highlighted) if time allows — nice-to-have, not required.

**Hour 9–10:** Own the live demo run-through. Rehearse the exact click sequence: load workflow → run under Speed → run under Eco → show the schedule change → show the baseline-vs-scheduled savings numbers. Time it to ~90 seconds.

## Definition of done
- [ ] Full 4-step DAG runs end-to-end against real model APIs and Person B's real scheduler by Hour 6
- [ ] Dashboard shows the chosen candidate per step and running totals
- [ ] Baseline-vs-scheduled comparison is visible and correct
- [ ] Custom-weight sliders work live
- [ ] Zero edits made outside `workflow/` except the one-time shared `schema.py`/`STEP_DEFINITIONS` agreement and paired sessions in `integration/run_pipeline.py`
