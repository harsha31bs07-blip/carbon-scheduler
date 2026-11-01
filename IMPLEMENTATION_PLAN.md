# A2 Implementation Plan: Carbon- and Latency-Aware Agent Workflow Scheduler

New app improvement backlog: [App analysis and implementation plan (2026-11-01)](docs/plans/APP_IMPROVEMENT_PLAN-2026-11-01.md). It prioritizes authentication correctness, guided setup, workflow tracking, scheduling explanations, review usability and operational checks; the experimental release gates below remain separate.

Latest repository review: **2026-10-26**. See the [project analysis and ordered completion plan](docs/plans/COMPLETION_PLAN-2026-10-26.md) for current verification, corrected capability status, implementation tasks, dependencies and release acceptance. The milestone history below is preserved.

Implementation update **2026-11-01**: the local implementation now integrates optimized dashboard submission/decisions, resource coverage, paired comparisons, human review, durable execution and offline replay. The fresh-install rehearsal repaired conflicting Windows dependency pins. A fully executed scripted demo is retained with explicit simulation labels and no human scores. The full A2 experimental release remains open: the user has no second destination and no assigned human reviewer; the local endpoint is currently unavailable. See [release readiness](docs/release-readiness.md), [the dashboard](docs/dashboard.md) and [the current demo guide](DEMO_GUIDE.md).

Latest checks: the new isolated environment installed successfully, passed `pip check`, and passed **1,917 full-suite tests in 233.58 seconds**. The added scripted-demo test passed; these cover all 1,918 collected tests. Dashboard startup returned HTTP 200/ok. The completed four-trial demo is simulation; real quality and live optimized execution remain unverified.

Updated: 2026-10-19. This is the working plan for implementation.

## 1. Objective

> Develop an AI scheduler that dynamically selects the execution location, model, and processing time for each agentic workflow step while balancing latency, accuracy, cost, energy consumption, and carbon emissions.

Build a scheduler whose choices control actual execution: **which model, on which deployment, at what time**. Evaluate the complete workflow against strong fixed execution strategies. Support-ticket processing is the first demonstration workload, not a restriction on the scheduler's eventual use.

All three decision dimensions are required for the A2 release. Region labels and future offsets in a chart establish a scenario, not executed routing or delayed processing. A simulation-only submission would require evaluator acceptance and an explicit scope change.

The previous plan is preserved at [docs/plans/IMPLEMENTATION_PLAN.pre-A2-2026-10-18.md](docs/plans/IMPLEMENTATION_PLAN.pre-A2-2026-10-18.md). Its decision to defer real routing and delayed execution is superseded. Its principles of honest measurement and reproducibility remain applicable.

## 2. Reviewed starting point

The original review covered local commit `b59e258` plus existing modified and untracked source files. That reviewed source, the generator-mock fix and this plan were subsequently published as baseline commit `2c09887` on `main` at `harsha31bs07-blip/carbon-scheduler`. Its GitHub Actions test run passed. Current implementation work is on `feat/a2-foundations`; preserve the published baseline and historical recordings.

| Area | Evidence from the 2026-10-18 review | Implication |
|---|---|---|
| Tests | `python -m pytest -q -p no:cacheprovider`: **226 passed, 1 failed**, 62.07 seconds | Repair the outdated generator mock; these mostly mocked tests do not establish live inference quality |
| Scheduler | Candidate factory, constraints, weighted policies and model/region/time scenarios exist | Extend and calibrate existing modules |
| Execution | Ollama/Gemini/NVIDIA clients; live candidates have zero delay and a reference location per model | Add explicit deployment dispatch and a delayed worker |
| Workflow | Ordered classify/retrieve/draft/summarize steps have independent fixed prompts | Implement actual inputs, retrieval and output chaining |
| Evidence handling | New versioned records, matched comparisons, strict checksummed replay and error tests exist in the working tree | Preserve and evolve these capabilities |
| Calibration | Fixed token budgets differ substantially from recorded task sizes | Recalibrate before interpreting policy outcomes |
| Local probe | All three policies chose the same models; retrieve and summarize each had 0/2 estimated-feasible candidates | Expose infeasibility and distinguish estimator errors from a genuine lack of trade-offs |
| Quality | Saved evaluation has simulated results and `is_measured: false` | No measured quality-preservation claim yet |
| Energy/carbon | Mixed provenance can be mislabeled; hourly curves are synthetic; local grid defaults to US assumptions | Correct measurement labeling and configure the actual grid |
| Demo | Dashboard/historical runs exist; default curated replay bundle was absent | Generate fresh compatible evidence after live validation |

