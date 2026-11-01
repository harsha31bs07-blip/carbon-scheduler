# Carbon Scheduler: project analysis and completion plan

Reviewed: 2026-10-26. Scope: complete the A2 prototype described in the repository's [implementation roadmap](../../IMPLEMENTATION_PLAN.md). This is an implementation-ready supplement to that roadmap; it preserves the existing milestone history and narrows the remaining work into ordered changes.

## Assessment

The project has substantial working foundations. It is a Python application with a Streamlit interface, a SQLite coordinator, explicit inference adapters, empirical profiles, and a bounded joint scheduler. The main remaining work is connecting these components into one validated execution and evaluation path.

The intended result is a support-ticket workflow that chooses **model, destination and start time** while respecting dependencies, quality requirements and deadlines, then records latency, quality, cost, energy and carbon with their provenance. The current live worker executes a fixed assignment. Joint planning exists separately and correctly rejects the retained local profiles because semantic quality has not been reviewed.

Do not rebuild the scheduler, replace SQLite, or add a learned router as part of this release. Finish the existing contracts and integration first. Production multi-tenancy, billing, arbitrary workflow authoring and distributed coordination remain outside this plan.

## Review evidence and limitations

- Reviewed branch: `feat/a2-foundations`, HEAD `e579016`, plus the substantial pre-existing modified and untracked working tree. HEAD alone does not identify the reviewed implementation. No applicable `AGENTS.md` was found in the repository scan.
- Reviewed source includes `shared/scheduling.py`, deployment contracts/adapters, profile construction, candidate generation, joint planning, connected workflow rendering, worker/queue boundaries, replay, and the queue dashboard. Existing documentation and retained JSON evidence informed the assessment.
- Full offline suite: **1,370 passed, 1 failed in 91.06 seconds**. The failure was `tests/test_queue_dashboard.py::test_submission_is_durable_and_cancel_does_not_call_provider`: Streamlit `AppTest` exceeded its default three-second timeout.
- Focused rerun: **all four queue dashboard tests passed in 4.96 seconds**. This suggests timing sensitivity; it does not establish the root cause or erase the full-suite failure.
- Test commands, with dotenv disabled, were `.\.venv-a2\Scripts\python.exe -m pytest -q -p no:cacheprovider` and the same command with `tests/test_queue_dashboard.py`. Sandbox permissions initially blocked the virtual environment's base Python; the actual runs used elevated sandbox access.
- No new live inference, endpoint probes, cloud provisioning, source-data verification, or remote CI verification was performed for this review. Prior live results below are retained evidence, not fresh live validation. Runtime source was not changed.

## Current state

| Area | What already exists | Remaining release gap |
|---|---|---|
| Contracts and deployments | Schema-3 requests, candidates, attempts, UTC deadlines, explicit endpoint/model registry and location provenance | Evolve persisted decision context and compatibility for integrated planning |
| Connected workload | Actual ticket inputs, versioned local KB, chained outputs, response/background split, validation and resume | Improve and review semantic correctness; cover actual generated context sizes |
| Calibration | Retained trials, split isolation, latency/resource estimates, frozen profiles and held-out error reporting | Human rubric review, coverage expansion and reproducible reviewed-profile construction |
| Joint planner | Model/destination/start selection, dependencies, capacity, five metrics, bounded search, frozen-work replanning | Live request preparation, actual-output-aware replanning and worker orchestration |
| Durable worker | Leases, heartbeats, deadlines, cancellation, explicit retries, late-attempt retention and restart recovery | Optimized submission, fresh eligibility checks and atomic plan/claim coordination |
| Accounting | Cost, energy and carbon helpers used in calibration | Durable dispatch currently records latency and tokens but leaves quality/cost/energy/carbon unknown |
| Dashboard | Durable fixed submission, deadlines, progress, cancellation, planned/executed identities and outputs | Policy submission, decision explanations, replans and matched comparisons |
| Replay | Strict queue bundle export/load, plus a separate legacy replay path | Integrated decision/accounting evidence and matched policy evaluation replay |
| Infrastructure | Example registry has one local Ollama destination with two models | Second real destination; appropriate carbon observations for geography/time claims |
| Validation | Broad automated suite and retained local calibration/restart evidence | Stable full-suite result, fresh integrated live evidence and release CI |

