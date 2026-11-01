# App analysis and implementation plan

Reviewed: 2026-11-01. Scope: improve the existing local Carbon Scheduler app.
Milestone 1 is implemented; milestones 2-7 remain proposed work. This complements
the [A2 completion plan](COMPLETION_PLAN-2026-10-26.md)
and [release readiness record](../release-readiness.md).

## Recommendation

Keep the existing Python, Streamlit and SQLite architecture. The strongest next
investment is making the implemented scheduler understandable and operable:
guided setup, clear queue states, useful failure explanations, accessible evidence
views and a manageable human-review workflow. Preserve the existing durable worker,
strict replay validation and honest treatment of missing measurements.

Prioritize correctness and operator experience before adding providers, replacing
the frontend or expanding to arbitrary workflows. Assume a local, single-operator
application for this iteration. Shared hosting requires a separate identity,
authorization and storage design; email OTP alone does not provide tenant isolation.

## Implementation checkpoint — 2026-11-01

- Published the complete existing app on GitHub branch `feat/a2-foundations`,
  commit `b9dfe91`, after **1,918 tests passed in 241.21 seconds**. Private local
  configuration was excluded; checksummed results retain exact bytes in Git.
- Completed milestone 1: request history survives code expiry, backend cooldown
  applies across UI sessions, a process lock serializes authentication operations,
  expiry is enforced at the exact boundary, and hash comparison uses constant time.
- Both workspaces share logout cleanup; login removes the displayed development
  code. Logout clears current-session evidence and draft fields without modifying
  other sessions or shared request-limit history.
- Added 12 regression cases, including concurrent create/verify and both actual
  workspace logout flows. **89 focused tests passed in 9.32 seconds** across
  `test_otp_lifecycle`, `test_development_login`, `test_queue_dashboard` and
  `workflow/tests/test_data_loading.py`. The new total is 1,930 tests; a full local
  suite including the added tests has not been rerun at this checkpoint.
- The OTP store and lock are process-local and reset on server restart; shared
  hosting still needs its own durable identity/rate-limit design. Next: milestone 2.

## Current architecture and strengths

| Layer | Implementation | Assessment |
|---|---|---|
| UI | `workflow/dashboard/app.py`, `queue_view.py`, `evidence_view.py` | Fixed/optimized submissions, inspection, comparisons and workflow reviews already exist. |
| Planning | `workflow/planning.py`, `scheduler/joint.py`, `scheduler/profile_candidates.py` | Joint model/destination/time selection with constraints, bounded search and retained decision evidence. |
| Execution | `workflow/worker.py`, `planned_worker.py`, `queue_store.py` | Separate worker, durable SQLite transactions, leases, capacity, cancellation and explicit recovery. |
| Measurement | `workflow/execution_accounting.py`, `accounting_report.py` | Raw attempts precede optional accounting; incomplete coverage and unknown values remain visible. |
| Experiments | `workflow/evaluate_policies.py`, `evaluation_protocol.py`, `policy_comparison.py` | Frozen paired trials, baselines, ablations and portable validated comparisons. |
| Quality | `workflow/review_quality.py`, `review_workflow_quality.py`, `workflow_quality.py` | Human judgments bound to retained evidence; calibration and final workflow quality are separate. |
| Verification | `tests/`, `data/tests/`, `workflow/tests/`, CI matrix | Extensive offline tests and dashboard AppTest coverage; historical fresh-environment verification is documented. |

The durable path is UI submission -> saved request/evidence -> worker eligibility
check -> provider call -> raw journal -> accounting -> inspection/export. Keep
provider execution outside Streamlit reruns. The legacy independent-prompt
comparison workspace has different semantics and must remain clearly identified.

## Findings grounded in the current source