No applicable `AGENTS.md` was found. `PERSON_A/B/C_*.md` describe historical module responsibilities; this plan defines implementation order and acceptance.

## 3. First complete A2 release

Release gates:

- [x] An input-driven, connected workflow with an urgent response path and an explicitly deferrable background step.
- [ ] At least two models and two real execution destinations. Geographic carbon comparisons require known, distinct grid locations; two endpoints on one machine do not establish geographic shifting. Not every model must run at every destination.
- [x] The chosen deployment actually receives the request; planned and executed identities are both retained (local destination verified; second destination still required above).
- [x] Immediate and delayed execution with deadlines, cancellation, durable state and restart recovery (fixed-assignment local worker; joint dispatch remains incomplete).
- [ ] Decisions adapt to workload size, deployment availability, elapsed time and timestamped carbon information.
- [ ] All five metrics appear in reproducible comparisons; quality is evaluated on outputs and every estimate is labeled.
- [ ] Passing automated checks, a strict replay bundle, a demonstrable live path and documented limitations.

Positive savings are not a guaranteed release outcome. Equal choices, zero/negative savings, failed runs and missed deadlines must remain visible. Correct execution and defensible evaluation are mandatory.

## 4. Architecture and contracts

### Deployments

Introduce an explicit deployment registry: stable ID, client/provider type, endpoint reference, supported model IDs, hardware/profile ID, configured region/grid, location verification source, concurrency limit, and credential environment-variable names. Never serialize credential values.

Separate model identity from deployment identity. Generic provider endpoints with unknown physical routing may be used for inference but cannot represent a selectable geographic region. A key or a hardcoded model name does not prove availability: use bounded health/model capability checks with recorded timestamps.

Preferred first topology: existing local Ollama plus a second accessible worker with a known location. A region-pinned hosted deployment is an alternative. Inventory access before choosing; no paid infrastructure is assumed.

### Requests, decisions and time

Proposed contracts, with names adjustable during implementation:

| Contract | Required information |
|---|---|
| WorkflowRequest | Input, arrival UTC, policy, response deadline, final completion deadline, workload/constraint versions |
| StepRequest | Dependencies, task type, actual prompt/context size, output limit, earliest start, latest finish, deferral permission |
| Candidate | Model, deployment, absolute planned start UTC, estimates, uncertainty and provenance |
| SchedulingDecision | Winner, alternatives, rejected constraints, feasible count, normalized metrics, weighted contributions, snapshot/version |
| ExecutionAttempt | Planned and actual timestamps, wait/provider durations, retries, executed identities, output, usage and measurement provenance |

Use UTC timestamps and explicit integer duration units. Migrate integer-hour offsets to second-resolution durations and absolute starts. Convert legacy hours explicitly; reject unsupported fractional legacy offsets instead of truncating. Preserve older evidence with its original meaning.

Distinguish inference SLA, deliberate deferral, queue delay and end-to-end completion. Include dependency waits, network/model-load overhead and retries where applicable. For a branching DAG, calculate completion along its critical path rather than summing parallel durations.

### Optimization

Retain transparent constrained optimization for the first release. Add a learned router only if the rubric requires one or measurements establish a benefit.

1. Generate compatible deployment/model/start-window candidates.
2. Enforce availability/capacity, minimum calibrated quality, dependency ordering, step limits and workflow deadlines.
3. For the small demo DAG, evaluate complete assignments with bounded enumeration over a finite time grid. Record search limits and overhead. A truncated or heuristic search must not be described as globally optimal.
4. Rank feasible plans using the five policy weights. Validate supported, finite, non-negative weights with a positive total.
5. Use the same reference snapshot and normalization when comparing policies. Explain that energy and carbon are correlated at a common grid intensity.
6. Replan unfinished work after prerequisite completion or significant changes in availability, forecast, capacity or remaining deadline. Retain decision history; freeze already-dispatched work.
7. Return explicit infeasibility. Configured best-effort execution must preserve violations and cannot count as satisfying the original constraints.