The frozen file `results/benchmarks/m3-local-frozen-20261019/profiles.json` contains eight model/task profiles; every profile has **zero reviewed quality samples**. `results/benchmarks/m3-legacy16-live-20261019.json` contains 32 completed records across two models, explicitly marked semantically unreviewed. Running that smoke again is not a substitute for quality review.

The current workload fixture has five calibration tickets and six held-out tickets. Repeated trials characterize execution variability but do not create more independent task examples. The existing held-out outputs have already informed development findings; use a new untouched final evaluation set after any resulting prompt or model tuning.

## Problems that determine implementation order

1. **Quality blocks real optimization.** `data/profiles.py` requires reviewed sources, and the retained profiles have none. Do not weaken this gate, import keyword scores as semantic quality, or lower a threshold merely to obtain a winner.
2. **Full downstream prompts do not exist at submission.** `generate_profile_candidates()` expects materialized dependency outputs, whereas `scheduler.plan_profiles` uses frozen fixture context and labels its result a simulation. Calling that CLI from the live worker would not solve live planning.
3. **Claiming and replanning must be coordinated.** `work_once()` claims before rendering/dispatch, and `QueueStore.replan()` only changes unclaimed scheduled work. A planner call inserted after claim cannot safely update the selected job. Replanning also needs to respect capacity occupied by other workflows.
4. **Execution and calibration use different accounting paths.** `dispatch_planned_candidate()` currently emits unknown non-latency resource observations. The release report must use executed attempts and actual-time attribution, with estimated and unknown values preserved.
5. **Local evidence cannot establish geographic or temporal carbon savings.** The supplied carbon configuration is an annual static proxy. It can support labeled carbon estimates, but its constant intensity provides no time-of-day benefit. Two model names on one machine do not establish two execution destinations.
6. **Documentation understates some completed work.** The legacy live smoke, basic queue dashboard and local queue replay bundle already exist. Conversely, those do not establish full joint dispatch or a matched policy comparison. Track these capabilities separately.

## Ordered implementation work

### P0 — Stabilize and record the reviewed baseline

Dependencies: none. Existing roadmap: M0 and release hygiene.

- Preserve all current work and historical recordings. Capture a source manifest or checkpoint identifying the implementation used for subsequent experiments; review untracked source before publication.
- Investigate the dashboard timeout under full-suite load. Check initialization/import cost and thread/SQLite cleanup. Use an explicit, justified test timeout if startup variance is the cause; retain assertions for durable submission, cancellation and absence of inference.
- Update stale status notes without replacing historical results. Distinguish the existing queue bundle from the absent legacy matched-comparison bundle.
- Verify dependency consistency in the existing environment and then the configured Windows/Linux CI environments when changes are published. Treat dependency API deprecation warnings as maintenance work, not the proven cause of the test failure.

Files: `tests/test_queue_dashboard.py`, `workflow/dashboard/queue_view.py` as warranted, `docs/development.md`, `README.md`, `IMPLEMENTATION_PLAN.md`, `.github/workflows/tests.yml` only if needed.

Acceptance: a passing complete offline run, a documented diagnosis for the intermittent failure, reproducible environment commands, and an identifiable source snapshot. Do not claim hosted CI passed before observing it.

### P1 — Make quality evidence usable and expand profile coverage

Dependencies: P0. Existing roadmap: M3.

