# Durable optimized workflow execution

The durable worker accepts fixed assignments and calibrated joint plans. Planned
work uses `workflow.planned_worker`, the staged planner, and the same provider
attempt/lease/reconciliation boundary as fixed work. The integrated path is
verified with deterministic provider fixtures; real local profiles remain
unreviewed and therefore cannot execute an optimized plan yet.

## Submit and run

Use frozen profiles with an explicit [review coverage policy](quality-review.md)
and a registry matching every included profile's exact deployment fingerprint.
Ready prompts are rendered from actual inputs/accepted outputs. Each blocked
step requires an explicit current [size forecast](staged-planning.md); a forecast
file contains a JSON list of schema-3 `WorkloadSizeForecast` records. Missing
forecasts or unknown required metrics produce an infeasible submission, not a
fallback to assumed quality or a fixed assignment.

```powershell
python -m workflow.worker --db .runtime/queue.sqlite3 submit-planned --config config/deployments.local.json --profiles path/to/reviewed/profiles.json --forecasts path/to/workload-forecasts.json --carbon path/to/carbon-snapshot.json --policy balanced --ticket "My CarbonDesk account is locked after failed login attempts."
python -m workflow.worker --db .runtime/queue.sqlite3 run --once
python -m workflow.worker --db .runtime/queue.sqlite3 run --idle-exit-seconds 90
python -m workflow.worker --db .runtime/queue.sqlite3 status WORKFLOW_ID
```

The carbon input is an explicit versioned static estimate or timestamped trace.
It stays a frozen snapshot with freshness and interval checks; this release does
not fetch new forecasts in the background. A stale snapshot suspends eligibility.
New calibration/review inputs require a new submission instead of silently
replacing evidence in an existing run. Provider API credentials remain in
environment variables; saved registry data contains only their variable names.

Options include response/final deadlines, an earliest background-summary delay,
speed/balanced/eco/custom policy, quality floor, finite start offsets, provider
timeout and thinking controls. Custom `--weights` requires exactly `latency`,
`accuracy`, `cost`, `energy`, and `carbon`, with finite nonnegative numeric values
and a positive sum. `--thinking provider-default` selects nullable controls;
profiles and adapters must support the exact requested generation settings.

Control defaults are a 10-second replan interval for blocked/waiting requests,
3-second bounded health probes, 300-second maximum health age, and a five-second
maximum decision age at dispatch. Changed accepted outputs and due feasible
work trigger immediate preparation. Planning/probe elapsed time consumes the
original deadlines. CLI `--max-jobs` counts processed jobs; planning-only events
do not reset the worker's idle-exit timer or count as inference jobs.

Python callers can use `submit_planned()` to build the request and retain its
context, or construct forecasts against a preview `WorkflowRequest`. Its
workload fingerprint binds ticket/KB/prompt version, independently of run ID.
Forecast intervals need a documented calibration source; the worker does not
derive a guaranteed bound from a few observed outputs.

## Atomic dispatch protocol

1. The queue captures the target workflow, complete planning context, revision,
   and shared occupancy in one consistent snapshot. Active external claims,
   including ambiguous/cancelled calls, conservatively reserve their slots
   through the target workflow's remaining horizon; no release time is invented.
2. Outside write transactions, the worker probes configured capabilities, renders
   ready prompts and prepares the entire remaining plan. Completed assignments
   use actual provider starts and durable completion times, including persistence
   and validation overhead. Dispatched work stays frozen.
3. A short SQLite transaction checks the snapshot token, accepted outputs,
   completed facts, cancellation and capacity. It installs a contiguous decision
   revision and claims one due, materialized step atomically. Changed state or
   another worker's claim produces a conflict and no provider call.
4. Before `mark_running`, the worker checks decision/health freshness, rendered
   prompt identity, remaining plan deadlines and carbon coverage at the actual
   proposed dispatch time. A changed carbon window or expired decision releases
   the undispatched claim and retains a preparation failure for replanning.
5. `mark_running` verifies the latest revision, matching actual prompt, lease,
   cancellation, capacity and freshness again. Only then can the selected
   deployment receive the request. Returned output/usage follow the existing
   atomic completion, late-attempt and orphan-evidence rules.

Fixed claim polling skips planned workflows. An infeasible submission can have
zero jobs; it still retains the original request, context and rejection report.
An infeasible revision suspends previously scheduled assignments. Preparation
failures retain a safe error code and cannot masquerade as provider attempts.

A fresh capability snapshot may route to another eligible model/destination
before dispatch. After a provider call fails, retries remain explicit and
bounded. An ambiguous dispatched call still requires duplicate-usage
acknowledgement; it is never automatically repeated. Cancellation prevents
downstream dispatch while retaining already incurred usage. Conservative
occupancy can reduce feasible plans while another call is unresolved; this is
intentional and does not establish optimal multi-workflow utilization.

## Evidence and replay

Existing queue/schema versions keep their meaning. Opening a queue adds versioned
planning tables without rewriting legacy attempts. Older databases lacking the
planning tables still support read-only fixed export. Each planned context
retains the registry/configuration association, KB, profiles, forecasts, carbon,
weights, constraints and control settings needed after restart. Decision history
includes full reports, revisions, timestamps and opaque snapshot tokens.

Strict validation reconstructs prompts and candidates from retained inputs and
recomputes bounded planner results offline. Previous-winner links must refer to
retained plans; an incumbent explanation cannot invent an earlier decision.
Validation caches are bounded per connection and keyed by exact persisted input
bytes. Changed evidence is revalidated, and copies prevent callers mutating the
cache. Checksums and hashes establish content consistency, not authenticity.

```powershell
python -m workflow.queue_replay export --db .runtime/queue.sqlite3 --workflow WORKFLOW_ID --output results/demo/new-planned-bundle
python -m workflow.queue_replay inspect --bundle results/demo/new-planned-bundle
```

Legacy fixed workflows use bundle version 1. Planned workflows without accounting use version 2,
which retains and validates context/history, actual materialized dispatch,
attempts and inferred state, including infeasible submissions with no jobs.
Replay imports no worker/provider transports, calls no models, and does not
wait for original timestamps. See [queue replay](queue-replay.md).
New submissions retain accounting and export bundle 3, with both planning and
accounting evidence. Original retained demonstrations keep their versions.

## Retained verification and remaining gates

`results/demo/a2-planned-infeasible-20261026/` retains workflow
`906ca02a0e634714a04ddee7e2a034bc`: one planning revision, zero jobs, zero attempts,
unknown reviewed quality, no observed available destination, and three missing
downstream size forecasts. Submission made only bounded read-only capability
probes. Export and offline inspection succeeded. This is evidence of honest
durable infeasibility, not live optimized inference or savings.

Offline integration tests cover all four steps, actual output chaining, delayed
restart from saved snapshots, endpoint outage routing, concurrent workers,
cancellation during planning/provider execution, ambiguous crash recovery,
explicit preflight retry, probe overhead, actual-time carbon boundaries,
expired decisions, rate limiting and CLI rejection inspection.
An additional controlled eco-policy test chooses a lower-intensity future window
without a forced earliest-start delay and records actual queued dispatch at that
time. This is a synthetic test, not measured emissions reduction.

Executed resource observations now use append-only accounting after raw output
is durable; the shared dispatch adapter's original observations stay unchanged.
Telemetry defaults off and semantic quality remains unreviewed. See
[execution accounting](execution-accounting.md). Remaining work is
live accounting validation, human review and broader calibration, a second real
destination, final matched comparisons and optimized dashboard submission.