Use total cost/energy/carbon and workflow completion time for plan scoring. Per-step quality estimates are scheduling proxies; their average is not measured end-to-end quality. Evaluate final outputs separately.

### Worker and evidence

Use SQLite for the initial single-machine coordinator/queue and a separate worker CLI. Streamlit submits work and reads progress; it does not own durability or wait for delayed jobs.

Persist inputs, dependency outputs, decisions and attempts. Define pending, scheduled, running, completed, failed, cancelled and needs-reconciliation states. Atomically claim work with leases; define renewal, bounded retries and restart recovery.

A crash after provider execution but before result persistence is ambiguous. Use provider idempotency when supported; otherwise reconcile or explicitly retry while recording possible duplicate usage. Do not promise exactly-once external inference. Running-job cancellation is best effort; retain incurred usage and prevent downstream dispatch.

Version the run schema and preserve scheduled/executed distinctions. Strict replay reconstructs stored evidence without inference calls or original wall-clock waits. Simulated clocks are valid for tests/scenarios and must be labeled accordingly.

## 5. Milestones and checklist

Status values: **not started**, **in progress**, **blocked on external dependency**, **verified**. Existing code does not automatically satisfy the new contracts. Record acceptance evidence before marking a milestone verified.

| ID | Milestone | Dependencies | Current status |
|---|---|---|---|
| M0 | Stabilize the reviewed baseline | None | Verified |
| M1 | Define deployments, requests and records | M0 | Verified |
| M2 | Connect workflow inputs, outputs and deadlines | M1 | Verified |
| M3 | Correct measurements and calibrate profiles | M1, M2 | In progress — local pilot and held-out latency verified; semantic review pending |
| M4 | Plan model, location and processing time jointly | M1-M3 | In progress — planner/profile bridge verified offline; live quality gate unmet |
| M5 | Execute decisions through a durable queue | M1, M2, M4 | In progress — joint revalidation and accounting verified offline; reviewed live run and second destination pending |
| M6 | Evaluate the complete scheduler | M3-M5 | In progress — matched runner/comparison tooling verified offline; end-to-end reviews and final live measurements pending |
| M7 | Complete dashboard, replay and release demonstration | M5, M6 | In progress — queue submission/progress/cancellation; optimized policy views pending |

### M0 — Stabilize the reviewed baseline

Primary files: `tests/test_scheduler.py`, dependency files, `.gitignore`, existing workflow/run/replay tests.

- [x] Inventory existing changes/results and preserve unrelated work. Published reviewed baseline: `2c09887`; historical recordings remain in place.
- [x] Fix `test_different_policies_produce_different_results` so its mock accepts current generator arguments while retaining winner assertions.
- [x] Replace `or True` and other vacuous checks with deterministic trade-off, feasibility/fallback and complete tie-break expectations.
- [x] Verify an isolated Python environment and capture tested dependency versions. Fresh `.venv-a2`, Windows x64 CPython 3.13.14; dependency snapshot in `constraints/windows-py313.txt`; imports and `pip check` pass. Setup documented in `docs/development.md`.
- [x] Separate new scratch runs (`results/scratch/`), ordinary runs (`results/runs/`), runtime state (`.runtime/`), curated replay (`results/demo/`) and versioned benchmarks. Ignore new transient artifacts; preserve all historical files. See `results/README.md`.
- [x] Run the complete offline suite and retain strict-replay/comparison-integrity coverage. On 2026-10-19, `PYTHON_DOTENV_DISABLED=1 .venv-a2/Scripts/python.exe -m pytest -q`: **547 passed in 17.12 seconds** in the fresh Windows environment; `pip check` reports no broken requirements. Published baseline preflight was 227 passing tests.

**Acceptance:** reproducible environment, passing offline tests, preserved artifacts and traceable source. Remote publication is not required to start implementation.

### M1 — Define deployments, requests and records

Primary files: `shared/schema.py`, `shared/run_records.py`, `shared/availability.py`, new deployment registry/configuration, `workflow/model_clients.py`, `.env.example`.

