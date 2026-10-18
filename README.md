# Carbon Scheduler

Carbon-aware scheduling for a multi-step LLM pipeline. For each step of a support-ticket
triage workflow (`classify → retrieve → draft → summarize`), the scheduler picks a
**model**, **location**, and **time offset** by trading off latency, cost, energy,
carbon, and accuracy under a chosen policy.

Local models run through [Ollama](https://ollama.com) (`qwen3.5:4b`, `llama3.2:3b`).
Cloud models (Gemini, NVIDIA NIM) join the candidate pool when their API keys are set.

This is an A2 prototype under active development. Live execution currently uses
configured endpoints and runs immediately; alternate regions and future time windows
are scenario projections. Real deployment routing and durable delayed execution are
required milestones in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Workflow
prompts are currently independent, and energy, carbon and quality figures include
estimates rather than fully measured outcomes.

## Layout

| Path | Role |
|---|---|
| `shared/schema.py` | The contract: `Candidate` dataclass and `STEP_DEFINITIONS` (SLAs, accuracy floors) |
| `data/` | Estimates per candidate: cost, energy, latency, carbon (region + time-of-day curves), accuracy |
| `scheduler/` | `schedule_step(step_id, policy, custom_weights)` — filter by constraints, normalize, weighted score |
| `workflow/executor.py` | Runs the pipeline: schedule → call model → log to `results/runs/<run_id>.jsonl` with a manifest |
| `workflow/model_clients.py` | Ollama / Gemini / NVIDIA clients with retry, replay, and energy measurement |
| `workflow/dashboard/app.py` | Streamlit dashboard |
| `workflow/replay.py` | Exports and validates checksummed bundles for strict offline replay |
| `results/` | Historical recordings and benchmarks; new runs go in `results/runs/`, curated replay bundles in `results/demo/` |

Policies: `speed`, `balanced`, `eco`, `custom` (weights `w_latency`, `w_cost`, `w_energy`,
`w_carbon`, `w_accuracy`), plus `baseline` (largest model, no deferral) for comparison.

## Setup

```bash
pip install -r requirements-dev.txt
cp .env.example .env          # fill in keys; all cloud keys are optional
ollama pull qwen3.5:4b
ollama pull llama3.2:3b
```

Leave unused cloud keys blank. Local execution needs Ollama and downloaded models;
automated tests use mocked model calls and need no provider credentials.

## Run

```bash
# Pipeline (CLI)
python -m workflow.executor --policy eco
python -m workflow.executor --policy custom --weights '{"w_carbon":0.7,"w_latency":0.1,"w_cost":0.1,"w_energy":0.05,"w_accuracy":0.05}'
python -m workflow.executor --policy eco --compare    # paired live baseline/policy runs

# Dashboard
python -m streamlit run workflow/dashboard/app.py

# Baseline vs. scheduled comparison table
python -m scheduler.baseline

# Tests
python -m pytest
```

Strict replay requires a compatible exported bundle; historical files alone are not
eligible. After a completed live comparison, export its printed run IDs to a new
directory (replace the two example IDs):

```bash
python -m workflow.replay --baseline baseline_RUN_ID --scheduled RUN_ID --output results/demo
python -m workflow.executor --policy eco --replay --replay-dir results/demo
```

No curated bundle is included yet. Missing or invalid replay evidence stops the run
without making model calls.

## Docs

- `IMPLEMENTATION_PLAN.md` — current state and roadmap
- `PERSON_A/B/C_*.md` — original module requirements
- `DEMO_GUIDE.md`, `DEMO_SCRIPT_LOCAL.md` — demo walkthrough
- `GMAIL_SETUP_GUIDE.md` — SMTP setup for dashboard OTP login
- `data/README.md` — data-layer assumptions and sources
