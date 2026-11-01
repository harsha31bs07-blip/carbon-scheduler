# Local implementation and A2 release readiness

Recorded 2026-11-01. Local implementation and experimental release validation
are separate. The integrated local software is implemented and exercised. The
full A2 experimental release remains blocked by missing real evidence.

## Implemented local path

- Actual ticket and KB retrieval flow through classify/retrieve/draft/summarize.
- Joint model/destination/start planning retains forecasts, eligibility, rejected
  choices and score evidence; dispatched work stays frozen.
- Durable SQLite execution covers delays, restarts, capacity, leases, deadlines,
  cancellation, explicit retries and ambiguous-call reconciliation.
- Raw output/usage commits before optional accounting; unknown resources,
  uncertain calls, incompatible carbon scopes and failed attempts remain visible.
- Frozen paired policy experiments include strong fixed/per-step baselines and
  dimension ablations; strict portable comparisons reconstruct evidence offline.
- Workflow reviews freeze rubric/policy, require attributable human approvals,
  preserve judgments and report coverage without changing execution records.
- Dashboard submission, decisions, resource coverage, comparisons and human
  review operate independently of provider calls and the worker.

## Verified here

The Windows dependency constraints were incompatible with runtime requirements:
they still pinned Streamlit 1.66.0 and Pillow 12.3.0. They now match the required
[Streamlit 1.66.1](https://pypi.org/project/streamlit/1.66.1/) and
[Pillow 13.0.0](https://pypi.org/project/pillow/13.0.0/) releases. A new isolated
CPython 3.13.14 environment installed the full constrained development set and
passed `pip check`.

The original environment passed 1,916 tests in 215.77 seconds, then the added
local-login logging test passed. The fresh environment passed **1,917 tests in
233.58 seconds**; the subsequently added offline-demo test passed, including a
10.62-second final targeted run with HTTP/provider calls forbidden.
These runs cover all 1,918 currently collected tests. Model calls in tests are
fixtures, not live inference evidence. The dashboard HTTP health endpoint returned
200/`ok` on a loopback-only startup; interaction coverage comes from AppTest.
Development login no longer writes its verification code into logs.

A completed scripted comparison is retained under
`results/benchmarks/a2-offline-dashboard-20261101`, with its local evaluation at
`.runtime/p7-offline-evaluation-20261101`. Four trials complete the durable path
and validate offline. They remain simulation, have zero human judgments, and
cannot supply live quality/savings claims. Regeneration and startup instructions
are in [the current demo guide](../DEMO_GUIDE.md). Previous guides are preserved
under `docs/plans/`.

## Open experimental gates

| Gate | Observed state | What closes it |
|---|---|---|
| Human semantic quality | Reviewer unassigned; real profiles/outputs remain unreviewed | Genuine rubric judgments and coverage, with any needed prompt/calibration corrections |
| Second destination | User confirmed none available | Exact endpoint/model mapping, capacity and location evidence, destination-specific reviewed calibration |
| Current local backend | November 1 read-only probe returned connection-failed | A running configured Ollama service with the exact installed models |
| Workload/carbon support | Broader actual-context coverage and timestamped experimental carbon evidence remain insufficient | Calibrated size forecasts and defensible time/grid inputs; annual CO2 proxies cannot establish hourly benefits |
| Final experiment | Retained pilot is reused data; no final reviewed live comparison was performed | Independent final tickets, controlled observations/sensitivity studies and matched live trials retaining failures |
| Release platform checks | Local Windows checks passed; hosted Linux/Windows CI and manual browser/screen-reader rehearsal not observed | Validate the final source revision on those surfaces |

Unknown quality remains a hard optimizer gate. No scores, destinations or savings
were invented to close these items. There is no remaining permission question:
the missing items require people, infrastructure and real measurements. The user
can operate the local implementation and offline example now; full A2 completion
must remain unclaimed until these gates are evidenced.