- [x] Add validated deployment configuration and separate endpoint, canonical/provider model and physical location (`shared/deployments.py`, `config/deployments.example.json`). Credential values are resolved only at call time.
- [x] Inventory without provisioning: the 2026-10-19 retry of `/api/tags` confirmed `qwen3.5:4b` and `llama3.2:3b` available at local Ollama after the earlier failed probe. No second destination or verified grid is configured. `workflow.deployment_inventory` records timestamped observations and unprobed cloud health as unknown.
- [x] Implement strict schema-3 requests/decisions/attempts, UTC, seconds, deadline/DAG validation, five-metric provenance and unknown values (`shared/scheduling.py`). Decisions retain workflow, policy and actual weights; dispatched/provider-reported identities remain distinct.
- [x] Preserve schema-1/2 readers and comparison/replay rules. Typed schema-3 JSON has a separate strict reader/writer (`workflow/scheduling_records.py`); explicit candidate migration converts integral legacy hours without inferring real geography. Historical runs are never silently promoted.
- [x] Dispatch through an explicit endpoint and exact mapped provider model (`workflow/deployment_clients.py`, `workflow/dispatch.py`). Network-free integration tests verify endpoint selection, future-start rejection and saved success/failure facts.
- [x] Define clock and carbon-data interfaces. Carbon samples carry region, issue time, validity interval, value, units, source and provenance. No live carbon-feed claim is made at this milestone.
- [x] Define comparison compatibility: actual workload identity/version, invariant configuration, deployment snapshot, calibration, policy implementation version, reference UTC and experiment mode. Policy selection and run ID do not determine cohort compatibility.

**Acceptance:** adapter tests verify the selected endpoint is used; unsupported pairs are rejected; unknown location stays unknown; records contain no credentials; legacy fixtures remain readable with accurate labels.

**Verification (2026-10-19):** all 547 offline tests pass, including explicit endpoint/model dispatch, typed record roundtrips, policy/weight audit fields, comparison isolation, invalid credentials, incomplete Ollama responses and legacy replay. Read-only review findings were fixed before the final run. See `docs/a2-foundations.md` for the API and `docs/development.md` for reproduction.

**Live follow-up (2026-10-19):** local Ollama 0.34.3 responded and reported both configured models. A schema-3 dispatch to `llama3.2:3b` completed with `READY`, 35 input tokens and 2 output tokens; selected and provider-reported model identities matched. Initial request wall time was 107.58 seconds; an immediate repeat took 2.38 seconds. Preserve loading/queue/network overhead in later latency calibration; this probe does not isolate its cause. Local evidence: `results/scratch/ollama-smoke-20261019T063740Z.json` and `results/scratch/ollama-warm-smoke-20261019.json` (ignored transient artifacts). Qwen inference and a second destination remain untested.

**Integration boundary:** the new one-step A2 dispatch API is available independently of the original schema-2 CLI/dashboard. M2 connects actual workflow data; M4/M5 wire joint planning and durable dispatch into the application. M1 does not establish live two-destination operation, delayed execution, measured quality or policy savings. Cloud generation was not invoked during foundation verification. CI is now configured for Windows and Linux; the changed workflow has not yet run remotely.

### M2 — Connect workflow inputs, outputs and deadlines

Primary files: `workflow/connected.py`, `workflow/workload.py`, `workflow/workflow_store.py`, `data/workloads/`, generation controls in the schema-3 contracts and deployment adapters. The original executor/dashboard retain their legacy path until later application integration.

- [x] Accept ticket text or a UTF-8 ticket file through `python -m workflow.connected`.
- [x] Classify the actual ticket, query a versioned fictional-product KB by category and ticket terms, synthesize retrieved evidence, retain source IDs and measure local lookup overhead. Missing evidence remains explicit.
- [x] Draft from actual ticket/classification/retrieval results and summarize actual preceding outputs. Unknown citations, invalid categories, empty output and explicit truncation stop downstream work.
- [x] Separate response and completion deadlines; `--response-only` saves the urgent reply for later background resume, while `--all-urgent` requires every step before the response deadline. Checks include materialization, journaling and dispatch; late provider failures remain deadline misses. Known violations persist atomically and are audited on restart.
- [x] Estimate actual rendered prompt sizes using an explicitly labeled UTF-8 byte proxy; retain provider token counts separately. Per-step output limits and nullable thinking controls reach the selected provider and are retained with attempts. Explicit unsupported cloud thinking controls fail before inference.
- [x] Atomically persist original input, rendered prompts, source metadata, attempts and validated outputs. Resume skips completed steps; configuration/version mismatches are rejected; interrupted dispatch and stale locks require reconciliation rather than automatic duplicate inference.

