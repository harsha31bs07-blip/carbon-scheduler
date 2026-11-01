# Carbon Scheduler

Carbon-aware scheduling for a multi-step LLM pipeline. For each step of a support-ticket
triage workflow (`classify → retrieve → draft → summarize`), the scheduler picks a
**model**, **location**, and **time offset** by trading off latency, cost, energy,
carbon, and accuracy under a chosen policy.

Local models run through [Ollama](https://ollama.com) (`qwen3.5:4b`, `llama3.2:3b`).
Gemini/NVIDIA adapters use explicit deployment mappings and credentials; key
presence alone does not establish model availability or physical location.

This is an A2 prototype under active development. Ticket workflows pass actual
outputs between steps, retrieve versioned local KB evidence and execute through a
durable SQLite worker with deadlines, delayed background work and restart recovery.
The dashboard submits and inspects work independently of the worker.
New worker submissions also retain API-cost and optional host-energy/carbon
accounting with explicit measurement coverage. See
[execution accounting](docs/execution-accounting.md).
Frozen paired policy experiments, dimension ablations and portable offline
comparisons are available through [policy evaluation](docs/policy-evaluation.md).
Human judgments can be bound to complete trial outputs with
[workflow quality review](docs/workflow-quality-review.md).
The [integrated dashboard](docs/dashboard.md) supports fixed/optimized submission,
planning explanations, resource coverage, offline comparisons and human review.
Use the current [demo guide](DEMO_GUIDE.md) for local startup and scope limits.

Two local Ollama models have empirical task profiles. A bounded joint planner
checks model/deployment/start choices, dependencies, capacity and all five metrics.
The real profiles currently have **unknown semantic quality**, so joint planning
correctly reports infeasibility. The worker supports fixed assignments and planned
submissions, with atomic plan/claim coordination verified offline. Reviewed live
optimized execution and a second real destination remain release gates in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). The original policy CLI
retains its independent prompts and region/time scenario projections.

## Layout

| Path | Role |
|---|---|
| `shared/schema.py` | The contract: `Candidate` dataclass and `STEP_DEFINITIONS` (SLAs, accuracy floors) |
| `shared/scheduling.py` | A2 schema-3 requests, decisions, execution attempts, UTC deadlines and metric provenance |
| `shared/deployments.py`, `config/` | Explicit endpoint/model registry with independent location evidence |
| `data/` | Estimates per candidate: cost, energy, latency, carbon (region + time-of-day curves), accuracy |
| `scheduler/` | `schedule_step(step_id, policy, custom_weights)` — filter by constraints, normalize, weighted score |
| `workflow/executor.py` | Runs the pipeline: schedule → call model → log to `results/runs/<run_id>.jsonl` with a manifest |
| `workflow/connected.py`, `workflow/workload.py` | Input-driven workflow, real KB evidence, response/background deadlines and a fixed explicit deployment |
| `workflow/workflow_store.py` | Atomic per-run prompt/output journal, resume and interrupted-call reconciliation boundary |
| `workflow/queue_store.py`, `workflow/worker.py` | Transactional leases, capacity, real delayed dispatch, cancellation and explicit bounded retries |
| `workflow/planned_worker.py`, `workflow/planning_records.py` | Optimized submissions, fresh eligibility, atomic decision/claim revisions and retained planning context |
| `workflow/execution_accounting.py`, `workflow/accounting_report.py` | Raw-attempt-bound fees, optional energy/carbon, coverage and separate response/final clocks |
| `workflow/evaluate_policies.py`, `workflow/evaluation_protocol.py`, `workflow/policy_comparison.py` | Frozen paired trials, fixed/per-step baselines, constrained policy ablations and offline comparison packages |
| `workflow/calibrate.py`, `data/profiles.py` | Retained production-prompt trials, honest telemetry and split-isolated empirical profiles |
| `scheduler/joint.py`, `scheduler/profile_candidates.py` | Joint DAG planning, calibrated candidates and timestamped carbon validation |
| `data/workloads/` | Versioned fictional support KB and separate calibration/held-out ticket fixtures |
| `workflow/model_clients.py` | Ollama / Gemini / NVIDIA clients with retry, replay, and energy measurement |
| `workflow/dispatch.py`, `workflow/deployment_clients.py` | Execute one due A2 candidate on its selected endpoint; retain executed identities and unknown measurements |
| `workflow/deployment_inventory.py` | Read-only capability probes; no model generation |
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
For a tested Windows dependency snapshot and isolated setup, see
[docs/development.md](docs/development.md).

