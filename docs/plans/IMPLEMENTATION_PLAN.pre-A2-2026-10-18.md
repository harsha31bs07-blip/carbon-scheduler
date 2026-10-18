# Carbon Scheduler — Implementation Plan

Updated 2026-10-09 against local commit `b59e258` and the authenticated GitHub repository.

**Next milestone:** a credible, repeatable demo with defensible benchmark results. The user selected this target. The priority is correct execution records, comparable experiments, calibrated scheduling, and clear presentation of measured versus modeled results.

## Verified starting point

| Area | Current state |
|---|---|
| GitHub | CLI authenticated as `harsha31bs07-blip`; `origin` points to [harsha31bs07-blip/carbon-scheduler](https://github.com/harsha31bs07-blip/carbon-scheduler). The public remote is empty: no branches, commits, issues, PRs, or Actions runs. |
| Local Git | `main` at `b59e258`, five commits, no configured upstream. Ten untracked run files in `results/`; existing tracked source files were unchanged before this plan update. |
| Tests | **89 passed in 11.36 seconds** on Python 3.13.14, with dotenv disabled and model calls mocked. This verifies offline logic, not model availability or benchmark accuracy. |
| Data and scheduler | One mode-aware candidate pool, common estimate factory, latency/accuracy filtering, normalized policy scores, custom weights, baseline, and deferral penalties in scoring are implemented. |
| Execution | Ollama, Gemini, and NVIDIA clients; four steps execute in DAG order with JSONL logs. Prompts are fixed and independent: preceding outputs are not consumed downstream. |
| Dashboard | Streamlit with policies, custom weights, run comparisons, and OTP development mode. Current logging and display disagree in several places. |
| Evidence | Committed accuracy evaluation is simulated (`is_measured: false`). Cloud energy and hourly carbon curves are assumptions. Local energy can use sensors or estimates, but its provenance is not consistently preserved. |

No applicable `AGENTS.md` was found. The original `PERSON_A/B/C_*.md` documents describe module responsibilities; use those roles as ownership guidance if multiple contributors are available.

## Problems that determine the order

| Priority | Finding and source | Consequence |
|---|---|---|
| P0 | `workflow/executor.py::_write_result` omits policy and custom weights; `workflow/dashboard/app.py::load_all_runs` labels these records `unknown` and comparison excludes them. | Newly executed scheduled runs can disappear from the headline comparison. |
| P0 | Replay searches by step/model and file modification time, returns no output text, and makes live calls on a miss (`executor.py::_get_replay_data`, `execute_step`). | The offline demo is not deterministic or guaranteed to remain offline. |
| P0 | Selected region is not passed to clients; future offsets are displayed but never waited for (`executor.py::_dispatch`, `execute_step`). | Region changes and deferral savings are projections, not executed routing or measured reductions. |
| P0 | Local clients can raise `ConnectionError`/`TimeoutError`, but executor fallback catches `RuntimeError`/`AuthError`. Retry code can classify other non-transient failures as auth failures (`workflow/model_clients.py`). | Real outages do not follow the fallback behavior covered by current tests. |
| P0 | Local measured energy fields are dropped by the executor. Benchmark code returns `measured: true` regardless of sensor flags and uses `any(...)` for mixed samples (`workflow/benchmark_local_models.py`). | Estimates or mixed measurements can appear as fully measured energy. |
| P1 | Fixed task token counts drive estimates while local per-step latency tables are unused (`scheduler/data_adapter.py`, `data/model_table.py`). | In a fixed-time offline probe, retrieve and summarize had no feasible candidates; both fell back to Qwen at 10,500 ms versus 6,000/8,000 ms limits. These are estimator results, not newly measured timings. |
| P1 | Constraint fallback returns an ordinary candidate with only a warning (`scheduler/scorer.py`); comparison totals omit deliberate waiting (`scheduler/baseline.py`). | The UI cannot explain infeasibility, and planned completion time is understated. |
| P1 | Custom NaN weights are accepted; fractional offsets are used for carbon then truncated in the candidate; several tests have ineffective assertions. | Invalid inputs and decision regressions can pass unnoticed. |

## Demo contract

The release should demonstrate a real local four-step workflow and a deterministic replay of the same workload. Hybrid runs are an additional evidence set when cloud access is available.

Every comparison must share a workload version, provider mode, calibration version, baseline rule, and reference timestamp. Live comparisons also record hardware, model configuration, and trial conditions. Equal policy choices and zero savings are valid outcomes; do not adjust assumptions to force visible differences.

For this milestone, execute on the configured local/cloud endpoint. Keep alternate regions and future windows in an explicitly labeled **scenario preview**. Live selection uses only executable deployments and zero deferral. A real delayed queue and provider-specific regional routing belong to a later milestone.

The demo must distinguish observed latency, token-count provenance, modeled token-price cost, sensor/estimated energy, and calculated carbon. Synthetic future carbon is always a projection. It must show constraint violations and fallback decisions instead of implying all SLAs were met.

## Milestones and dependencies

Estimates assume one implementer, working Ollama models, and accessible hardware. Allow approximately **8–11 working days**, plus any model downloads, provider access delays, or repeated benchmark collection. Start with M0, then M1; M2 supplies the evidence needed by M3. Complete the final dashboard after the record and decision contracts stabilize.

| Milestone | Estimate | Main owner | Deliverable |
|---|---|---|---|
| M0 — Establish a reproducible GitHub baseline | 0.5 day | Repository maintainer | Reviewed publication scope, isolated setup, green remote CI |
| M1 — Make runs and replay trustworthy | 1.5–2 days | Workflow | Versioned logs, strict replay, usable comparisons, consistent errors |
| M2 — Build reproducible workload evidence | 2–3 days | Data + Workflow | Output chaining, corrected measurements, benchmark and live eval artifacts |
| M3 — Calibrate and explain scheduling | 1–2 days | Scheduler + Data | Realistic estimates, explicit feasibility, reproducible policy decisions |
| M4 — Deliver an honest demo dashboard | 1–2 days | Workflow/dashboard | Matched comparisons, provenance labels, decision explanation |
| M5 — Rehearse and release | 0.5–1 day | Maintainer + contributors | Clean-checkout validation, release evidence, repeatable demo script |

### M0 — Establish a reproducible GitHub baseline

1. Review the exact `main` history and shareable result artifacts before the first public push. `.env` is currently ignored and untracked. Keep local tool/checkpoint refs out of publication; publish `main`, not all refs or a mirror.
2. Separate curated fixtures from generated experiments. Keep a small, documented `results/demo/` fixture set; store new scratch runs outside it and ignore them. Inventory existing tracked recordings before replacing them; preserve the ten existing untracked files.
3. Create an isolated Python 3.13 environment and record tested dependencies in a reproducible lock/constraints file. The successful test run used the shared machine environment, which includes a development build of `requests`; do not treat that as a clean-install guarantee.
4. During implementation, publish the reviewed baseline with `git push -u origin main`, then verify the existing Actions workflow actually passes. Use feature branches and small PRs; require the test check once it is running.

**Acceptance:** a clean checkout installs and passes the offline suite; GitHub has `main`, an upstream, and a successful Actions run; secret/config files and scratch experiments stay out of the published change. GitHub access was verified during planning; no push or repository settings change was performed.

### M1 — Make runs and replay trustworthy

**Files:** `workflow/executor.py`, `workflow/model_clients.py`, a new shared run-record contract, dashboard data loading, and `workflow/tests/test_workflow.py`.

1. Define a versioned run record and manifest. Include policy/custom weights, workload/prompt version, comparison ID, provider mode, model ID/configuration, reference UTC time, code revision, calibration ID, execution mode, and units. Preserve legacy loading with explicit unknown provenance; do not infer a historical policy from new defaults.
2. Store the scheduled decision separately from the executed model/deployment and fallback attempts. Retain output text for chaining/replay, observed latency and token-count source, candidate estimates, energy value/method/component provenance, and measured deferral (zero in this milestone). Persist enough failure information to explain incomplete runs.
3. Restore policy metadata first so new scheduled runs load into the comparison view. Compare explicitly paired complete runs, not whichever baseline and scheduled files happen to be newest.
4. Extract one execution helper shared by baseline and scheduled paths. Standardize authentication, configuration, timeout, connection, rate-limit, and provider-response failures; preserve fail-fast authentication and test real exception types.
5. Make replay strict: a fixture manifest selects exact compatible recordings, retains outputs and source IDs, and never reads newly generated replay files as new evidence. On a missing/invalid recording, stop with an actionable message and make no model call. Any later live-on-miss mode must be a separate explicit option.
6. Restrict live candidates to actual configured deployments with offset zero. Keep region/time exploration in a separate preview path and mark it projected in every record and display.

**Acceptance:** baseline and scheduled records use the same contract; policy runs appear in comparisons; missing replay data cannot call Ollama or cloud APIs; connection/timeout/auth tests exercise the advertised fallback behavior; a failed or partial run cannot become a successful comparison.

### M2 — Build reproducible workload evidence

**Files:** `workflow/executor.py`, `workflow/dag.py`, `workflow/model_clients.py`, both benchmark scripts, `data/eval/`, and methodology documentation.

1. Replace independent fixed prompts with a versioned ticket input and downstream prompt builders. Classification feeds retrieval context, retrieved text feeds drafting, and actual interaction outputs feed summarization. Keep the initial workflow small and retain its full outputs in curated fixtures.
2. Consolidate the two benchmark paths around the same prompt builders and model settings as execution. Start with one warm-up and at least ten retained trials for each of two local models across four steps; retain every trial. Record cold/warm status, model/Ollama version, hardware, token limits, thinking mode, timestamp, failures, and sensor method.
3. Use short task-specific output limits and disable thinking for mechanical steps. Freeze those settings before collection. Report median and spread; report p95 as exploratory until the sample supports a stable tail estimate. Record token throughput with its exact numerator and definition.
4. Fix measurement provenance before collecting evidence. Sensor absence must produce an estimate label; keep CPU/GPU/RAM provenance separately and label mixed samples as mixed. Stop trackers in `finally`, avoid repeating inference accidentally after a measurement failure, and disclose whether energy includes the Ollama process, host idle power, and modeled components. Short calls may require longer batches for meaningful sampling.
5. Run the existing 16-item eval against both local models without simulated fallback, using task-appropriate context/settings. Retain individual outputs, rubric scores, sample counts, and model/configuration IDs. Manually check representative outputs. Describe the result as rubric performance on a small set, not universal accuracy; do not overwrite live evidence with a later mock run.
6. On a small fixed ticket set, run the baseline and every compared policy end to end. Use one rubric for task completion, response correctness, and summary fidelity; manually review outputs and store per-run quality beside performance. This checks whether a lower-energy mixed-model workflow preserves useful outcomes, beyond isolated model scores.
7. Write `data/METHODOLOGY.md`: actual local grid/origin configuration, grid source/year, energy scope, PUE treatment, token-price assumptions, synthetic time curves, and measurement limitations. Remove the implicit claim that the developer's hardware is in the default US grid.
8. If cloud models are accessible, collect a separate hybrid evaluation with the same workload. Verify the configured NVIDIA model matches its table identity and check current model/pricing documentation before recording dated assumptions. Keep cloud energy assumed unless supported by evidence; vary it independently from local energy for sensitivity analysis. Scaling every model equally does not test the local/cloud assumption.

**Acceptance:** every aggregate links to retained trials; no estimate or mixed aggregate is marked wholly measured; complete local live eval has `is_measured: true`; each compared policy has end-to-end rubric results on the same tickets; demo artifacts include workload, outputs, hardware/settings, methodology, and provenance. A cloud outage does not block the local milestone.

### M3 — Calibrate and explain scheduling

**Files:** `data/model_table.py`, `data/latency_model.py`, `scheduler/data_adapter.py`, `scheduler/candidate_gen.py`, `scheduler/scorer.py`, `scheduler/optimizer.py`, `scheduler/baseline.py`, and focused tests.

1. Feed M2 token/latency observations into versioned per-model/per-step profiles. Remove unused tables or make them authoritative; keep one factory for scheduler, baseline, and fallback. Validate estimates against retained observations and document error instead of claiming exact prediction.
2. Keep current SLAs while tuning output/settings and collecting data. Then document realistic per-step budgets for the demo hardware. If a limit remains infeasible, show it explicitly; revise a budget only with dated benchmark evidence and a stated reason.
3. Add a scheduling decision wrapper containing the winner, feasible count, rejected constraints, fallback reason, raw metrics, normalized metrics, and weighted contributions. Preserve `schedule_step()` compatibility for current callers while exposing the richer decision to execution/UI.
4. Carry one reference UTC timestamp through all steps, policies, baseline, and fallback estimates. Distinguish inference SLA from a completion deadline. Scenario scoring and totals include planned waiting; live runs report actual execution time and zero deferral.
5. Validate custom weights as finite, non-negative numbers with a positive total; reject unsupported keys. Reject fractional offsets while the contract remains integer hours. Make zero/absent latency limits and accuracy defaults explicit. Handle an empty candidate pool with a clear decision error.
6. Replace ineffective assertions, including `or True`, with fixed-time synthetic trade-off tests. Cover explicit infeasibility, unknown steps, missing providers, all-equal metrics, NaN/infinite weights, unsupported offsets, and consistency between scoring and comparison totals. Check policy behavior on controlled fixtures; real calibrated policies may legitimately choose the same model.

**Acceptance:** a decision is reproducible from its stored context; infeasible choices are visible in logs/UI; speed does not defer on controlled trade-off fixtures; preview wait time is accounted for; local/hybrid assumptions and sensitivity results explain any policy convergence.

### M4 — Deliver an honest demo dashboard

**Files:** split `workflow/dashboard/app.py` into thin app orchestration, authentication, data loading, and chart/decision rendering modules.

1. Show live execution, replay, and scenario preview distinctly. Use dynamic shared availability and the actual baseline rule rather than hardcoded provider/model text.
2. Load compatible complete run pairs by comparison ID and display their workload/configuration. Explain mismatches rather than comparing unrelated files. Handle malformed records, unknown legacy metadata, missing fixtures, and zero-valued baselines without inventing values.
3. Label latency, tokens, cost, energy, carbon, and rubric quality with their provenance. Derive modeled cost from the applicable price and token counts; neither assumed pricing nor a cloud call makes cost measured. Identify replayed values and projected carbon windows.
4. Display scheduling explanations, constraint status, executed fallback, and planned versus actual time. Place custom-weight sliders and candidate scatterplots in preview so exploratory scenarios cannot be mistaken for executed savings.
5. Keep the existing development OTP path documented and SMTP-independent for the local demo. Defer production identity/access work. Test extracted loading/aggregation logic and smoke-test Streamlit; finish with a visual walkthrough of complete, partial, replay, and zero-savings cases.

**Acceptance:** a viewer can identify the executed model, the reason it was selected, any violated constraint, and the source of every displayed number; a fresh replay visibly matches its recorded fixture and needs neither SMTP nor model servers.

### M5 — Rehearse and release

1. From a clean checkout, install the pinned environment, run offline tests, launch the dashboard, and complete a strict replay with network/model calls disabled.
2. Run the live local pipeline and paired baseline/policy trials using the frozen workload/configuration and fixed tickets. Preserve failures, variability, and per-run rubric quality. Compare modeled carbon separately from measured execution evidence and report zero or negative savings honestly.
3. Curate the fixture manifest and benchmark/eval report; update `README.md`, `DEMO_GUIDE.md`, and `DEMO_SCRIPT_LOCAL.md` with exact commands and a short walkthrough. Archive stale measurements as legacy instead of silently presenting them as current evidence.
4. Verify GitHub CI and prepare a tagged demo release with methodology, sample sizes, run IDs, calibration version, and limitations.

**Release gate:** complete reproducible four-step execution; strict offline replay; passing CI; matched comparisons with end-to-end quality scores; live local rubric evaluation; correctly labeled energy/carbon/cost; explicit SLA and fallback outcomes; published instructions that work from a clean checkout.

## Suggested implementation PRs

| Order | Proposed title | Dependency / principal review check |
|---|---|---|
| 1 | Establish reproducible setup and curated demo artifact layout | M0; exact publication scope and clean-install test |
| 2 | Preserve run configuration and compare complete compatible runs | PR 1; new scheduled logs appear in comparison |
| 3 | Make replay deterministic and unify execution errors | PR 2; missing recordings never dispatch; real timeout/connection fallback tests |
| 4 | Chain workflow outputs and correct benchmark provenance | PR 3; stored outputs drive subsequent prompts; sensor/estimate/mixed labels |
| 5 | Add reproducible calibration and local evaluation evidence | PR 4; retained trials, methodology, frozen settings, measured-only eval |
| 6 | Explain scheduler feasibility and calibrate policy inputs | PR 5; profile validation, fixed-time decisions, constraint/input tests |
| 7 | Present matched runs and projected scenarios clearly | PRs 2–6; dashboard smoke test and visual review |
| 8 | Publish a repeatable demo release | PR 7; clean-checkout rehearsal and green remote CI |

**First code change after setup:** restore policy/configuration metadata in the executor and extract comparison loading into a testable module. This fixes a demonstrated demo blocker and establishes the record contract the remaining work depends on.

## Deferred beyond this milestone

- Durable delayed execution, cancellation, queue persistence, and completion deadlines across real hours of waiting.
- Provider-specific regional deployments and live grid forecasts.
- Hosted multi-user service, production authentication, quotas, and operational monitoring.
- Broad quality benchmarks, statistically stable tail-latency claims, and publication-grade energy accounting.

These require additional scope and evidence. The demo milestone remains complete when the release gate above is met.