**Verification (2026-10-19):** **772 offline tests passed in 27.40 seconds** in `.venv-a2`. Live Ollama / `llama3.2:3b` run `5eefddb453a14cefbb07d60a1e74030a` reached `response_ready` with three attempts; a separate CLI invocation resumed only `summarize`, reaching `completed` with exactly four attempts. Evidence: `results/scratch/m2-live-connected-v4-20261019/workflow.json`. Generation ended with natural-stop metadata, with requested limits/thinking and actual usage retained. See `docs/connected-workflow.md` for commands.

**Retained failures and limits:** the earlier `m2-live-connected-20261019` and `m2-live-connected-v2-20261019` probes exposed truncation, templates and incorrect speaker role; `m2-live-connected-v3-20261019` was interrupted after summary dispatch during an interface change and correctly requires reconciliation. All remain untouched in `results/scratch/`. The successful smoke establishes data flow/resume, not semantic quality: its draft implied an account-status review that was not performed and included unnecessary MFA guidance. These are M3/M6 quality-review findings. Current execution is explicitly fixed and immediate, with unknown quality/cost/energy/carbon; joint scheduling, leased queues and measured savings remain incomplete.

**Acceptance:** upstream changes affect downstream prompts; dependencies hold; unsupported KB answers can be evaluated; failed prerequisites block dependents; workload version reflects prompt/data changes.

### M3 — Correct measurements and calibrate profiles

Primary files: `workflow/model_clients.py`, both benchmark scripts, `data/model_table.py`, `data/latency_model.py`, `data/eval/`, new `data/METHODOLOGY.md`.

- [x] Consolidate benchmarks around production prompt builders, model settings and adapters.
- [x] Retain every trial, including failures, warm-up/cold-start status, versions, hardware, tokens and timings. Record load/transfer overhead when available.
- [x] Fix unconditional measurement flags and `any(energy_measured_flags)` aggregation. Preserve CPU/GPU/RAM provenance; tracker availability does not prove sensor measurement.
- [x] Stop trackers in `finally`; never repeat inference because telemetry failed. Avoid nested trackers; document inclusion of the Ollama process, host idle energy and modeled components.
- [x] Calculate post-execution token-price cost from actual usage. Local API cost can be zero without implying zero electricity/ownership cost. Retain retry usage or mark it unknown.
- [x] Configure the actual local grid. Support a timestamped real carbon feed or documented historical trace; retain synthetic curves for simulation. Verify access/terms when selecting the source.
- [x] Define stale/missing-carbon behavior: explicitly labeled static estimate or unknown/infeasible, never silent synthetic substitution into live savings claims.
- [x] Start pilot calibration with a warm-up plus at least 10 retained trials per supported model/task/deployment profile, separating cold starts. This is not sufficient by itself for stable tail-latency claims.
- [ ] Run the existing 16-item evaluation live as a smoke check. Create separate calibration and held-out workflow examples, rubrics and manual review.
- [x] Add versioned workload-sized profiles and explicit unknown-profile fallback for the A2 planner; the historical demo remains separate. Record sample counts, uncertainty and held-out estimation error.

**Local evidence (2026-10-19):** `workflow.calibrate`, `data.profiles`, `shared.energy`, `data.carbon_sources` and `data.cost_accounting` implement the documented protocol in `data/METHODOLOGY.md`. The first pilot retained 88 trials, including 11 connection failures before Ollama was restarted. A separate 11-trial Llama classification follow-up preserved those failures and supplied ten successful classification measurements. Frozen aggregate: 99 retained trials, 90 non-warm-up attempts, eight profiles, at least ten valid natural completions per profile. Quality remains unknown, and CPU/GPU/RAM host telemetry totals remain mixed rather than wholly measured.