- Define a versioned scoring rubric for classification, evidence relevance, grounded drafting, unsupported questions and faithful summaries. Explicitly score invented account actions, contradictions, unsupported promises and missing escalation guidance.
- Add review export/import tooling with separate immutable review records: trial/output hash, case and split, rubric version, reviewer, timestamp, criterion judgments, score and rationale. Validate that a review refers to the exact retained output. Preserve raw calibration journals.
- Join approved calibration reviews during profile freezing. Extend the profile fingerprint to cover review content and rubric identity. Reject duplicate/conflicting judgments, mismatched outputs, mixed rubric scales and held-out leakage. An automated suggestion must not be labeled a human judgment.
- Review retained calibration outputs first. If prompt or generation settings need improvement, version them and collect compatible new trials; old profiles cannot silently describe the changed workload.
- Expand calibration examples and prompt-size coverage, including downstream contexts produced by real workflow runs. Current classify profiles cover only 550–590 input bytes. Quantify coverage/exclusions and keep unsupported configurations unknown.
- Freeze quality floors and an explicit minimum review coverage rule before policy evaluation. Keep quality failure and execution failure in reporting even when latency fitting uses only valid completions.
- Add a new final evaluation split isolated from calibration and development feedback. Keep the already-inspected held-out set as development validation history.

Files: `data/profiles.py`, `workflow/profile_report.py`, `workflow/calibrate.py`, `workflow/workload.py`, `data/workloads/`, `data/METHODOLOGY.md`; proposed `workflow/review_quality.py` and versioned review artifacts.

Acceptance: profiles can be rebuilt from retained trials plus attributable reviews; unknown/unreviewed quality stays ineligible; leakage and review tampering tests pass; reviewed eligibility and profile coverage are reported for every supported task/model/deployment. Feasibility is an outcome to measure, not a required score to manufacture.

### P2 — Define the live planning boundary

Dependencies: P1 for real profiles; implementation can use clearly simulated fixtures earlier. Existing roadmap: M4.

- Introduce one service for live submission and replanning, shared by CLI and dashboard. Inputs include policy/weights, deadlines, quality floor, profile versions, current health/carbon and capacity snapshots.
- Plan in stages. At submission, only ready steps have actual prompts. For unfinished downstream steps, use explicitly labeled, calibrated workload-size estimates/bounds with retained assumptions; do not pass expected fixture answers off as observed outputs. If downstream coverage is insufficient, report an unsupported/infeasible planning state rather than inventing known metrics.
- Extend candidate preparation as needed to distinguish exact rendered prompts from provisional downstream estimates. Keep full-workflow deadline/cost/resource reasoning in the joint planner; selecting only the next step greedily would change the objective.
- After each prerequisite completion, render actual ready prompts and regenerate affected candidates. Freeze dispatched/completed assignments and account for elapsed time, overhead and current reservations. Revalidate before each dispatch.
- Recompute carbon at the final candidate start/interval. Include dependency-ready times and available capacity in bounded start-grid construction; never shift a timestamp while retaining an obsolete forecast value.
- Persist decision revisions, trigger/reason, candidate exclusions, input/profile fingerprints, assumptions, health/carbon snapshots, normalization reference, search bound/truncation and planning duration. Add a bounded replan frequency/improvement rule to prevent churn.

Files: `scheduler/profile_candidates.py`, `scheduler/joint.py`, `shared/scheduling.py`, `workflow/scheduling_records.py`; proposed `workflow/planning.py`. Keep `scheduler/plan_profiles.py` as a scenario entry point.

Acceptance: offline integration tests show changed upstream output sizes, elapsed time, availability and forecasts altering only eligible unfinished work; unsupported profiles produce explicit diagnostics; fixed work and deadlines are preserved. Exact and provisional estimates are distinguishable in persisted evidence.

### P3 — Execute plans safely through the durable worker

Dependencies: P2. Existing roadmap: M5.

