# Durable workflow worker

The SQLite worker executes connected steps independently of Streamlit. The fixed
submission command selects a model and deployment explicitly; its unknown metric
estimates do not represent an optimized five-metric decision. A separate
`submit-planned` command now connects staged joint planning to atomic claims and
dispatch-time validation. It is verified offline; real reviewed quality remains
a live gate. See [the planned worker guide](planned-worker.md).

## Local commands

Use the same Python environment as the rest of the project. Configure an explicit
Ollama deployment as described in [a2-foundations.md](a2-foundations.md).

```powershell
python -m workflow.worker --db .runtime/queue.sqlite3 submit --config config/deployments.local.json --ticket "My CarbonDesk account is locked after failed login attempts." --summary-delay-seconds 60
python -m workflow.worker --db .runtime/queue.sqlite3 run --max-jobs 3
python -m workflow.worker --db .runtime/queue.sqlite3 status WORKFLOW_ID
python -m workflow.worker --db .runtime/queue.sqlite3 run --idle-exit-seconds 90
```

The first worker exits after the urgent classification, retrieval and draft.
The second process executes only the remaining summary once its absolute start
time has arrived. A summary already due runs immediately. Stopping a worker
between steps does not regenerate completed outputs. Defaults are a 300-second
response deadline and a 600-second final deadline from submission; time spent
with no worker running still consumes those budgets.

`--once` processes at most one due job. Otherwise the worker polls every 0.5
seconds and exits after 30 idle seconds; `--max-jobs 0` has no count limit. Polling
uses real wall time. Tests use explicit simulated clocks and are not live timing
evidence. The short live delay demonstrates durability, not carbon savings.

```powershell
python -m workflow.worker --db .runtime/queue.sqlite3 cancel WORKFLOW_ID
python -m workflow.worker --db .runtime/queue.sqlite3 retry WORKFLOW_ID STEP_ID
python -m workflow.worker --db .runtime/queue.sqlite3 retry WORKFLOW_ID STEP_ID --acknowledge-possible-duplicate
```

Retries are explicit and limited to three by the saved configuration; a queue
caller can set a smaller `max_retries`. Provider dispatch itself has one attempt.
An earlier dispatched call requires the explicit duplicate-usage acknowledgment.
The worker never automatically retries an ambiguous external request. Cancellation
prevents downstream dispatch; it cannot forcibly cancel an in-flight provider.

## Durable boundaries

- SQLite transactions claim due work only when all prerequisites completed.
  Reserved claims consume the deployment's configured concurrency slots across
  workflows and connections.
- The original request, configuration fingerprints and all rendered prompts are
  persisted. KB, workload or deployment mismatches fail before inference.
- `mark_running` commits before contacting the provider. A heartbeat renews the
  lease using a separate SQLite connection. An expired dispatched lease becomes
  `needs-reconciliation` during worker recovery and is never automatically freed
  for another inference call.
- Completed attempts, validation outcomes and accepted outputs commit together.
  Format/citation validation does not establish semantic quality. Invalid outputs
  remain visible and cannot become downstream inputs.
- A late result retains incurred usage after cancellation or lease expiry. If an
  operator explicitly superseded that call, its old lease is checked against
  durable history and the result is archived without changing the newer retry.
- If result persistence fails, the worker attempts an atomic orphan record beside
  the database in `DATABASE.orphan-attempts/`. The job still needs reconciliation.
  A machine/disk failure before either write remains ambiguous; exactly-once
  external inference is not promised.

The worker retains actual provider IDs, usage, generation settings, provider wall
time and wait since prerequisite completion. New submissions also retain an
accounting context and append actual fee/energy/carbon records after raw output
is durable. Telemetry defaults off; unavailable observations stay unknown. See
[execution accounting](execution-accounting.md) for scope, reports and offline
accounting reconciliation.
This fixed-assignment path does not optimize or fall back. Fresh eligibility and
joint replanning belong to the planned path described above. A second real
destination remains a release gate.