| Priority | Finding and evidence | User impact |
|---|---|---|
| P0 | `workflow/auth/otp.py::_cleanup_expired_entries` removes rate-limit history with the expired code. A clock-controlled reproduction issued codes at 0, 61 and 122 seconds; another request was allowed at 423 seconds, inside the 600-second window. | The advertised three-request limit is not enforced across code expiry. |
| P1 | `queue_view.py` requires file paths, JSON weights and a separate terminal worker command; readiness is not summarized before submission. | New users can queue work without understanding why nothing runs. |
| P1 | Progress refresh is manual. Lease heartbeats exist, but the UI has no idle-worker liveness/last-seen display. | A healthy idle worker, absent worker and delayed job are difficult to distinguish. |
| P1 | Workflow selection is a raw SQL query limited to the newest 100 IDs. | Older work is inaccessible through this selector; IDs provide little context. |
| P1 | Submission uses a new workflow ID per call and the UI has no durable submission token. | Repeated clicks or retries can create separate tickets; the boundary needs deliberate duplicate handling. |
| P1 | Rejection details and paired comparisons are largely JSON. Generic save errors combine input and storage failures. | Users need developer knowledge to resolve problems or interpret results. |
| P1 | Review forms are embedded inside trial inspection; calibration review remains a separate CLI workflow. | Existing review tooling is difficult to discover and process systematically. |
| P2 | `app.py` is 1,014 lines and imports legacy execution/chart modules before choosing the workspace. `queue_view.py` mixes forms, SQL and report presentation. | Changes are harder to isolate; startup/rerun costs should be measured before optimization. |
| P2 | Logout resets authentication fields but leaves other application session values; comparison reports are cached until explicitly reloaded. | Session cleanup and snapshot freshness should be explicit, especially on shared workstations. |
| P2 | CI runs tests and dependency checks, while `conftest.py` disables selected availability checks rather than globally denying outbound traffic. | Offline behavior relies on individual mocks; accidental network paths deserve a central guard. |

These are code findings and improvement opportunities, not a production penetration
test or measured performance study. No throughput bottleneck is asserted solely
from file size. Current inference availability was not re-probed during this review;
the earlier unavailable-endpoint result is historical.

## Implementation milestones

Effort ranges are planning estimates for one developer familiar with the code,
including focused verification. They exclude infrastructure procurement, human
scoring, live calibration and external review. Re-estimate after milestones 1-2.

### 1. Fix authentication lifecycle and establish the baseline — P0, 1-2 days

**Changes**

- Separate OTP expiry from request-history retention. Keep request timestamps
  until the rate window ends, even after successful verification or code expiry.
- Enforce resend cooldown in the backend, not only the UI; make create/verify
  mutations atomic for concurrent local sessions. Use constant-time hash comparison.
- Centralize logout cleanup for both workspaces, including development codes,
  cached evidence and review widgets. Do not clear another active session's state.
- Record a reproducible source/dependency/test baseline without discarding the
  substantial existing modified/untracked work.

**Files:** `workflow/auth/otp.py`, `workflow/dashboard/app.py`, proposed
`workflow/dashboard/session.py`, focused auth tests.

**Acceptance:** the reproduced fourth request is rejected until the oldest request
leaves the window; expiry, cooldown, single use, concurrent creation and failed
attempt limits are tested. Logout clears sensitive UI state. OTPs remain absent
from logs, and development mode remains explicitly labeled.

### 2. Extract UI boundaries and add readiness/setup — P1, 3-4 days

**Changes**

- Extract login, common styling and legacy views from `app.py`; keep a small
  navigation shell and lazy-load the selected workspace.
- Introduce a dashboard application-service boundary for validated submission,
  workflow summaries and evidence selection. Move direct SQL out of views.
- Add an Overview page showing deployment configuration, last observed health,
  exact model mappings, reviewed profile coverage, forecast/carbon coverage and
  worker last-seen status. Show unavailable, unknown and stale as distinct states.
- Offer a named offline-demo entry and validated saved evidence/configuration
  choices; retain paths and raw JSON under advanced controls.
- Add an offline diagnostic command for config/schema/path/dependency checks.
  An explicit probe option may run bounded read-only inventory; ordinary page
  rendering and submission must remain free of provider network calls.
- Generate startup commands using the active Python executable, actual database
  and appropriate shell quoting. Explain that the worker is a separate process.

**Files:** `workflow/dashboard/{app,queue_actions,queue_view}.py`,
`workflow/deployment_inventory.py`, proposed dashboard services/readiness modules
and diagnostic CLI.

**Acceptance:** a first-time user can open the bundled simulation without typing
paths, see why live optimized execution is blocked, and obtain correct startup
commands. Fresh/unobserved health is never presented as a successful probe.
AppTest verifies navigation with all provider calls forbidden.

### 3. Make workflow submission and tracking dependable — P1, 3-5 days

**Changes**

- Validate ticket/deadline/delay/weight fields individually and show corrective
  messages next to them; distinguish storage errors with a diagnostic reference.