- Add optimized submission alongside explicit fixed submission, including speed/balanced/eco/custom policies. Validate custom weights and configuration before enqueueing.
- Version queue persistence for complete planning context and decision revisions. The current configuration allowlist and candidate-only plan history are insufficient for this evidence. Define compatibility with existing SQLite databases and queue bundles; retain their original meaning.
- Add a plan revision and an atomic compare-and-swap operation for installing assignments/reservations. Compute outside long SQLite transactions, then verify workflow version, cancellation, ownership, dependency completion and capacity before committing.
- Choose and implement a consistent pre-dispatch protocol: replan before claim, or atomically release/reassign an owned undispatched claim. Do not bypass the existing prohibition on changing running work. Recheck the chosen plan at the durable `mark_running` boundary.
- Probe model availability with bounded timeouts, enforce forecast freshness, and deduct planning/probe elapsed time from remaining deadlines. Missing or stale eligibility cannot silently become healthy.
- Restrict fallback to reviewed, compatible candidates within remaining constraints. Distinguish a confirmed pre-dispatch rejection from an ambiguous provider call; preserve explicit duplicate-usage acknowledgement for the latter.
- Expose infeasible/unsupported requests and preflight failures durably. Keep scheduled, running, cancelled and reconciliation states consistent across CLI, dashboard and replay.

Files: `workflow/worker.py`, `workflow/queue_store.py`, `workflow/deployment_inventory.py`, `workflow/dispatch.py`, `workflow/queue_replay.py`, planning service and associated tests.

Acceptance: competing workers cannot dispatch the same step or oversubscribe a changed destination; restart preserves latest committed decisions; stale plans cannot overwrite newer ones; outage/cancellation/overrun tests retain all attempts and never silently repeat ambiguous inference. A real local run demonstrates a recorded optimized decision reaching the selected endpoint, once profiles support it.

### P4 — Record comparable metrics on executed workflows

Dependencies: P3 for integrated verification; accounting development can overlap P2. Existing roadmap: M3/M6.

- Reuse calibration's cost/energy/carbon helpers in one execution accounting boundary. Persist returned provider output and usage before optional enrichment so a telemetry failure cannot lose results or repeat inference.
- Retain planned estimates separately from execution observations. Record provider duration, deliberate delay, queue/dependency wait, planner/probe overhead, urgent response time and final completion time without double counting.
- Calculate API fees from reported usage and a versioned tariff; keep zero local API fees separate from electricity/ownership costs. Preserve unknown usage for ambiguous failures and retries.
- For local telemetry, retain component scope and mixed provenance. For a remote destination, require remote observations or a clearly labeled deployment-specific estimate; coordinator power is not remote inference energy.
- Attribute carbon to actual execution grid/time with the correct CO2/CO2e scope. Handle interval boundaries explicitly through supported segmentation or unknown attribution. Retain forecast decisions and actual attribution separately.
- Aggregate all attempts, including failures and superseded late calls. Report coverage alongside partial totals. Attach reviewed end-to-end output quality separately from per-step scheduling proxies.

Files: `workflow/dispatch.py`, `workflow/deployment_clients.py`, `shared/energy.py`, `data/cost_accounting.py`, `data/carbon_sources.py`, worker/store/replay schemas and report loaders.

Acceptance: one completed durable workflow traces every reported metric to inputs and provenance; accounting failures preserve completed inference; unknowns never become zeros; zero baselines and incompatible carbon scopes are handled explicitly.

### P5 — Establish the second destination and credible carbon inputs

Dependencies: begin access discovery alongside P1; live integration requires P3/P4 and destination-specific profiles. Existing roadmap: M1/M3/M5 external gates.

- Confirm access to another inference host or region-pinned deployment, exact available models, capacity and location evidence. The example registry alone does not prove live availability. No infrastructure purchase is assumed.
- Prefer another supported Ollama endpoint for the first additional destination when available. If a cloud adapter is selected, add its bounded capability/health check: the current inventory intentionally leaves non-Ollama providers unprobed.
- Calibrate and review each supported destination/model/task configuration. Do not reuse local hardware profiles as remote measurements.
- Select a suitable timestamped feed or historical trace with documented coverage, permitted use and freshness. Verify those details when selecting it. Keep historical replay and forecasts labeled distinctly; use only information available at decision time for planning.
- Exercise actual endpoint routing, outage handling and delayed execution. Retain endpoint/provider evidence and actual timestamps. Distinct known grids are required for geographic carbon comparisons.