The independent held-out run retained 56 trials: 32 supported latency predictions, eight warm-up exclusions and 16 input-range exclusions. Mean absolute latency error was 0.236 seconds for the 32 covered predictions; this does not describe the excluded cases or a stable production tail. Evidence lives under `results/benchmarks/m3-local-*20261019/`; frozen profiles were not retrained on held-out outputs. Local carbon uses a documented all-India annual historical CO2 proxy, not an hourly Karnataka forecast. Explicit rubric review and complete end-to-end policy evaluation are still outstanding.

**Acceptance:** every number traces to a reading or assumption; mixed/simulated values cannot become wholly measured; realistic workload sizes drive estimates; calibration and final evaluation sets are separate. Local calibration can proceed while remote access is unresolved.

### M4 — Plan model, location and processing time jointly

Primary files: `scheduler/candidate_gen.py`, `scheduler/data_adapter.py`, `scheduler/scorer.py`, `scheduler/optimizer.py`, `scheduler/baseline.py`, decision contracts and tests.

- [x] Enumerate supported pairs/start windows within deadline and forecast horizons.
- [x] Model dependencies, remaining downstream execution and capacity reservations; calibrated duration includes adapter/load overhead, with no invented transfer estimate.
- [x] Implement constrained search and explicit infeasibility from section 4.
- [x] Validate custom inputs and preserve normalization snapshots and decision explanations.
- [x] Provide frozen-work replanning API and a minimum improvement threshold; automatic worker integration remains pending.
- [x] Deduct actual elapsed time after overruns in the replanning API before planning downstream work.
- [ ] Include waiting in comparisons and handle zero baselines without invented percentages.

**Acceptance:** deterministic fixtures independently demonstrate model, location and time changes; deadlines exclude unsuitable delays; outages/empty pools/invalid inputs are handled. Check small problems against exhaustive enumeration. Record planner overhead and any search truncation.

**Core verification:** `scheduler/joint.py`, `scheduler/profile_candidates.py` and `scheduler/plan_profiles.py` have deterministic model/location/time, capacity, parallel critical-path, exhaustive-oracle, strict persistence and bounded-search coverage. The frozen-context CLI made no inference calls and rejected all 36 real-profile candidates for unknown semantic quality. It is explicitly a planning scenario, not an executed connected workflow or a measured savings result. See `docs/joint-planner.md`.

### M5 — Execute decisions through a durable queue

Primary files: `workflow/executor.py`, new `workflow/queue_store.py` and `workflow/worker.py`, deployment adapters, run store and replay migration.

- [x] Implement SQLite enqueue/claim/update and a worker independent of Streamlit.
- [x] Dispatch only after prerequisites and scheduled start; record planned/actual times.
- [x] Revalidate health, forecast freshness and remaining deadline at dispatch; retain revised decisions (integrated path verified offline on 2026-10-26; reviewed live validation remains open).
- [x] Implement leases, bounded explicit retries, cancellation, restart recovery and ambiguous-call reconciliation; no automatic ambiguous retries.
- [x] Restrict pre-dispatch rerouting to eligible remaining deployments/models; preserve actual destination and constraint violations. Provider failures still require explicit retry; ambiguous inference is never automatically repeated.
- [x] Retain failed-attempt resource usage, including unknown values; preserve superseded late usage without overwriting a newer retry.
- [x] Run real immediate/delayed jobs and restart during a queued wait.
- [ ] Verify routing to two actual destinations using worker-side evidence or provider-supported deployment metadata.

**Acceptance:** queued work survives restart; atomic claims prevent concurrent duplicate dispatch; dependencies/cancellation work; fallback and failures remain visible; selected endpoints are actually called. Short-delay live tests prove queue mechanics, not meaningful carbon savings. Accelerated clocks remain simulation.

**Live local verification:** workflow `0db62cb834ba4e47aaca2427e02e85bb` reached `response_ready` with three attempts, then a second worker process executed only the background summary. Its scheduled start was `2026-10-19T12:54:49.597333Z`; provider dispatch began at `12:54:50.253558Z`, with 40.340 seconds waiting since prerequisite completion. Exactly four attempts completed. Database: `.runtime/m5-live-restart-20261019.sqlite3`; immutable export: `results/demo/a2-local-restart-20261019`. The draft contradicted the KB on password resets removing a lockout, and the summary repeated that error. This verifies durability and timing, not semantic quality or carbon savings. See `docs/durable-worker.md`.

### M6 — Evaluate the complete scheduler

