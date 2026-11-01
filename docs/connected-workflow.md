# Connected workflow (M2)

Run an input-driven ticket workflow with one explicit model and deployment:

```powershell
.\.venv-a2\Scripts\python.exe -m workflow.connected --ticket "My account is locked after failed login attempts." --config config/deployments.example.json --run-dir results/runs/my-ticket --response-only
.\.venv-a2\Scripts\python.exe -m workflow.connected --resume results/runs/my-ticket --config config/deployments.example.json
```

Use a new run directory for each new ticket. Alternatively, pass `--ticket-file`
with a UTF-8 text file. Defaults are `local-ollama` / `local-llama3.2`; select an
explicit configured pair with `--deployment` / `--model`. The current mode is
`fixed-immediate`: it establishes connected execution before M4 joint scheduling.
It does not apply speed/eco optimization or choose future execution windows.

## Actual dependency data

1. `classify` reads the supplied ticket and returns one supported category.
2. `retrieve` queries the versioned local KB using that category and actual ticket
   terms, then synthesizes the selected evidence with `[KB-...]` citations.
3. `draft` uses the ticket, actual category and actual retrieved evidence. Its
   allowed source IDs come from citations present in the retrieval output.
4. `summarize` reads the actual preceding outputs and produces the CRM summary.

The KB describes a fictional product; these records are workload fixtures. No
matching evidence remains explicitly unavailable. Invalid categories, unknown
source IDs, empty output, explicit provider truncation and incomplete generation
stop downstream execution. These checks establish format and evidence boundaries,
not measured semantic quality. M3/M6 will calibrate and evaluate actual outputs.

`data/workloads/support_tickets.v1.json` keeps calibration and held-out examples
separate from one another and from the legacy evaluation set. Workload identity
binds prompt and KB versions; changed inputs and dependency outputs change the
materialized prompts and their fingerprints.

## Deadlines and generation controls

The response deadline covers classification, retrieval and draft. The summary
has a separate completion deadline. `--response-only` saves the urgent reply and
leaves the summary pending; rerunning with `--resume` finishes it without
regenerating completed outputs. `--all-urgent` puts all four steps under the
response deadline, including when `--response-only` is supplied.

Defaults are 300 seconds for the response and 600 for completion, measured from
arrival; `--response-seconds` / `--completion-seconds` set different budgets.
Resume retains the original deadlines and settings. Cold model loading can use a
large part of that budget, so choose explicit budgets for experiments and retain
missed deadlines rather than resetting them.

The runner checks deadlines before materialization, after journal persistence,
immediately before provider dispatch, and after output validation/persistence.
The remaining budget limits each request's configured inactivity timeout.
Requests can still overrun a deadline while in flight; this is recorded as
`deadline_missed`, not claimed as hard real-time cancellation. Failed/invalid
late attempts keep their provider and validation evidence as well.
Known deadline violations are persisted atomically with the attempt; recovery
also audits older saved attempt times. This records observed outcomes without
promising an exact commit timestamp or exactly-once external inference.

Prompt-size counts use an explicitly labeled UTF-8 byte proxy, not provider token
measurements. Actual reported token usage is retained separately. Current output
limits are 24/192/256/128 tokens for classify/retrieve/draft/summarize. Limits go to
the selected provider unchanged. `--thinking off` is the local default; `on`
requests native Ollama thinking and `provider-default` leaves it unspecified.
Explicit thinking controls for cloud adapters are rejected until implemented,
rather than silently ignored.

## Saved progress and restart

Each run's `workflow.json` keeps the original request, rendered prompts, source
IDs, KB lookup duration, prompt fingerprints, attempts and validated outputs.
Atomic replacements preserve output and its attempt together. The selected
model/deployment, registry fingerprint and workload fingerprint are checked on
resume. New-run settings cannot silently replace saved settings.

An exclusive lock supports one runner per directory. A crash after a step starts
can leave an external request's outcome ambiguous. Such a run becomes
`needs-reconciliation`; stale locks also require explicit operator reconciliation.
The CLI does not automatically retry, break locks, or repeat completed inference.
Failed runs remain inspectable; a new run is a separate experiment. The leased
SQLite queue, automated cancellation and recovery policy belong to M5.

Ordinary runs live under ignored `results/runs/`. Live probes use ignored
`results/scratch/`; curated evidence can later be exported deliberately. The
legacy dashboard and strict replay continue using their original schema-2 path.

## Current verification

On 2026-10-19, 772 offline tests passed. A live Llama workflow saved its urgent
reply and then completed only the summary in a separate CLI process; the final
journal contains exactly four attempts. Evidence is in
`results/scratch/m2-live-connected-v4-20261019/workflow.json`. Earlier failed and
interrupted probes remain alongside it.

The live draft still implied an account-status review that had not happened and
included unnecessary MFA guidance. Format/citation validation does not establish
semantic quality; these findings belong in M3/M6 calibration and held-out review.
Energy, carbon, quality and cost remain unknown in this runner. Shell environment
variables supply any cloud credentials; this API path does not automatically load
the legacy `.env` file.