Files: `config/`, `shared/deployments.py`, `workflow/deployment_inventory.py`, `workflow/deployment_clients.py`, carbon adapters, calibration artifacts and deployment documentation.

Acceptance: two real destinations receive recorded work, their supported configurations have reviewed profiles, and location/time attribution has defensible evidence. If access remains unavailable, finish local work but leave this A2 release gate open. A static-factor local demo cannot be relabeled a time-shifting carbon experiment.

### P6 — Evaluate the complete scheduler against strong baselines

Dependencies: P1–P5 for full A2; a clearly scoped local pilot can precede P5. Existing roadmap: M6.

- Add one reproducible evaluation runner for the connected durable workflow. Freeze the final dataset, protocol, constraints, quality thresholds, profiles, source versions and destination set before measurements.
- Compare eligible immediate fixed model/destination choices, a calibration-selected fixed per-step assignment, model-only routing, and the joint scheduler. Include speed/balanced/eco and ablations with time or destination held fixed.
- Use paired tickets and repeated, randomized/interleaved execution where practical. Retain failures, infeasibility, deadlines, model residency and observed conditions. Synthetic synchronized scenarios and non-simultaneous live trials must have separate labels.
- Report response/final latency, quality, deadline success, API cost, energy, carbon, retries, planner overhead, sample size, variability and metric coverage. Include absolute differences when percentage denominators are zero or unknown.
- Report observed attribution separately from forecast counterfactuals. Evaluate sensitivity to deployment-specific energy and quality uncertainty. Equal or worse policy outcomes are valid results.
- Export checksummed comparison manifests linking source snapshots, protocol, individual queue bundles and review artifacts. Extend strict replay validation to the new decisions and accounting without contacting providers or waiting for original delays.

Files: proposed `workflow/evaluate_policies.py`, comparison/report helpers, `workflow/queue_replay.py`, `results/benchmarks/`, `results/demo/`, `data/METHODOLOGY.md`, evaluation tests.

Acceptance: a fresh checkout can reproduce the report from retained evidence offline; every compared policy uses a stated common workload/protocol; quality and deadline failures remain visible; matched comparisons reject incompatible evidence. Savings are reported only if supported by the results.

### P7 — Finish the dashboard and release demonstration

Dependencies: P3/P4 for interactive work, P6 for final comparison views. Existing roadmap: M7.

- Extend the existing queue view with policy/custom weights and clear infeasibility feedback. Retain fixed execution as an explicitly identified baseline mode.
- Show selected/executed identities, known/unknown geography, planned/actual times, decision revisions, rejected alternatives and constraints. Display metric provenance and coverage beside values.
- Add matched evaluation/replay views, output-quality review and response/final-deadline outcomes. Keep legacy schema-2 scenarios clearly separate from the A2 live path.
- Continue running inference in the worker, independently of Streamlit reruns. Preserve local SMTP-free development access; production identity work remains outside scope.
- Update startup, deployment, carbon, review/calibration, worker, evaluation and replay instructions. Rehearse a fresh local setup, an offline replay, a live delayed workflow and a controlled endpoint outage/replan.

Files: `workflow/dashboard/queue_view.py`, extracted comparison/decision views, `workflow/dashboard/app.py`, `README.md`, `DEMO_GUIDE.md`, `DEMO_SCRIPT_LOCAL.md`, `docs/`, UI tests.

Acceptance: a reviewer can trace ticket → decision → actual execution → quality/resource evidence from the UI and documented commands. Full offline suite and observed Windows/Linux CI pass; the final demo states any remaining infrastructure or measurement limits.

## Sequence and checkpoints