## Run

```bash
# Connected workflow (fixed explicit model/deployment; Ollama required)
python -m workflow.connected --ticket "My account is locked after failed login attempts." --run-dir results/runs/my-ticket --response-only
python -m workflow.connected --resume results/runs/my-ticket

# Original weighted-policy demo and comparison (legacy independent prompts)
python -m workflow.executor --policy eco
python -m workflow.executor --policy custom --weights '{"w_carbon":0.7,"w_latency":0.1,"w_cost":0.1,"w_energy":0.05,"w_accuracy":0.05}'
python -m workflow.executor --policy eco --compare    # paired live baseline/policy runs

# Durable connected workflow (separate worker; start time uses real UTC)
python -m workflow.worker --db .runtime/queue.sqlite3 submit --config config/deployments.example.json --ticket "My CarbonDesk account is locked." --summary-delay-seconds 60
python -m workflow.worker --db .runtime/queue.sqlite3 run --max-jobs 3
python -m workflow.worker --db .runtime/queue.sqlite3 run --idle-exit-seconds 90

# Dashboard
python -m streamlit run workflow/dashboard/app.py

# Baseline vs. scheduled comparison table
python -m scheduler.baseline

# Tests
python -m pytest

# Read-only inventory (Ollama tags; cloud verification remains unknown)
python -m workflow.deployment_inventory --config config/deployments.example.json
```

Strict replay requires a compatible exported bundle; historical files alone are not
eligible. After a completed live comparison, export its printed run IDs to a new
directory (replace the two example IDs):

```bash
python -m workflow.replay --baseline baseline_RUN_ID --scheduled RUN_ID --output results/demo
python -m workflow.executor --policy eco --replay --replay-dir results/demo
```

No legacy matched-comparison bundle is included yet. Durable queue examples in
`results/demo/` use `workflow.queue_replay`; they do not satisfy the legacy CLI's
comparison-bundle requirement. Missing or invalid replay evidence stops the run
without making model calls.

## Docs

- `IMPLEMENTATION_PLAN.md` — current state and roadmap
- [docs/a2-foundations.md](docs/a2-foundations.md) — explicit deployment API and record compatibility
- [docs/connected-workflow.md](docs/connected-workflow.md) — connected ticket workflow, deadlines and resume
- [docs/durable-worker.md](docs/durable-worker.md) — queue commands, delayed execution and reconciliation
- [docs/joint-planner.md](docs/joint-planner.md) — constrained joint planning and honest infeasibility
- [data/METHODOLOGY.md](data/METHODOLOGY.md) — calibration, energy, quality and carbon provenance
- [docs/quality-review.md](docs/quality-review.md) — export human review packets and freeze reviewed profiles
- [docs/staged-planning.md](docs/staged-planning.md) — prepare joint decisions from actual outputs and explicit downstream forecasts
- [docs/planned-worker.md](docs/planned-worker.md) — submit and execute durable optimized plans, inspect infeasibility and replay decision history
- `PERSON_A/B/C_*.md` — original module requirements
- `DEMO_GUIDE.md`, `DEMO_SCRIPT_LOCAL.md` — demo walkthrough
- `GMAIL_SETUP_GUIDE.md` — SMTP setup for dashboard OTP login
- `data/README.md` — data-layer assumptions and sources
