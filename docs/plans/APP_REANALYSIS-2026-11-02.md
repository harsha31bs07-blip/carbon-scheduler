# App reanalysis and follow-up implementation plan

Reviewed 2026-11-02 at commit `a6fafcffc3cc00b5b4aae04ef8b35e6e484def42`
on `feat/a2-foundations`. The working tree was clean at the start.

The earlier improvement milestones are implemented, but a second review found
three reproducible correctness defects and one query-scaling gap. The application
remains a capable local prototype. Its next iteration should repair these
boundaries and expand their regression coverage. A frontend rewrite or new
infrastructure is not justified by these findings.

This pass changes analysis documentation only. Reproductions use temporary,
fictional queues; they make no provider calls or human judgments. Existing live
queues, calibration evidence and portable results remain unchanged.

## What is working

| Area | Assessment from the current implementation |
|---|---|
| Architecture | Streamlit submits and inspects work; the separate worker owns inference. SQLite remains suitable for the stated local operator scope. |
| Submission | Token/content binding is transactional, supports concurrent repeats and preserves intentional new submissions. |
| Execution | Lease ownership, deadlines, cancellation, uncertain-call recovery and raw-attempt retention have substantial regression coverage. |
| Planning | The joint planner checks dependencies, capacity, deadlines and metric eligibility. Bounded search explicitly reports truncation and limits its optimality claims. |
| Evidence | Calibration quality, final workflow reviews, simulation, unknown measurements and CO2/CO2e are explicitly distinguished. |
| Operator experience | Setup diagnostics, searchable history, decision explanations, review drafts and recovery commands are implemented. The newly found issues below narrow their reliability claims. |
| Verification | Windows/Linux unit CI and a separate Chromium journey passed for the reviewed commit. These results do not imply that every behavior is covered. |

The existing Python/Streamlit/SQLite design should be retained. Keep queue schema
validation and transitions centralized; do not split the large store module merely
to reduce line count. Extract shared validation where the restore discrepancy
demonstrates a concrete need.

## Confirmed findings

**1. P1: restore can report a valid queue that the application cannot open.**

`workflow/operations.py:80` validates the main metadata, SQLite integrity, foreign
keys and workflow snapshots. It bypasses `QueueStore.__init__` and does not apply
its submission-table schema validation (`workflow/queue_store.py:295`).

Reproduction: create a queue with one fixed workflow, add an unsupported column to
`queue_submissions`, then back it up and restore it. Restore returns
`validated: true`; opening that restored database normally raises
`Unsupported submission table schema`. This uses a deliberately incompatible
fixture, not evidence that normal submissions corrupt the database.

Impact: recovery gives false assurance for an incompatible database. Use one
read-only compatibility validator for normal opening, backup and restore, covering
all supported extensions while preserving supported legacy omissions. Validate
before creating the restore destination. Do not solve this by opening the source
through a constructor that migrates it.

**2. P2: backup and restore disagree at the member-count boundary.**

`workflow/operations.py:123` checks `len(files) > 4096` before adding a member;
restore at line 159 checks `len(files) >= 4096` before adding one.

Reproduction: an empty queue plus 4,096 tiny orphan JSON sidecars produces a
successful backup with 4,097 manifest members. Its immediate restore fails with
`Backup exceeds the local operator bound`.

Impact: an artifact accepted by backup cannot be restored. Define shared limits
and prospective-count checks, including the database member. Apply the byte limit
to the actual SQLite backup, including committed WAL data, rather than relying
only on the live main file's size. The file-count failure was reproduced; the
large-WAL byte-limit case is an additional code-review concern, not a measured run.

**3. P2: automatic progress refresh can stay disabled after selection changes.**

`workflow/dashboard/queue_view.py:120` chooses the fragment's timer from the
previous `queue_last_status`. The new selection's status is read inside the
fragment at line 169. A full rerun is requested only for the active-to-terminal
direction, leaving the reverse transition unhandled.

Reproduction with AppTest and an observer around the real `st.fragment`: select
a cancelled workflow, enable refresh, then select a scheduled workflow. The
checkbox remains true and selected status becomes `scheduled`, but the registered
timer is `None`. No app exception is raised. This is a framework-level timer
registration reproduction; a fresh real-browser timing reproduction was not run.

Impact: progress can appear frozen until another full rerun or manual refresh.
Reconfigure the timer when the selected workflow crosses either direction of the
active/terminal boundary. Preserve selection and form state, and prevent rerun
loops. Add a real-browser test because AppTest interactions alone do not reproduce
all fragment-only browser reruns.

