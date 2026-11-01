# A2 foundation APIs (M1)

The original executor retains legacy schema-2 runs. Schema-3 APIs power the
[connected workflow](connected-workflow.md), [durable planned worker](planned-worker.md)
and [integrated dashboard](dashboard.md). This foundation guide preserves the
adapter boundary; reviewed live geographic scheduling and measured savings remain
release gates.

## Inventory and routing

```powershell
Copy-Item config/deployments.example.json config/deployments.local.json
.\.venv-a2\Scripts\python.exe -m workflow.deployment_inventory --config config/deployments.local.json
```

The inventory loads explicit deployment/model mappings and probes Ollama's
`/api/tags` without inference. It records check time, reachability, and observed
configured models. Cloud endpoints are marked unprobed (`reachable: null`);
configured keys or models do not establish availability. HTTP redirects are
rejected so a call cannot silently move to a different endpoint.

For Ollama, `endpoint` is a base URL; the adapters append `/api/generate` or
`/api/tags`. For Gemini it is the SDK API base URL. For NVIDIA it is the full
chat-completions URL. Cloud entries require `credential_env`, the name of an
environment variable resolved at call time. JSON configuration never contains
credential values. Model mappings use the project's canonical ID as the key and
the exact provider model ID as the value. Unknown physical routing stays unknown.

The default registry contains only local Ollama. Configure a second real
destination before geographic-shifting experiments. Multiple endpoints on the
same machine do not establish different grids.

## One due step

This example performs inference if Ollama is reachable and the model is installed:

```python
from shared.deployments import load_deployments
from shared.scheduling import MetricEstimate, PlannedCandidate, StepEstimates, SystemClock
from workflow.dispatch import dispatch_planned_candidate
from workflow.scheduling_records import save_record

registry = load_deployments("config/deployments.local.json")
deployment = registry.require_model("local-ollama", "local-llama3.2")
unknown = lambda unit: MetricEstimate(None, unit, "unknown")
candidate = PlannedCandidate(
    candidate_id="ticket-1-classify", step_id="classify",
    model_id="local-llama3.2", deployment_id=deployment.deployment_id,
    planned_start=SystemClock().now(), region=deployment.region,
    estimates=StepEstimates(unknown("s"), unknown("ratio"), unknown("USD"),
                            unknown("kWh"), unknown("gCO2e")),
)
attempt = dispatch_planned_candidate(
    candidate, registry, "Classify this support ticket: I cannot sign in.",
    workflow_id="ticket-1",
)
save_record("results/scratch/ticket-1-attempt.json", attempt)
```

`dispatch_planned_candidate` rejects a future start before making a model call.
It dispatches once, without changing endpoint/model or automatically retrying a
possibly completed request. The durable worker enforces dependency readiness,
deadlines, capacity, persistence and restart reconciliation. Per-request
connection/read inactivity timeouts are bounded; they are not total workflow
deadlines. `call_deployment` separately supports bounded, explicit same-pair
retries for direct adapter use.

Success records preserve canonical planned model, exact dispatched provider model,
deployment ID, and any separate provider-reported model/version. Failures retain
safe error codes and distinguish preflight failures from attempted requests.
Client wall time includes network/model loading; it is not inference-only time.
Host energy, carbon, monetary cost, quality and unreported usage remain unknown
unless supplied by the separate [accounting layer](execution-accounting.md). The coordinator's own power is
never used as a measurement of a remote inference host.

## Versions and comparison boundaries

`shared.scheduling` normalizes aware timestamps to UTC and uses seconds for
durations. It validates finite values, metric units/provenance, deadlines and DAG
dependencies. Carbon samples include issue time and a half-open validity interval;
the carbon provider interface can return no sample when coverage is missing.

`save_record`/`load_record` handle typed schema-3 JSON records atomically and reject
unknown types/versions, duplicate fields and non-finite JSON numbers.
`ComparisonContext` fingerprints actual workload identity and version,
configuration, deployment snapshot, calibration, policy implementation version,
reference UTC time and experiment mode. Invariant configuration excludes the
policy choice and weights so different policies can share the same cohort.
Decisions separately retain workflow ID, policy name and the exact five weights.
Run IDs do not affect compatibility.

Historical schema-1/2 JSONL runs remain readable through `workflow.run_store` and
eligible only for their existing comparison/replay rules. They are not silently
promoted into schema-3 evidence. `legacy_candidate_to_planned` explicitly converts
integral hour offsets to absolute UTC starts, requires a caller-resolved deployment,
retains the historical location label separately and labels old metrics as
unverified estimates. Fractional legacy hours are rejected.
