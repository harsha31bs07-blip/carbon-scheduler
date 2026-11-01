# Executed workflow accounting

New fixed and planned submissions retain an immutable accounting context beside
their request: a versioned API tariff book, optional carbon snapshot, explicit
host-telemetry choice and measurement scope. Planning estimates remain separate
from execution observations. Defaults enable no telemetry and assume only an
explicit zero Ollama API fee, bounded to this request's lifetime and exact
deployment configuration. Electricity and hardware ownership are excluded.

```powershell
python -m workflow.worker --db .runtime/queue.sqlite3 submit --config config/deployments.local.json --ticket "My CarbonDesk account is locked." --carbon config/carbon/india-cea-v22-static.json --energy codecarbon --energy-country IND
python -m workflow.worker --db .runtime/queue.sqlite3 run --once
python -m workflow.worker --db .runtime/queue.sqlite3 report WORKFLOW_ID
```

Both submission commands accept `--tariffs path/to/tariff-book.json`, `--carbon`,
`--energy none|codecarbon` and `--energy-country`. Tariff files use
`data.cost_accounting.TariffBook`'s versioned format; cloud prices are never
guessed. The planned command also uses its carbon snapshot when preparing
candidates. Restart uses saved snapshots, not mutable tariff/carbon file paths.
The India example is an annual historical CO2 proxy, not an hourly forecast.

## Persistence and failure ordering

1. After claim/preflight, an explicitly enabled offline tracker starts. Planned
   eligibility is checked again after tracker startup, before `mark_running`.
2. Exactly one provider dispatch returns a raw `ExecutionAttempt`. Output
   validation and the raw attempt/accepted job state are committed together.
3. Tracker cleanup follows that raw journal write. Enrichment calculates actual
   fees, energy and carbon from retained inputs, without another provider call.
4. A separate immutable `AttemptAccounting` record binds the complete raw attempt
   hash, accounting-context fingerprint and measurement timestamps. Appending
   accounting does not modify raw observations, outputs, jobs or plan tokens.

Cancellation, failed output validation and explicitly superseded late calls still
retain resource accounting. A crash before a returned attempt remains ambiguous;
recovery does not automatically dispatch it again. Raw journal failure retains an
orphan attempt when disk access permits, with separate orphan accounting. The job
continues to need reconciliation.

If accounting persistence fails after raw completion, the returned event names a
file in `DATABASE.orphan-accounting/`. Once the raw attempt is durable, retry only
the accounting write:

```powershell
python -m workflow.worker --db .runtime/queue.sqlite3 reconcile-accounting path/to/orphan-accounting.json
```

This command validates the saved record and inserts it idempotently. Conflicting
records are refused; it never calls a model. An enrichment failure or unavailable
orphan write leaves an explicit coverage gap while preserving completed output.

## Meaning of the metrics

- **Latency:** actual adapter duration, or the actual provider interval if the
  adapter duration is unavailable. Response/final time runs from arrival to the
  durable accepted job finishes on the relevant path. Per-attempt delay, queue
  and preparation wait, provider duration and journal interval are separate
  fields; their sums must not be added to end-to-end latency. Durable decisions
  retain planner preparation and health-probe timers; older decisions may lack
  probe timing.
- **Cost:** actual reported input/output usage times exact provider-model rates,
  with deployment, validity and verification checks. Missing usage at nonzero
  rates stays unknown. Confirmed no-dispatch failures have zero API fees. A
  zero API fee says nothing about electricity or machine cost.
- **Energy:** CodeCarbon host readings are accepted only for literal loopback
  destinations. The interval encloses the provider, validation, raw journaling
  and cleanup. Scope includes idle power and other host processes. Component
  metadata is retained; sensor/model composition remains mixed or estimated.
  Tracker availability alone cannot establish wholly measured energy. Remote
  inference energy remains unknown; coordinator power is not attributed to it.
  No uncalibrated wattage fallback is installed.
- **Carbon:** actual configured grid and the entire measurement interval must fit
  one retained valid, fresh, nonsimulated intensity sample, available as of
  accounting time. Crossing a sample boundary stays unknown; the implementation
  does not invent energy segmentation. CO2 and CO2e keep distinct units.
- **Quality:** end-to-end semantic quality stays unknown until attributable
  output review. Per-step scheduling proxies and format/citation validation are
  not promoted to reviewed workflow quality.

`report` includes every returned attempt, including failures and superseded calls.
Each resource has a known subtotal, unknown count, provenance counts and a total
only when coverage and units support it. Missing accounting, unrepresented
retries or ambiguous dispatched work prevent complete totals. A returned timeout
or other dispatched failure can leave work running beyond the observed window:
its energy/carbon stays a window subtotal, with no complete total. Overlapping
host intervals also block a complete total because their sums can double count
shared machine energy. Confirmed no-dispatch retries remain distinguishable from
missing provider usage. Future work in a
still-running workflow is not part of its current attempt totals. CO2/CO2e
subtotals are never pooled. No savings percentages are inferred.

## Compatibility and verification

Existing schema-3 raw attempts and legacy databases keep their meaning. New
accounting uses additive versioned SQLite tables and an explicit configuration
marker. Partial schemas, missing marked contexts, conflicting records, changed
raw hashes and rehashed metric inconsistencies are rejected.

New submissions export [queue replay](queue-replay.md) bundle 3, retaining
accounting plus optional planned context/history. Original bundle 1/2 examples
remain unchanged. Replay and reporting are offline and perform no inference.

```powershell
python -m workflow.queue_replay report --bundle path/to/exported-bundle
```

Offline fixtures cover all four steps, cleanup/enrichment/storage failures,
cancellation, late attempts, unknown usage, scope boundaries and compatibility.
No fresh live inference or measured savings are claimed for this implementation.
