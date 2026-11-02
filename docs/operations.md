# Queue operations and recovery

Workers retain informational observations for polling, idle, running and stopped
states; lease heartbeats also update running observations. After thirty seconds
the display says **stale**, which does not establish a stopped process, release
leases or permit retries. Default worker IDs are unique; avoid sharing IDs across
simultaneous processes. Overview/workflow views read observations separately from
execution authority.

```powershell
python -m workflow.operations inventory --db .runtime/queue.sqlite3
python -m workflow.operations backup --db .runtime/queue.sqlite3 --output .runtime/backups/checkpoint-1
python -m workflow.operations restore --backup .runtime/backups/checkpoint-1 --output .runtime/restored-checkpoint-1
```

Inventory performs no migrations, recovery, deletion or inference. It reports job
states/deadline misses, retry indices, oldest scheduled start, accounting gaps,
presence, sidecar counts, database size and retained planning duration sums.
Profiling samples up to twenty summaries and their snapshot validation; local
timings do not establish throughput or stable-tail performance. Retention is
inventory-only, with no delete operation.

A 2026-11-02 local synthetic sample queued 500 workflows without provider calls.
Twenty summaries took approximately 0.031 seconds and full validation of those
twenty snapshots took 0.172 seconds. Bounded planner fixtures examined 10, 100 and
1,000 of 810,000 offered combinations in approximately 0.001, 0.006 and 0.051
seconds; all three correctly reported truncation. These single-machine diagnostic
samples justify retaining the current architecture, not extrapolating production
throughput or optimality. Local evidence is retained at
`.runtime/app-profile-20261102-d96f765e/profile.json`; all profiles are explicitly
simulation and supply no real quality/energy/carbon evidence.

Stop workers for an application-wide backup including orphan evidence. Backup
uses SQLite's backup API to include committed WAL contents, validates integrity,
foreign keys, supported schema and every workflow, and copies orphan JSON bytes
with checksums. Its database snapshot is consistent; sidecars are individual
captures, not atomic with a concurrently active worker. The manifest states this
limit. Local bounds are 256 MiB and approximately 4,096 files. Failed/interrupted
backups without a completed manifest are not valid backups.

Backup and restore outputs must be new directories. Restore verifies paths,
checksums and queue semantics before creating the isolated output; existing
directories are never overwritten. Its database is named `queue.sqlite3`, with
sidecars restored using the worker's expected names. Neither command starts a
worker, sends inference, reconciles uncertain calls or grants retries.

Inspect restored status and orphan evidence before explicitly executing from a
restored queue. A call can remain ambiguous after a process exits. Use existing
reconciliation/retry commands and acknowledge possible duplicates where required.
Do not run the restored copy alongside the original as though they share leases.
Hashes establish consistency, not authenticity; retain raw tickets/outputs under
the operator's local data policy.