- Add a submission token with an atomic store-level token-to-workflow mapping.
  Bind it to normalized user inputs/settings before generated IDs/timestamps;
  same-token retries return the saved workflow, changed payloads conflict.
  Intentional repeat submissions receive a new token.
- Add lightweight paginated workflow summaries, status/time filters and lookup by
  ID. Include a bounded ticket preview, policy, progress and latest issue.
- Add a step timeline and status explanations for queued, waiting, infeasible,
  running, uncertain, failed, cancelled and completed states. Explain intentional
  delay and the next eligible time separately from worker absence.
- Add optional bounded refresh while work is active; preserve selection and draft
  form inputs. Pause refresh during review entry and avoid unnecessary full reruns.
- Provide recovery guidance using existing retry/reconciliation commands. Keep
  ambiguous-call acknowledgement explicit; never automatically repeat inference.

**Files:** `workflow/dashboard/queue_view.py`, proposed workflow views/services,
`workflow/queue_store.py`, existing worker command interfaces.

**Acceptance:** two concurrent requests with the same token create one workflow;
changed payloads are rejected and new tokens permit deliberate duplicates. Users
can locate work beyond the newest 100 records. Refresh never dispatches work or
duplicates submission. Cancellation wording correctly describes in-flight work.

### 4. Explain scheduling and comparison results — P1, 2-3 days

**Changes**

- Map retained rejection codes to plain-language reasons and specific next actions,
  for example missing reviewed coverage, unsupported context size, stale carbon
  input or unavailable destination. Preserve the original codes in details.
- Show selected model/destination/start alongside alternatives, constraints and
  score contributions. Display bounded-search exhaustion separately from a
  proven lack of feasible combinations; show examined/total combinations.
- Present paired outcome tables and compact plots for latency, cost and resource
  coverage. Include sample counts, deadline failures and quality coverage adjacent
  to any reported difference. Keep CO2 and CO2e separate.
- Identify loaded evidence as a validated snapshot with validation time and source
  fingerprint. Refresh mutable evaluations after review; cache immutable bundles
  only with content identity and bounded memory. Revalidate changed inputs.

**Files:** `workflow/dashboard/evidence_view.py`, planning presentation modules,
existing reports from `planning.py`, `accounting_report.py`, `policy_comparison.py`.

**Acceptance:** users can identify why a plan failed without reading JSON. Unknown
or zero baselines never produce misleading percentage savings. Presentation
does not recalculate scheduler decisions or bypass strict evidence validation.

### 5. Build a review inbox — P1, 3-4 days

**Changes**

- Add an inbox with separate calibration and workflow review views, pending counts,
  eligibility reasons and progress toward the applicable frozen coverage policy.
- Show the ticket, KB evidence, output and rubric together; support moving through
  pending items without manually finding trial IDs.
- Save incomplete drafts separately from approved evidence, keyed to reviewer,
  trial and evidence fingerprint. Changed evidence invalidates the draft binding.
- Show completion errors before submission and refresh coverage after a successful
  approval. Use the existing strict imports; never prefill a score or rationale.
- Keep actual reviewer assignment external. Do not infer identity from a typed
  reviewer name or treat simulation reviews as live quality validation.

**Files:** `workflow/dashboard/evidence_view.py`, proposed review inbox/draft store,
existing `workflow/review_quality.py` and `review_workflow_quality.py` adapters.

**Acceptance:** blank/partial drafts cannot become approvals; existing approvals
cannot be overwritten. Calibration and workflow judgments retain their distinct
bindings and eligibility rules. A saved draft survives navigation without altering
raw attempts or validated exports.

### 6. Add operations visibility and recovery rehearsal — P2, 3-4 days

**Changes**

- Add worker registration/last-seen records distinct from job lease ownership,
  including idle workers. Expired presence means stale, not confirmed stopped.
  Observability records must never authorize dispatch or free an uncertain lease.
- Expose queue depth, oldest waiting work, deadline misses, retries, planning
  duration and measurement gaps through sanitized structured diagnostics.
- Add a consistent SQLite backup command and documented restore rehearsal,
  accounting for WAL state. Validate restored queues and retained sidecars in an
  isolated directory before enabling execution.
- Profile list queries, snapshot validation and bounded planner search on defined
  fixtures. Add indexes or cached read summaries only where measurements justify
  them; retain semantic validation at dispatch/export boundaries.
- Add a dry-run retention inventory for queue files, raw evidence and exports.
  Deletion is a separate explicit operation and must preserve evidence dependencies.