Critical path: **P0 → P1 → P2 → P3 → P4 → P6 → P7**. Start P5 access and data-source discovery early; full A2 evaluation waits for it. Simulated fixtures let implementation proceed while human review or destination access is pending, but they do not satisfy live gates.

| Checkpoint | Deliverable | Decision |
|---|---|---|
| C1: credible inputs | Stable baseline, review tooling, frozen reviewed local profiles and coverage report | Are any configurations eligible without weakening quality constraints? |
| C2: integrated local scheduler | Actual-output-aware planning, atomic worker dispatch and per-attempt accounting | Can a supported local ticket execute with a retained decision/replan history? |
| C3: complete experiment | Second destination, suitable carbon data, frozen final protocol and policy comparisons | Are all three decision dimensions and five metrics evidenced? |
| C4: release | Validated bundles, dashboard, documentation, clean setup and live rehearsal | Can another reviewer reproduce and inspect the result? |

Planning estimate: roughly **4–6 focused developer-weeks** for the remaining integration, tooling, validation and release work, assuming timely review and destination access. This is an estimate from the inspected scope, not a measured schedule. Human review, additional calibration, second-host access and carbon-data acquisition can extend elapsed time. Re-estimate after C1; do not promise a complete A2 release by a calendar date before those gates are understood.

The first implementation change should address P0 and add P1 review records/tooling. The next should establish the exact-versus-provisional planning contract before modifying queue dispatch. This reduces the risk of integrating a planner that has no defensible live inputs.

## Definition of complete

- [ ] A connected workflow runs from actual ticket input through response and background completion.
- [ ] Reviewed profiles and explicit constraints drive model, destination and time decisions.
- [ ] At least two models and two real destinations are demonstrated; geographic claims use known distinct grids.
- [ ] Delayed dispatch, replanning, cancellation, deadlines, retries and restart/reconciliation preserve execution evidence.
- [ ] All five metrics appear in matched end-to-end comparisons with honest provenance, uncertainty and coverage.
- [ ] Strong fixed baselines, final untouched evaluation examples, failures and deadline outcomes remain in the report.
- [ ] Checksummed replay reproduces the analysis without inference or network calls.
- [ ] Dashboard, documentation, fresh setup, automated checks and live rehearsal support the same integrated path.

Completion does not require positive savings. It requires correct execution and evidence strong enough to explain the result.

## Implementation progress — 2026-10-26

| Slice | Implemented and checked | Remaining |
|---|---|---|
| P0 | Bounded dashboard AppTest startup budget, current dataframe API, dependency check, source manifest and a passing complete offline suite | Hosted Windows/Linux CI and fresh release setup remain unverified |
| P1 | Versioned rubric; immutable human-review templates/contracts; exact trial/output/rubric bindings; export/validation CLI; profile import/evidence; explicit trial/case/fraction coverage policy | Actual human judgments, broader examples/context coverage, prompt corrections if needed, untouched final evaluation split |
| P2 | Typed workload-size forecasts; separate actual/provisional candidate preparation; connected planning service with committed outputs, capacity/health/deadline checks, bounded start closure, incumbent threshold and detailed reports | Automatic forecast fitting/validation study and reviewed live profiles |
| P3 | Optimized submission; full retained context/decision revisions; atomic install/claim tokens; shared occupancy; pre-dispatch validation/rerouting; replan rate limiting; ambiguous-call recovery; strict planned bundle-2 replay | Reviewed live optimized execution and a second real destination remain external gates |
| P4 | Immutable accounting context; raw output before optional enrichment; API usage fees; optional loopback host telemetry and full-interval carbon; failure/late-attempt retention; timing/coverage report; strict bundle-3 replay and offline accounting reconciliation | Fresh live accounting validation and attributable end-to-end human quality review |
| P5 | Explicit destination contracts and conservative carbon inputs remain available | Second real destination and suitable timestamped carbon evidence |
| P6 | Frozen protocol/source; paired randomized schedule; fixed/per-step/training-selected baselines; constrained joint ablations; isolated durable trial queues; strict portable comparisons; frozen output-review policy, blank packets and attributable quality joins | Actual human judgments, untouched final inputs, controlled conditions, sensitivity studies and reviewed live measurement |
| P7 | Optimized/fixed UI, planning history and rejection evidence, accounting coverage, offline comparisons, blank human-review forms, updated demo guides, completed scripted demo and clean Windows install/rehearsal | Reviewed live/geographic demonstration, hosted CI and manual browser/assistive-technology rehearsal |

