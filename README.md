# Carbon Scheduler

Carbon-aware scheduling for a multi-step LLM pipeline. For each step of a support-ticket
triage workflow (`classify → retrieve → draft → summarize`), the scheduler picks a
**model**, **location**, and **time offset** by trading off latency, cost, energy,
carbon, and accuracy under a chosen policy.

Local models run through [Ollama](https://ollama.com) (`qwen3.5:4b`, `llama3.2:3b`).
Cloud models (Gemini, NVIDIA NIM) join the candidate pool when their API keys are set.

## Layout

| Path | Role |
|---|---|
| `shared/schema.py` | The contract: `Candidate` dataclass and `STEP_DEFINITIONS` (SLAs, accuracy floors) |
| `data/` | Estimates per candidate: cost, energy, latency, carbon (region + time-of-day curves), accuracy |
| `scheduler/` | `schedule_step(step_id, policy, custom_weights)` — filter by constraints, normalize, weighted score |
| `workflow/executor.py` | Runs the pipeline: schedule → call model → log to `results/<run_id>.jsonl` |
| `workflow/model_clients.py` | Ollama / Gemini / NVIDIA clients with retry, replay, and energy measurement |
| `workflow/dashboard/app.py` | Streamlit dashboard |
| `results/` | Recorded runs; also the data source for replay mode |

Policies: `speed`, `balanced`, `eco`, `custom` (weights `w_latency`, `w_cost`, `w_energy`,
`w_carbon`, `w_accuracy`), plus `baseline` (largest model, no deferral) for comparison.

## Setup

```bash
pip install -r requirements-dev.txt
cp .env.example .env          # fill in keys; all cloud keys are optional
ollama pull qwen3.5:4b
ollama pull llama3.2:3b
```

## Run

```bash
# Pipeline (CLI)
python -m workflow.executor --policy eco
python -m workflow.executor --policy custom --weights '{"w_carbon":0.7,"w_latency":0.1,"w_cost":0.1,"w_energy":0.05,"w_accuracy":0.05}'
python -m workflow.executor --policy eco --replay      # no model calls, uses results/

# Dashboard
python -m streamlit run workflow/dashboard/app.py

# Baseline vs. scheduled comparison table
python -m scheduler.baseline

# Tests
python -m pytest
```

## Docs

- `IMPLEMENTATION_PLAN.md` — current state and roadmap
- `PERSON_A/B/C_*.md` — original module requirements
- `DEMO_GUIDE.md`, `DEMO_SCRIPT_LOCAL.md` — demo walkthrough
- `GMAIL_SETUP_GUIDE.md` — SMTP setup for dashboard OTP login
- `data/README.md` — data-layer assumptions and sources
