# Integrated local dashboard

Submission uses a durable token bound to the ticket, settings and retained input
snapshots. Repeated clicks return the existing workflow. Changed details require
**Start another ticket**, which also permits intentional repeats. Tokens survive
queue reopen; browser sessions hold the token rather than a cross-device identity.
History supports ticket/ID search, status/date filters and pages of twenty records,
including work older than the newest hundred entries. Optional five-second refresh
runs only the progress fragment with a fresh database connection; it never submits
work or refreshes an evidence review form. Progress explains intentional waits and
uncertain calls. Worker observations distinguish reported idle/running/stopped
states from stale data without granting dispatch or retry authority; see
[operations](operations.md).

**Review inbox** supports separate calibration/workflow judgments and local
evidence-bound drafts; see [review inbox](review-inbox.md). Comparison tables and
charts retain units, sample counts, failures and missing coverage. Changed evidence
invalidates the cached report. A truncated search is identified separately from
exhaustive infeasibility or optimality.

Login requests enforce a backend 60-second resend cooldown and at most three
codes per normalized email in ten minutes. Expiring or consuming a code does not
erase that request history. Codes expire at five minutes and verification is
single-use. Authentication mutations are serialized within the server process;
the in-memory store resets on restart and is intended for this local application.
Successful login removes the displayed development code. Logout from either
workspace clears current-session codes, evidence and unfinished review fields.

The default authenticated workspace is **Overview**. It shows local configuration,
reviewed profile coverage, selected forecast/carbon inputs and installed dependency
versions. Availability and worker presence remain unknown until observed; a valid
configuration never approves a dispatch. The overview reads worker observations
from an existing queue without migrating it. **Open offline demonstration** validates the bundled four-trial
simulation without requiring a path or running an inference service.

Choose **Ticket workflows** to submit and inspect work. **Activity** selects
workflow submission/inspection or offline comparison evidence. The legacy policy
workspace remains separate and retains its original scenario semantics.

Login, styling and legacy views live in separate modules. Legacy execution and
chart dependencies load only when that workspace is selected. Startup commands
use the active Python interpreter, selected database and literal shell quoting;
the worker command exits after 90 idle seconds, so restart it for later work.

**Fixed assignment** preserves explicit endpoint/model selection. **Optimized
policy** saves the registry, requested policy/custom weights, deadlines, profiles,
size forecasts and carbon snapshot. Unknown health is recorded as
`awaiting-worker-verification`; it establishes neither availability nor outage.
Dashboard submission performs no provider probes or generation. The separate
worker checks eligibility. Current unreviewed profiles remain infeasible.

Planning evidence uses local JSON file paths for this operator application.
Snapshots, not those paths, persist. Delays and weights are validated before
enqueue. Infeasible zero-job submissions retain a four-step denominator, decision
history, candidate diagnostics and score details. The displayed worker command
uses the actual database. Cancellation prevents further dispatch and preserves
incurred usage; it cannot stop an already running provider request.

Workflow inspection includes planned/executed identities, geography, UTC times,
deadlines, accepted outputs and failed/cancelled attempts. Resource tables show
complete totals only when coverage supports them, plus partial subtotals and
provenance. Timing and missing measurement evidence are separate.

**Comparison evidence** validates an evaluation directory or portable comparison
offline. It displays all outcomes, deadline results, resource/quality coverage,
paired deltas and full evidence. Simulation is labeled; zero/unknown baselines
produce no percentage. Trial views expose actual ticket, prompts and outputs.
**Load bundled offline demonstration** opens the same validated simulation without
typing a path. A failed load removes the previous cached report.

Local diagnostics are also available outside Streamlit:

```powershell
python -m workflow.readiness
python -m workflow.readiness --config config/deployments.example.json --probe
```

The default command only inspects local JSON schemas, KB, profiles, optional typed
forecasts/carbon snapshots, queue-file presence and installed package versions.
Use `--profiles`, `--forecasts`, `--carbon` and `--db` for other inputs. Relative
paths resolve against `--root` (the project by default). Registry selection is
explicit `--config`, then `CARBON_DEPLOYMENTS_FILE`, then local/example config.
Neither command loads `.env`; set environment variables in the calling shell.
Exit 2 denotes invalid configuration/input or missing runtime dependencies; exit 0
means diagnostics ran without those errors, even when review/availability gates
remain open. Read the component statuses; the report always has
`dispatch_approved: false` because it is not request-specific eligibility.

`--probe` explicitly performs read-only provider inventory without generation.
It supports at most sixteen destinations with a three-second connection/read
inactivity timeout per request; it is not a total wall-clock deadline. Cloud
verification remains unknown where the adapter cannot establish availability.
The command reports canonical observed models, timestamps and safe error states
without returning endpoint URLs or credentials. It does not persist observations;
the dashboard continues to show unknown until a future retained health integration.

The human-review form is available for original evaluations with a frozen review
policy. Every score starts blank. Each criterion has its description, score and
rationale. The reviewer supplies attribution and overall rationale and explicitly
approves. Backend checks include arithmetic, identities, hashes and chronology.
Imports are additive; raw attempts remain unchanged. Existing judgments and
portable packages cannot be overwritten. Approval automatically refreshes coverage. See [workflow quality review](workflow-quality-review.md).

Native Streamlit controls have visible labels, and states/coverage are expressed
in text rather than color alone. AppTest covers submission, cancellation,
infeasibility, invalid settings, comparisons and human approval without provider
calls. Manual NVDA/browser accessibility verification has not been performed;
this is not a full WCAG compliance claim.

The completed scripted example at `results/benchmarks/a2-offline-dashboard-20261101`
can be loaded without an inference service. It is explicitly simulation evidence.
The original local evaluation at `.runtime/p7-offline-evaluation-20261101` also
supports the blank human-review form. See [the demo guide](../DEMO_GUIDE.md).
