# Integrated local dashboard

Login requests enforce a backend 60-second resend cooldown and at most three
codes per normalized email in ten minutes. Expiring or consuming a code does not
erase that request history. Codes expire at five minutes and verification is
single-use. Authentication mutations are serialized within the server process;
the in-memory store resets on restart and is intended for this local application.
Successful login removes the displayed development code. Logout from either
workspace clears current-session codes, evidence and unfinished review fields.

The default authenticated workspace is **Ticket workflows**. **Activity** selects
workflow submission/inspection or offline comparison evidence. The legacy policy
workspace remains separate and retains its original scenario semantics.

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

The human-review form is available for original evaluations with a frozen review
policy. Every score starts blank. Each criterion has its description, score and
rationale. The reviewer supplies attribution and overall rationale and explicitly
approves. Backend checks include arithmetic, identities, hashes and chronology.
Imports are additive; raw attempts remain unchanged. Existing judgments and
portable packages cannot be overwritten. Reload after approval to update
coverage. See [workflow quality review](workflow-quality-review.md).

Native Streamlit controls have visible labels, and states/coverage are expressed
in text rather than color alone. AppTest covers submission, cancellation,
infeasibility, invalid settings, comparisons and human approval without provider
calls. Manual NVDA/browser accessibility verification has not been performed;
this is not a full WCAG compliance claim.

The completed scripted example at `results/benchmarks/a2-offline-dashboard-20261101`
can be loaded without an inference service. It is explicitly simulation evidence.
The original local evaluation at `.runtime/p7-offline-evaluation-20261101` also
supports the blank human-review form. See [the demo guide](../DEMO_GUIDE.md).
