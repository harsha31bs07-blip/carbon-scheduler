# Durable workflow evidence replay

Export one workflow from an existing queue into a **new** directory:

```powershell
.\.venv-a2\Scripts\python.exe -m workflow.queue_replay export `
  --db .runtime\queue.sqlite3 --workflow WORKFLOW_ID --output results\demo\queue-example
```

Inspect and validate the bundle offline:

```powershell
.\.venv-a2\Scripts\python.exe -m workflow.queue_replay inspect `
  --bundle results\demo\queue-example
.\.venv-a2\Scripts\python.exe -m workflow.queue_replay report `
  --bundle results\demo\queue-example
```

The Python API is `export_workflow(db, workflow_id, output)` and
`load_bundle(bundle) -> WorkflowSnapshot`. Export opens the existing database
read-only and captures one consistent validated snapshot. It includes the
request, permitted configuration, jobs, actual rendered prompts, source IDs,
outputs, plan history, attempts and events for that workflow only. Credential
environment-variable names may appear in the registry snapshot; credential
values are never read. Existing output directories are refused.

Each bundle contains exactly `workflow.json` and `manifest.json`. The manifest
identifies schema 3, the bundle version, workflow ID and SHA256 checksum of
`workflow.json`. The manifest is written last, so a partial export cannot pass
strict loading. Checksums detect changed files; they are not an authenticity
signature against someone rewriting both files.

Strict loading verifies the checksum, versions, supported fields and typed
request/candidate/attempt records. It also checks step and deployment identities,
generation controls, rendered-request constraints, persisted outputs and their
attempts, prerequisites, deadlines, cancellation/retry events and overall state.
Unknown provider timestamps/resources stay unknown. Failed, late, cancelled and
ambiguous calls remain evidence; superseded late outputs never become accepted
current-retry outputs. Malformed or inconsistent evidence is rejected.

Replay imports no worker or provider transports, performs no inference or network
calls, and never waits for original scheduled timestamps. The inspection's
`mode: replay` describes how the evidence is being read. Its original experiment
mode and execution facts remain intact inside the snapshot.

Legacy fixed workflows retain bundle version 1. Planned workflows without
accounting use bundle version 2,
which adds the immutable planning context and contiguous decision history. It
reconstructs prompts/candidates and checks planner results, incumbent links,
selected assignments, actual dispatch contexts and attempts. Honest infeasible
submissions can contain zero jobs and zero attempts. Legacy fixed bundles and
read-only older databases remain supported; planned evidence cannot be silently
exported as version 1. See [the planned worker guide](planned-worker.md).

New fixed/planned submissions use bundle version 3. It adds the immutable
accounting context and append-only records bound to raw attempts. Loading
recomputes fees/energy/carbon from retained evidence, checks journal chronology,
and rejects stripped marked contexts or conflicting/rehashed records. Missing
records remain visible gaps; they are not zero usage. Older read-only databases
without accounting tables and original bundle 1/2 files remain supported. See
[execution accounting](execution-accounting.md).

Replay does not restore a live queue, resolve ambiguous calls, execute missing
steps, establish policy savings or provide matched policy comparison. The
existing schema-2 replay path remains separate.

Prompts and ticket text are included so the stored outputs can be reviewed.
Choose a bundle's destination according to the intended audience before sharing
it or committing it as a curated demonstration.