P2 verification: 1,492 offline tests passed in 59.17 seconds. P3 verification: **1,604 full-suite tests passed in 162.71 seconds**, followed by one added policy-selected carbon-window test passing in 6.70 seconds. Together these cover all 1,605 collected tests. `pip check`: no broken requirements. Controlled fixtures demonstrate actual output size changing model choice, eligible destination changes after outages, and actual queued execution at a policy-selected later time. No new live model generation was performed, and no hosted CI result is claimed.

Prepared human review packet: `results/reviews/m3-local-pending-20261026/`, with 99 retained trials, 80 blank review templates and 19 excluded warm-up/failed attempts. See [the review guide](../quality-review.md). The new gated aggregate at `results/benchmarks/m3-local-gated-20261026/` freezes the pilot policy and leaves all eight profiles quality-ineligible while unreviewed. The policy requires ten reviews, five distinct cases and full coverage of valid natural completions per profile; this is a pilot gate, not population-quality evidence.

The real-input planning probe correctly returned infeasible with classification materialized, three downstream contexts unavailable and no dispatchable work. It did not invent forecasts, observed availability or carbon. See [staged planning](../staged-planning.md) for the API and boundaries.

P3 subsequently connected the preparation service to the durable worker. Offline workflow tests verify actual output chaining, delayed restart, outage rerouting, racing workers, cancellation, known preflight retry, and no automatic replay of an ambiguous provider call. The real-profile CLI submission `906ca02a0e634714a04ddee7e2a034bc` retained an infeasible decision with zero jobs and zero attempts after read-only health probes. Export/inspection passed for `results/demo/a2-planned-infeasible-20261026/`. No new model generation or measured savings are claimed. See [the planned worker guide](../planned-worker.md).

P4 now enriches durable executed attempts without changing raw provider facts. Tests cover tracker startup/shutdown/report failures, enrichment/disk failures, delayed four-step execution, cancellation, superseded calls and offline reconciliation. Reports retain unknowns, prevent pooled CO2/CO2e totals, and flag unreturned work, uncertain provider completion and overlapping host windows. Human quality, reviewed live optimized execution, the second destination and final policy comparisons remain open. See [execution accounting](../execution-accounting.md).

P4 verification: **1,719 full-suite tests passed in 94.67 seconds**, then all 25 accounting replay tests passed in 8.13 seconds after adding offline bundle reporting. These cover all 1,720 currently collected tests. Dependency and whitespace checks passed. Both original curated demonstrations were loaded and reported offline; compatibility reports are in `results/scratch/p4-accounting-compatibility-20261026/`. Their absent accounting remains unknown. No new inference or measured savings are claimed.

Source identity: `.runtime/implementation-source-20261026.json` preserves P2; `.runtime/p3-implementation-source-20261026.json` preserves P3; `.runtime/p4-implementation-source-20261026.json` records the accounting implementation. These exclude credentials and machine-local deployment configuration. Existing changes and historical recordings were preserved.

## Evaluation tooling progress — 2026-11-01

P6 now has a common protocol and execution/comparison path. The runner passes the frozen KB, isolates every trial in its own queue, adopts a committed submission after an interrupted journal update, and stops uncertain provider outcomes before another serial trial. Simulation is retained throughout protocol/configuration/planning evidence and cannot be relabeled live through injected fixtures. Calibrated fixed-arm claims are recomputed from frozen training medians. Reports retain all outcomes, unknowns, positive/zero/negative deltas, zero baseline guards and incompatible carbon scopes. Portable export is immutable and recomputed entirely offline.

