# A2 Implementation Plan: Carbon- and Latency-Aware Agent Workflow Scheduler

Updated: 2026-10-18. This is the working plan for future implementation.

## 1. Objective

> Develop an AI scheduler that dynamically selects the execution location, model, and processing time for each agentic workflow step while balancing latency, accuracy, cost, energy consumption, and carbon emissions.

Build a scheduler whose choices control actual execution: **which model, on which deployment, at what time**. Evaluate the complete workflow against strong fixed execution strategies. Support-ticket processing is the first demonstration workload, not a restriction on the scheduler's eventual use.

All three decision dimensions are required for the A2 release. Region labels and future offsets in a chart establish a scenario, not executed routing or delayed processing. A simulation-only submission would require evaluator acceptance and an explicit scope change.

The previous plan is preserved at [docs/plans/IMPLEMENTATION_PLAN.pre-A2-2026-10-18.md](docs/plans/IMPLEMENTATION_PLAN.pre-A2-2026-10-18.md). Its decision to defer real routing and delayed execution is superseded. Its principles of honest measurement and reproducibility remain applicable.

## 2. Reviewed starting point

Baseline: local `main`, commit `b59e258`, with existing modified and untracked source files. Those changes are part of the reviewed baseline; the commit alone does not reproduce it. Preserve existing work and recordings. No current remote publication or CI status is assumed.

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

- [ ] An input-driven, connected workflow with an urgent response path and an explicitly deferrable background step.
- [ ] At least two models and two real execution destinations. Geographic carbon comparisons require known, distinct grid locations; two endpoints on one machine do not establish geographic shifting. Not every model must run at every destination.
- [ ] The chosen deployment actually receives the request; planned and executed identities are both retained.
- [ ] Immediate and delayed execution with deadlines, cancellation, durable state and restart recovery.
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

| ID | Milestone | Dependencies | Initial status |
|---|---|---|---|
| M0 | Stabilize the reviewed baseline | None | In progress |
| M1 | Define deployments, requests and records | M0 | Not started |
| M2 | Connect workflow inputs, outputs and deadlines | M1 | Not started |
| M3 | Correct measurements and calibrate profiles | M1, M2 | Not started |
| M4 | Plan model, location and processing time jointly | M1-M3 | Not started |
| M5 | Execute decisions through a durable queue | M1, M2, M4 | Not started |
| M6 | Evaluate the complete scheduler | M3-M5 | Not started |
| M7 | Complete dashboard, replay and release demonstration | M5, M6 | Not started |

### M0 — Stabilize the reviewed baseline

Primary files: `tests/test_scheduler.py`, dependency files, `.gitignore`, existing workflow/run/replay tests.

- [ ] Inventory existing changes/results and preserve unrelated work. Establish a traceable source snapshot or reviewed baseline commit.
- [x] Fix `test_different_policies_produce_different_results` so its mock accepts current generator arguments while retaining winner assertions.
- [ ] Replace `or True` and other vacuous checks with explicit feasibility/fallback expectations.
- [ ] Verify an isolated Python environment and capture tested dependency versions. Shared-machine success does not establish clean-install support.
- [ ] Separate new scratch runs, runtime SQLite state, curated fixtures and versioned benchmarks without deleting historical artifacts.
- [x] Run the complete offline suite and record its result, retaining strict-replay and comparison-integrity coverage. Publication preflight: **227 passed in 27.42 seconds** with dotenv disabled in the existing machine environment; isolated clean-install verification remains pending.

**Acceptance:** reproducible environment, passing offline tests, preserved artifacts and traceable source. Remote publication is not required to start implementation.

### M1 — Define deployments, requests and records

Primary files: `shared/schema.py`, `shared/run_records.py`, `shared/availability.py`, new deployment registry/configuration, `workflow/model_clients.py`, `.env.example`.

- [ ] Add validated deployment configuration and separate endpoint, model and physical location.
- [ ] Inventory actual local models and possible second deployments without provisioning paid resources.
- [ ] Implement request/decision/attempt contracts, units, deadline rules and unknown values.
- [ ] Add schema migration and legacy readers; exclude incompatible old records from new comparisons.
- [ ] Dispatch through an explicit deployment endpoint and exact model configuration.
- [ ] Define clock and carbon-data interfaces. Carbon samples carry region, issue time, validity interval, value, units, source and provenance.
- [ ] Define comparison compatibility: workload, configuration, deployment set, calibration, policy version, reference snapshot and experiment mode.

**Acceptance:** adapter tests verify the selected endpoint is used; unsupported pairs are rejected; unknown location stays unknown; records contain no credentials; legacy fixtures remain readable with accurate labels.

### M2 — Connect workflow inputs, outputs and deadlines

Primary files: `workflow/dag.py`, `workflow/executor.py`, new workload/prompt builders, versioned KB and ticket fixtures.

- [ ] Accept variable ticket input.
- [ ] Use classification to query a small actual knowledge base. Let the retrieval step select/synthesize retrieved evidence and preserve source IDs; account for lookup overhead.
- [ ] Build drafts from actual ticket/classification/retrieval results; summarize actual preceding outputs.
- [ ] Separate the urgent response deadline from a longer CRM-summary deadline. Also support a strict all-urgent case.
- [ ] Estimate prompt size from actual context and record estimator provenance. Set per-step output limits and thinking settings.
- [ ] Persist outputs so restart does not regenerate completed steps.

**Acceptance:** upstream changes affect downstream prompts; dependencies hold; unsupported KB answers can be evaluated; failed prerequisites block dependents; workload version reflects prompt/data changes.