Primary files: unified evaluation runner, curated datasets/results, methodology, comparison loader and benchmark report.

**Tooling update (2026-11-01):** `workflow.evaluate_policies`, `evaluation_protocol`, `planning_constraints` and `policy_comparison` implement protocol/source freeze, seeded paired trials, baselines/ablations, isolated queues and strict offline export. `workflow_quality` and `review_workflow_quality` add frozen-policy output review and comparison joins. Real human judgments, independent final inputs and controlled live measurement remain outstanding; experiment acceptance items below are not checked solely because their tooling exists.

- [ ] Freeze workload, rubric, constraints, profiles, candidate set and protocol before final measurement.
- [ ] Compare immediate fixed-model/deployment options, a calibration-selected fixed model-per-step assignment, model-only routing, and the joint scheduler.
- [ ] Include speed/balanced/eco comparisons and controlled ablations holding location or time fixed. Identical choices are valid.
- [ ] Measure end-to-end quality, completion/deadline misses, cost, energy, calculated carbon, planner overhead, retries and failures.
- [ ] Use paired workloads and repeated randomized/interleaved trials where feasible. Record actual conditions; equal reference timestamps do not make live runs simultaneous.
- [ ] Report variability/sample size; expand the pilot when differences are indistinguishable from noise.
- [ ] Attribute delayed-run carbon using actual execution time/location data where available. Separate forecast-based counterfactual savings and synthetic experiments from observed evidence.
- [ ] Vary uncertain energy/quality assumptions independently by deployment and show whether conclusions survive.
- [ ] Export a compatible checksummed live evidence bundle; verify strict replay makes no inference/network calls.

**Acceptance:** retained inputs/protocol reproduce the analysis; strong simple baselines are included; improvements do not hide quality/deadline violations; failed runs remain in reliability statistics. Modeled results alone cannot establish measured emissions reduction.

### M7 — Complete dashboard, replay and release demonstration

Primary files: `workflow/dashboard/app.py`, extracted views/data modules, `README.md`, `DEMO_GUIDE.md`, `DEMO_SCRIPT_LOCAL.md`.

- [x] Submit workflows with deadlines and policy; display queue progress/cancellation (offline AppTest verified).
- [x] Show selected/executed identities, geography, UTC times, deadlines and replan history.
- [x] Explain rejected alternatives, score contributions and infeasible requests.
- [x] Distinguish live/replay/simulation and retain metric provenance/coverage.
- [x] Display matched outcomes and attributable reviewed-quality fields, including partial/unknown evidence and zero baselines (real judgments still pending).
- [x] Keep SMTP-free local login and remove verification codes from logs; production identity remains deferred.
- [x] Document startup, destinations, carbon, worker/evaluation/replay and revised honest demo flow; historical guides retained.
- [ ] Rehearse in a clean environment; run offline and explicit live integration checks. Verify hosted CI if/when published.

**Acceptance:** a reviewer can follow input → decision → execution → evidence; all three dimensions and five metrics are demonstrated with limitations; documented commands reproduce the result.

Local tooling and clean Windows setup were verified. The remaining unchecked live/hosted-CI rehearsal and the full experimental acceptance are not claimed: no second host or human reviewer is available and the current local inference endpoint is down. See [release readiness](docs/release-readiness.md).

## 6. Validation matrix

| Requirement | Deterministic check | Live or retained evidence |
|---|---|---|
| Model choice | Controlled trade-off changes winner | Two models with actual outputs |
| Location choice | Eligibility and dispatch-target assertions | Two real destinations; known grids for geographic claims |
| Processing time | Fake-clock deadline/queue/restart tests | Actual wait and timestamps; real trace for carbon attribution |
| Dynamic behavior | Change availability/forecast/elapsed time and verify replan | Recorded replan; controlled endpoint-disable exercise |
| Latency | Critical path, waiting and retry accounting | End-to-end completion and deadline outcomes |
| Quality | Rubric checks and no mock-score promotion | Held-out outputs and human-reviewed judgments |
| Cost/energy/carbon | Units, provenance, unknowns and aggregation | Usage, telemetry/estimates, grid-source metadata |
| Reliability/replay | Lease race, corrupt fixture, ambiguous attempt, no-dispatch tests | Worker restart and exported live bundle |

