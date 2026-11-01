# Planning from live workflow state

`workflow.planning.prepare_plan()` connects the calibrated candidate generator
to actual connected-workflow state. It prepares a joint decision without
performing inference, probing endpoints or modifying the durable queue. The
worker has a planned path using atomic installation and dispatch-time
revalidation. See [durable optimized execution](planned-worker.md). Real reviewed
quality remains a live gate.

## Inputs and guarantees

Supply a schema-3 `WorkflowRequest`, deployment registry, validated frozen
profiles, the matching KB, current UTC time, explicit health snapshots and a
carbon snapshot provider. Supply capacity reservations from the coordinator.
Live-mode preparation requires profiles with an explicit frozen review coverage
policy. An empty health snapshot leaves every unfinished destination ineligible.
Provider membership in configuration is never treated as observed health.

Completed outputs must correspond exactly to completed `CommittedAssignment`
records. Dependencies must have completed before any dependent assignment is
dispatched. Running assignments retain their original candidate and an explicit
expected finish; completed assignments retain actual finish time. The service
validates output formats/citations and never creates expected fixture outputs.

Only steps whose prerequisites have completed are rendered. Their actual UTF-8
prompt sizes select matching empirical profiles. A changed output can change
the next prompt's size, estimate, eligible model and winner. A prompt outside
calibrated coverage remains unsupported even if an earlier forecast fit.

## Provisional downstream work

Full-workflow planning needs estimates for unfinished downstream steps. Supply a
`shared.scheduling.WorkloadSizeForecast` for each blocked step that should be
considered. It binds:

- Workflow input/KB fingerprint from `workflow.connected.workload_fingerprint`.
- Workload version, step and frozen profile fingerprint.
- Minimum and maximum input size in the same UTF-8 byte proxy as calibration.
- Issue time, validity horizon, and an explicit versioned source/assumption.
- Estimated or simulated provenance; simulated metrics are excluded in live mode
  with retained `mode_exclusions` reasons.

These intervals must come from a documented calibration study or declared
simulation. This implementation does not manufacture intervals from fixture
answers or claim that empirical ranges guarantee future prompt size. It checks
both endpoints against each matching profile and evaluates latency at the upper
endpoint. The interval's validity covers each candidate's planned execution.
All candidate sources and diagnostic records identify provisional estimates.
Missing forecasts produce empty candidate pools and explicit infeasibility.
Stale, future-issued or mismatched forecasts are invalid input.

As outputs arrive, replace provisional estimates with rendered prompts and
prepare again. Unused forecasts cannot override an actual prompt. Preserve every
previous report as decision history. Never dispatch a provisional prompt.

## API

```python
from workflow.planning import prepare_plan
from shared.run_records import write_json

prepared = prepare_plan(
    request, registry, profiles,
    kb=knowledge_base,
    now=clock.now(),
    health=observed_health,
    carbon_provider=carbon_snapshot,
    outputs=accepted_outputs,
    fixed_assignments=committed_execution,
    forecasts=calibrated_size_forecasts,
    reservations=coordinator_reservations,
    start_offsets_seconds=(0, 60, 120),
    quality_floor=.8,
    trigger="prerequisite-completed",
    previous_plan=previous_winner,
    minimum_improvement=.01,
)
write_json(new_report_path, prepared.report)
```

The request policy selects speed/balanced/eco, or `custom` with an explicit
`custom_weights` mapping for `latency`, `accuracy`, `cost`, `energy`, `carbon`.
Invalid, boolean, negative, nonfinite, incomplete or all-zero weights fail
before planning. Set `experiment_mode="simulation"` for synthetic fixtures;
simulation is retained in the report and is never executed by this service.

The service closes a bounded start grid with predicted prerequisite finish times
and reservation boundaries. Nondeferrable steps still obey the joint planner's
earliest-capacity rule. Every start is generated with its own carbon sample;
existing candidates are not moved while retaining old carbon values. Actual
elapsed time reduces remaining deadline budgets. Freshness/availability is
checked from supplied health, and the worker must refresh it again at dispatch.

The returned `PreparedPlan` contains the typed `PlanResult`, actual rendered
steps, and a JSON-safe report. The report retains accepted outputs, materialized
prompts or explicit forecast context, registry/profile/policy identity, health,
reservations, candidates/exclusions, committed facts, normalized scores, trigger,
previous-plan fingerprint, search bounds and preparation/planner overhead.
`dispatchable_step_ids` identifies due, actually materialized steps from a
feasible winner. It is an informational list, not a lease or permission to call
the provider. The report is a preparation artifact, not an executable queue
bundle or a replacement for strict replay validation.

## Validation and remaining work

Offline fixtures verify actual-output size changes switching model choice,
replacement of provisional estimates, frozen execution, deadline overruns,
capacity boundaries, stale health/forecasts, unsupported sizes, custom weights,
missing assumptions, and schema-3 forecast persistence. They forbid HTTP calls
and use of calibration fixture answers.

The real local profiles remain unreviewed. The new
`results/benchmarks/m3-local-gated-20261026/` aggregate retains all 99 trials and
the explicit pilot review policy, with **zero of eight profiles quality-eligible**.
It is not evidence of live optimized inference. Atomic plan revisions, claim
coordination, durable preflight failures and replan rate limiting are implemented
at the worker boundary. Executed metric accounting and live reviewed validation
remain incomplete. This pure service honors an explicit incumbent improvement
threshold.

A local preparation probe on 2026-10-26 rendered the real classification prompt
and returned infeasible with the other three contexts explicitly unavailable,
no inference, and no dispatchable step. It supplied no observed health or carbon
and invented no downstream forecasts. Transient report:
`results/scratch/p2-local-preparation-20261026.json`.