Verification: **1,891 full-suite tests passed in 203.43 seconds**, followed by one added portable-package immutability test passing in 0.40 seconds. Together these cover all 1,892 currently collected tests. Dependency checks passed. No new model generation, endpoint probes or hosted CI runs were performed.

Retained current-source pilot: `.runtime/p6-local-pilot-v2-20261101/`; portable prepared comparison: `.runtime/p6-prepared-comparison-v2-20261101/`. Eight arms × six reused held-out tickets × one repeat gives 48 pending trials. Freeze/export/inspection succeeded with zero completed trials, no execution queues, unknown quality/resources and release eligibility false. Earlier preparation snapshots were preserved. This is protocol/tooling evidence, not final experimental results. See [policy evaluation](../policy-evaluation.md).

The source checkpoint for this slice is `.runtime/p6-implementation-source-20261101.json`. Human semantic review, reviewed live optimized execution, the second destination and final independent measurements remain open gates; no savings claim is made.

### Workflow quality integration — 2026-11-01

New protocols freeze output-review rubric and thresholds before execution. Blank review packets bind actual accepted natural completions; approved judgments bind the full workflow, outputs, protocol and rubric. Imports are additive, reject conflicting replacements, preserve prior revisions, and leave raw execution/planning facts unchanged. Comparisons retain reviewed/unknown/failed coverage over all scheduled trials and paired quality differences. Portable exports retain and revalidate approvals offline; older packages keep their original unreviewed meaning.

Verification: **1,912 tests passed in 225.45 seconds**, including synthetic judgment fixtures, malformed/hash/timing/arithmetic checks, partial/failing coverage, revision history and transport-free review export. Dependencies and whitespace checks passed. No real human scores or model outputs were generated.

Current-source preparation: `.runtime/p6-review-pilot-v2-20261101/`; blank packet: `.runtime/p6-workflow-review-packet-v2-20261101/`; portable preparation: `.runtime/p6-reviewed-comparison-prepared-20261101/`. All 48 trials remain pending, with zero reviewable outputs, zero approved judgments and release eligibility false. Source checkpoint: `.runtime/p6-quality-source-20261101.json`. See [workflow quality review](../workflow-quality-review.md). Next implementation work is the optimized dashboard; actual human review and live evidence remain external gates.

### Local dashboard and release rehearsal — 2026-11-01

P7 local implementation is integrated: fixed/optimized submission without UI provider calls; four-step progress even for infeasible zero-job plans; decision/score/rejection history; resource scope and coverage; validated offline comparisons; actual output inspection; blank human review forms with additive backend approval checks. Local login retains SMTP-free operation without logging its code. Demo guides now describe the implemented path and honest limits; exact older guides were archived.

Fresh installation initially failed because the Windows constraints pinned Streamlit 1.66.0/Pillow 12.3.0 while runtime requirements required 1.66.1/13.0.0. Aligning the pins resolved installation. The fresh `.venv-release-20261101` passed `pip check` and **1,917 full-suite tests in 233.58 seconds**, plus the added offline-demo test (10.62 seconds on its final guarded run), covering 1,918 collected tests. Dashboard HTTP startup returned 200/ok; AppTest covered policy submission, invalid settings, comparisons and actual review persistence.

The completed scripted package `results/benchmarks/a2-offline-dashboard-20261101` contains four simulation trials and no human judgments. Its original local evaluation is `.runtime/p7-offline-evaluation-20261101`. It uses no provider, no model download and no measured savings claim. Earlier live/rejection bundles and all prior checkpoints remain intact.

The user confirmed no second destination and no assigned reviewer. The actual configured local endpoint was also unavailable on read-only probe. Consequently the local software work can be inspected now, but the full A2 experimental release cannot be declared complete. [Release readiness](../release-readiness.md) records remaining human/infrastructure/data/measurement checks. Final source checkpoint: `.runtime/p7-implementation-source-20261101.json`.