**Files:** `workflow/{worker,planned_worker,queue_store}.py`, proposed diagnostics
and maintenance commands, `docs/durable-worker.md`.

**Acceptance:** stopped/idle/busy worker scenarios display correctly, restored
queues pass integrity/replay checks, and interrupted/uncertain calls are never
automatically retried. Logs omit prompts, OTPs, credentials and endpoint secrets.

### 7. Complete app-level verification and documentation — P2, 2-3 days

**Changes**

- Add an outbound-network denial fixture by default with narrow explicit loopback
  exceptions for tests that start a local server. Provider SDK/mock tests remain
  offline; isolate live integration commands from the default suite.
- Add browser smoke coverage for startup, navigation, submission, cancellation,
  infeasible explanations, evidence loading and blank-review rejection.
- Check keyboard navigation, labels, focus, textual status announcements and
  responsive layouts in the actual browser; perform a manual screen-reader pass.
- Add incremental lint/type checks on new boundaries without reformatting the
  entire existing project. Run Windows/Linux CI on the final reviewed source.
- Update demo/development guides and establish one current status page linking to
  historical milestone records so stale checklist wording does not confuse users.

**Acceptance:** focused tests and the full suite pass, both CI platforms are green,
fresh installation and backup restore are rehearsed, and critical browser journeys
work with the keyboard. Record exact checks and remaining limitations rather than
claiming accessibility certification from AppTest alone.

## Sequence, estimates and first delivery

Recommended order: **1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7**. Design worker presence
during milestone 2 and implement its durable records alongside milestone 3 or 6;
until then the overview must display worker presence as unknown. Add tests and
documentation with each milestone, not only in milestone 7.

Total proposed local improvement effort: **17-25 developer days**, approximately
four to six working weeks allowing modest integration time. This is not an A2
experimental-release deadline. A useful first delivery is milestones 1-3:
**7-11 developer days**, with OTP correctness, discoverable setup, trustworthy
submission and usable workflow tracking.

Track each milestone as a small series of reviewable changes. Do not rewrite
schemas, history or replay bundles merely to reorganize the UI. Additive queue
extensions need explicit version checks and compatibility tests for old databases.

## Separate experimental-release track

The following do not block local app improvements, but they do block the broader
A2 claims recorded in the existing release plan:

1. Restore the configured local inference service and verify exact installed models.
2. Assign a real reviewer and obtain valid calibration judgments; extend actual
   context coverage and forecasts as necessary. Unknown quality remains ineligible.
3. Obtain a second real destination with exact endpoint/model mappings and confirmed
   physical location/grid. The user currently has none available.
4. Provide defensible timestamped carbon inputs and appropriate energy observations;
   annual historical CO2 proxies cannot demonstrate hourly shifting benefits.
5. Freeze independent final tickets/protocol and perform controlled paired live
   experiments, retaining failures, uncertainty and actual human workflow reviews.

Owners remain unassigned where the user has not identified a person or host.
No local software change supplies these missing observations. Keep local-demo
readiness, single-destination live operation and full A2 validation visibly separate.

## Deliberately deferred

Defer a frontend rewrite, Kubernetes, multi-tenant service, billing, arbitrary DAG
authoring, learned scheduling and additional provider breadth. Reconsider only
when measured limitations or a concrete deployment requirement justify them.
If shared hosting becomes a goal, first design per-user authorization, authenticated
reviewer identity, server-side sessions, bounded file access and secret handling.

## Verification performed for this analysis

- Reviewed current source, dashboard flows, queue/worker interfaces, authentication,
  tests, dependencies, CI configuration and release documentation on branch
  `feat/a2-foundations`, HEAD `e579016`, including existing working-tree changes.
- Reproduced the OTP rate-window defect in an isolated Python process with a fake
  clock and no email/network calls. No real OTP or email address was exposed.
- Historical release verification reports 1,917 full-suite tests plus the added
  demo test. That historical result is not a newly rerun full-suite result.
- Current focused verification: **10 tests passed in 30.43 seconds** using
  `.venv-release-20261101`, with dotenv loading and pytest caching disabled:
  `tests/test_queue_dashboard.py`, `tests/test_optimized_dashboard.py`,
  `tests/test_development_login.py`, `tests/test_demo_evidence.py`.
- No production source changes, live inference, human judgments, publication or
  deployment were performed as part of this planning task.