## 7. Infrastructure and keys

- **No keys:** automated tests, simulation, queue development, local Ollama, local calibration, strict replay and OTP development login.
- **Second destination:** another accessible machine can host a worker without a model API subscription. Real geographic comparisons require a known distinct grid.
- **Cloud:** enable only providers used. Gemini needs its key; NVIDIA needs key, endpoint and exact model configuration. Generic API access does not establish region control. Verify current IDs/prices when configuring experiments.
- **Carbon:** use one suitable feed or a versioned historical dataset; credentials depend on the source. Record coverage and freshness.
- **Energy:** provider credentials do not automatically provide hardware telemetry. Label remote energy estimated unless direct evidence exists.

Resolve during M1/M3: second deployment access/location, carbon-data coverage, local sensor capability and evaluator expectations for a learned scheduler or simulation-only acceptance. Continue independent local work while these are pending; keep affected live gates incomplete.

## 8. Execution order and checkpoints

**Remaining release work:** real human judgments, supported context/calibration coverage, an available local backend, a second real destination, suitable timestamped carbon inputs and final controlled live measurements. The user confirmed no second destination and no assigned reviewer. Dashboard/review/evaluation integration and clean local setup are implemented and tested. Preserve infeasibility while required evidence is missing. The confirmed local location remains Mangaluru, Karnataka, India.

Order: M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7. Queue/clock scaffolding may begin after M1, but live scheduled dispatch depends on feasibility checks and the decision contract.

After each milestone update this file with status, changed files, commands/results, evidence paths and remaining dependencies. Do not mark simulated behavior as live completion.

Use the next **two weeks as a validation checkpoint, not a full-release deadline**. Review connected-workflow quality, calibration, meaningful controlled trade-offs, and feasibility of second-deployment/carbon-data access. Compare preliminary results to strong fixed strategies.

If infrastructure is unavailable, continue local and simulated work and record the missing live gate. An evaluator-approved simulation-only submission is a deliberate scope change. If calibrated policies repeatedly match a simple baseline, explain why and reassess added complexity rather than adjusting assumptions to manufacture savings.

Estimate the complete release schedule after the M1 inventory and M3 pilot. The previous 8–11 day immediate-execution demo estimate does not cover full A2.

Deferred: Kubernetes-scale placement, multi-tenant service, production SSO/billing, arbitrary workflow authoring, RL/learned optimization, extensive provider coverage, and publication-grade lifecycle emissions accounting. **Actual endpoint selection, durable delayed execution and workflow deadlines are not deferred.**

## 9. Progress log

| Date | Update | Validation / next action |
|---|---|---|
| 2026-10-18 | Replaced demo-only roadmap with A2 scope; preserved previous plan | Reviewed baseline: 226 passed / 1 failed. Next: M0. Planning did not complete an implementation milestone. |
| 2026-10-18 | GitHub publication preparation: repaired the stale test mock, corrected README paths/replay instructions and left optional cloud credentials blank in the template | Offline suite: 227 passed / 0 failed. M0 is in progress; isolated environment and remaining assertion improvements are not yet verified. |
| 2026-10-19 | M0/M1 foundations verified: isolated environment, meaningful scheduler assertions, explicit deployments, schema-3 records and typed dispatch | 547 offline tests passed; local Ollama inventory and Llama smoke later succeeded. |
| 2026-10-19 | M2 connected workload, deadlines, provider controls, retained completion reasons and resumable single-run journal verified | 772 offline tests passed; live response-only plus separate summary resume completed with exactly four attempts. Retained failed probes and semantic-quality limitations feed M3. |
| 2026-11-01 | P6 matched evaluation tooling: frozen common protocol/source, paired randomization, baseline/ablation restrictions, isolated durable trials, coverage-aware comparisons and portable replay | 1,891 full-suite tests plus one added immutable-package test passed. Frozen local pilot has 48 pending trials and no inference. Human quality, final live measurement and dashboard integration remain open. |
| 2026-11-01 | P6 workflow quality integration: frozen rubric/policy, actual-output packets, additive human approvals, quality coverage and paired differences in strict comparison exports | 1,912 full-suite tests passed. Review pilot remains unexecuted/unreviewed; actual human judgments and live release gates remain open. |