**4. P2: a status-filtered history page performs work proportional to queue size.**

`workflow/queue_store.py:335` retrieves candidate workflows, parses each request,
queries its jobs and latest planning decision, computes status in Python, and
only then applies the status filter. The result limit bounds returned rows, not
scanned workflows or database calls.

Reproduction: 100 scheduled workflows, filter `completed`, limit 20. The result is
empty after **203 SQL statements**, taking **0.112 seconds** in this local sample.
This confirms query amplification, not an unacceptable latency at today's size
or a general throughput benchmark. Rare/no-match filters are the relevant case;
the earlier first-page profiling did not cover them.

Move status derivation/filtering into a set-based read query, or introduce a
transactionally maintained summary projection only if measurement justifies its
additional state. Preserve exact existing status semantics, legacy compatibility,
keyset pagination and full validation when opening a selected workflow.

## Follow-up implementation sequence

| Order | Work | Completion criteria |
|---|---|---|
| 1 | Unify read-only queue compatibility validation. | Reproduced incompatible extension rejected before restore output exists; valid fixed/planned queues and supported legacy schemas still round-trip; no source migration. |
| 2 | Align backup/restore file and byte bounds. | Maximum accepted package restores; one extra member is rejected at backup; committed WAL size is accounted for; malformed input fails without overwriting data. |
| 3 | Correct refresh lifecycle. | Terminal-to-active selection starts updates, active-to-terminal stops them, cancellation behaves consistently, and selection/form values survive; browser test observes a status update without manual refresh. |
| 4 | Remove history-query amplification. | Common, rare and absent status matches tested on increasing queue sizes; query count no longer grows by two per scanned workflow; status and pagination results match validated snapshots. |
| 5 | Strengthen relevant checks and current status records. | Add regression tests for findings 1-4; broaden existing lightweight lint coverage; type the changed operations/query boundaries incrementally; link final checks to their exact commit. |

Keep these as small reviewable changes. Repeat the full suite and browser journey
on the resulting source, then verify Windows/Linux/browser CI. Do not use the
current passing suite as proof that the newly reproduced cases are covered.

Documentation also needs a small reconciliation: the current release-readiness
gate table still says hosted checks were not observed, while later development
records and the verified CI run establish success. Preserve historical dated
records, but make the current gate table distinguish completed automated checks
from outstanding manual screen-reader work.

## Verification for this review

- Rechecked the published commit's [Windows/Linux/Chromium CI run](https://github.com/harsha31bs07-blip/carbon-scheduler/actions/runs/41372989839): all three jobs succeeded.
- Fresh local full suite: **1,970 passed, 1 skipped in 240.23 seconds**, using `.venv-release-20261101` with dotenv loading and pytest caching disabled. The skipped test is the optional browser journey; it was not rerun locally in this pass.
- Ruff `E9,F63,F7,F82` passed across `workflow`, `scheduler`, `shared` and `data`.
- `pip check` passed. Existing incremental mypy checks passed for the two dashboard service/progress modules; this is not project-wide type coverage.
- Isolated reproduction scripts and JSON results are retained locally under `.runtime/reanalysis-20261102/`. Temporary fixture databases were removed by their scoped temporary-directory contexts.
- No fresh browser, manual accessibility, provider availability, dependency vulnerability or live experiment audit was performed in this pass. Browser CI was verified as existing evidence for the exact reviewed commit.

## External release requirements remain separate

The user has no second inference destination and has not assigned a human
reviewer. Genuine calibration/workflow judgments, broader actual-context
calibration and forecasts, confirmed destination/grid evidence, suitable
timestamped carbon inputs and independent controlled live comparisons remain
necessary for full A2 validation. Current local backend availability was not
probed. Manual screen-reader review remains outstanding.

The software fixes above can proceed independently. They cannot establish real
quality, measured savings or multi-destination experimental readiness. Shared
hosting also remains a separate product scope: the present process-local OTP and
asserted reviewer attribution are not a tenant-isolated identity system.

## Implementation checkpoints

Milestone 1: opening, backup and restore now share read-only extension compatibility validation. Added incompatible-extension recovery regressions; focused verification recorded in the commit.

Milestone 2: shared prospective file/byte limits now cover both backup and restore, including committed WAL pages. All 12 operations tests passed, including exact-boundary round trips and one-over rejection.

Milestone 3: refresh is reconfigured across both active/terminal transitions and cancellation updates saved status. The dashboard checks passed; the expanded headless Edge journey passed in 18.56 seconds, observing an externally cancelled workflow update without manual refresh. It revisits the workspace after submission before testing history navigation.
