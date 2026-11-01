# Run artifacts

- Root-level recordings and benchmark files are historical evidence. Keep their
  original schema and provenance; they are not automatically eligible for current
  policy comparisons or strict replay.
- `runs/` contains new local execution logs and manifests. It is ignored by Git.
- `scratch/` contains temporary experiments and inventory output. It is ignored.
- `demo/` contains deliberately exported, checksummed replay bundles. Validate a
  bundle before committing it; do not add credentials or sensitive workload input.
- Future versioned evaluations belong in `benchmarks/<evaluation-version>/` with
  their workload, configuration, calibration and deployment snapshot identifiers.

The coordinator's SQLite queue belongs in `.runtime/`, outside this
evidence tree. No historical files were relocated or deleted by this convention.

Curated queue examples include `demo/a2-local-restart-20261019/` (fixed live
execution, bundle 1) and `demo/a2-planned-infeasible-20261026/` (durable planned
rejection, bundle 2, zero execution attempts). Inspect them with
`python -m workflow.queue_replay inspect --bundle PATH`. Neither is a matched
five-metric policy comparison or evidence of optimized carbon savings.

New worker submissions retain accounting and export bundle 3. Use
`python -m workflow.queue_replay report --bundle PATH` for offline resource
coverage and response/final timing. The original curated bundles remain
unchanged; absent historical accounting is not backfilled with plan estimates
or assumed zero usage. P4 compatibility reports are retained locally under
`scratch/p4-accounting-compatibility-20261026/`.

Matched policy packages use `workflow.evaluate_policies export/inspect`; their
own protocol/source/journal and every finished queue bundle are checksummed and
recomputed offline. Prepared/interrupted packages retain unfinished denominators
and are not final measurement evidence. The current local preparation under
`.runtime/p6-local-pilot-v2-20261101/` and its portable export
`.runtime/p6-prepared-comparison-v2-20261101/` have 48 pending trials and zero
completed trials; no model calls were made. See
[policy evaluation](../docs/policy-evaluation.md).

`benchmarks/a2-offline-dashboard-20261101/` is a completed scripted comparison
for offline UI demonstration. Its four trials are explicitly simulation, retain
unknown semantic quality/energy/carbon, and supply no live savings evidence.
Regenerate using `python -m workflow.demo_evidence --output NEW_DIRECTORY`.
The original local evaluation at `.runtime/p7-offline-evaluation-20261101`
supports practicing the blank review form without changing the portable package.