### M3 — Correct measurements and calibrate profiles

Primary files: `workflow/model_clients.py`, both benchmark scripts, `data/model_table.py`, `data/latency_model.py`, `data/eval/`, new `data/METHODOLOGY.md`.

- [ ] Consolidate benchmarks around production prompt builders, model settings and adapters.
- [ ] Retain every trial, including failures, warm-up/cold-start status, versions, hardware, tokens and timings. Record load/transfer overhead when available.
- [ ] Fix unconditional measurement flags and `any(energy_measured_flags)` aggregation. Preserve CPU/GPU/RAM provenance; tracker availability does not prove sensor measurement.
- [ ] Stop trackers in `finally`; never repeat inference because telemetry failed. Avoid nested trackers; document inclusion of the Ollama process, host idle energy and modeled components.
- [ ] Calculate post-execution token-price cost from actual usage. Local API cost can be zero without implying zero electricity/ownership cost. Retain retry usage or mark it unknown.
- [ ] Configure the actual local grid. Support a timestamped real carbon feed or documented historical trace; retain synthetic curves for simulation. Verify access/terms when selecting the source.
- [ ] Define stale/missing-carbon behavior: explicitly labeled static estimate or immediate execution according to policy, never silent synthetic substitution into live savings claims.
- [ ] Start pilot calibration with a warm-up plus at least 10 retained trials per supported model/task/deployment profile, separating cold starts. This is not sufficient by itself for stable tail-latency claims.
- [ ] Run the existing 16-item evaluation live as a smoke check. Create separate calibration and held-out workflow examples, rubrics and manual review.
- [ ] Replace fixed token budgets and unused latency tables with versioned profiles and an explicit unknown-profile fallback. Record sample counts, uncertainty and held-out estimation error.

**Acceptance:** every number traces to a reading or assumption; mixed/simulated values cannot become wholly measured; realistic workload sizes drive estimates; calibration and final evaluation sets are separate. Local calibration can proceed while remote access is unresolved.

### M4 — Plan model, location and processing time jointly

Primary files: `scheduler/candidate_gen.py`, `scheduler/data_adapter.py`, `scheduler/scorer.py`, `scheduler/optimizer.py`, `scheduler/baseline.py`, decision contracts and tests.

- [ ] Enumerate supported pairs/start windows within deadline and forecast horizons.
- [ ] Model dependencies, remaining downstream execution, capacity reservations and transfer/load overhead.
- [ ] Implement constrained search and explicit infeasibility from section 4.
- [ ] Validate custom inputs and preserve normalization snapshots and decision explanations.
- [ ] Replan at meaningful boundaries; define a minimum improvement threshold to avoid unnecessary schedule changes.
- [ ] Deduct actual elapsed time after overruns before planning downstream work.
- [ ] Include waiting in comparisons and handle zero baselines without invented percentages.

**Acceptance:** deterministic fixtures independently demonstrate model, location and time changes; deadlines exclude unsuitable delays; outages/empty pools/invalid inputs are handled. Check small problems against exhaustive enumeration. Record planner overhead and any search truncation.

### M5 — Execute decisions through a durable queue

Primary files: `workflow/executor.py`, new `workflow/queue_store.py` and `workflow/worker.py`, deployment adapters, run store and replay migration.

- [ ] Implement SQLite enqueue/claim/update and a worker independent of Streamlit.
- [ ] Dispatch only after prerequisites and scheduled start; record planned/actual times.
- [ ] Revalidate health, forecast freshness and remaining deadline at dispatch; retain revised decisions.
- [ ] Implement leases, bounded retries, cancellation, restart recovery and ambiguous-call reconciliation.
- [ ] Restrict fallback to eligible remaining deployments/models; preserve actual destination and constraint violations.
- [ ] Retain failed-attempt resource usage, including unknown values.
- [ ] Run real immediate/delayed jobs and restart during a queued wait.
- [ ] Verify routing to two actual destinations using worker-side evidence or provider-supported deployment metadata.

**Acceptance:** queued work survives restart; atomic claims prevent concurrent duplicate dispatch; dependencies/cancellation work; fallback and failures remain visible; selected endpoints are actually called. Short-delay live tests prove queue mechanics, not meaningful carbon savings. Accelerated clocks remain simulation.

### M6 — Evaluate the complete scheduler

Primary files: unified evaluation runner, curated datasets/results, methodology, comparison loader and benchmark report.

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

- [ ] Submit workflows with deadlines and policy; display queue progress/cancellation.
- [ ] Show selected/executed model and deployment, known/unknown geography, planned/actual start, deadline and replan history.
- [ ] Explain rejected alternatives, score contributions and infeasible requests.
- [ ] Clearly distinguish live, replay and simulation, with provenance beside every metric.
- [ ] Display matched comparisons, actual output quality and completion outcomes. Handle partial runs, absent evidence and zero baselines.
- [ ] Keep SMTP-free development login; defer production identity work.
- [ ] Document local startup, adding a destination, carbon data, worker commands, evaluation and replay.
- [ ] Rehearse in a clean environment; run offline and explicit live integration checks. Verify hosted CI if/when published.

**Acceptance:** a reviewer can follow input → decision → execution → evidence; all three dimensions and five metrics are demonstrated with limitations; documented commands reproduce the result.

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

**Next implementation task: M0.** Fix the failing mock, strengthen ineffective assertions, establish the environment and rerun the suite. Then implement contracts before adding dashboard features.

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
