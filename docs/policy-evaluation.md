# Matched policy evaluation

`workflow.evaluate_policies` freezes a protocol before inference, executes each
strategy through the durable worker, and exports a comparison that can be
recomputed offline. It retains failed, infeasible, interrupted and unfinished
trials. This tooling does not establish semantic quality or savings by itself.

## Freeze, review, run

```powershell
python -m workflow.evaluate_policies freeze --protocol-id local-pilot-v1 --output .runtime/evaluations/local-pilot-v1 --config config/deployments.local.json --profiles path/to/reviewed/profiles.json --forecasts path/to/case-forecasts.json --carbon path/to/carbon-snapshot.json --repeats 3 --seed 42
python -m workflow.evaluate_policies report --evaluation .runtime/evaluations/local-pilot-v1
python -m workflow.evaluate_policies run --evaluation .runtime/evaluations/local-pilot-v1 --max-trials 1
python -m workflow.evaluate_policies run --evaluation .runtime/evaluations/local-pilot-v1
```

Freeze performs no capability probes or model calls. Inspect `protocol.json`
before running: it retains the actual tickets, KB, registry, profiles, forecasts,
carbon, optional tariffs, quality floor, deadlines, generation settings, arm
constraints, seed and source hashes. Source copies are saved under `source/`.
All inputs are snapshots; changing the original files does not update a protocol.
Execution refuses changed implementation source. Create a new protocol for a
changed experiment rather than rewriting prior evidence.

`run` performs real inference for eligible live trials. `--max-trials` counts
finished trial outcomes, including failures and infeasibility. A later run resumes
the remaining schedule. No model calls were made while implementing this slice.
The actual local quality profiles remain unreviewed, so optimized arms remain
infeasible until the existing quality and context-coverage gates are satisfied.
Explicit fixed baselines retain unknown semantic quality; they do not bypass a
claim that quality has been demonstrated.

Without `--dataset`, the command uses previously exercised held-out examples as
a **pilot**, explicitly labeled as reused data. It does not call them an untouched
final set. A separate dataset file has this form:

```json
{
  "dataset_id": "my-final-ticket-set-v1",
  "split": "final",
  "provenance": "Describe independent collection and prior exposure here",
  "cases": [{"case_id": "case-001", "ticket": "Actual support question"}]
}
```

The final label is a caller-supplied assertion, not evidence that nobody has seen
the examples. Reference answers are excluded from execution inputs. A forecast
file maps each case ID to a list of schema-3 workload-bound size forecasts, with
an explicit empty list when forecasts are absent. Original issue/expiry times
stay intact; the runner does not refresh or rebase forecasts to make them pass.

## Strategies and fairness

The default arms include an immediate fixed baseline for every configured
model/destination pair, joint speed/balanced/eco policies, model-only routing on
the first configured destination, a no-time-shifting ablation, and a fixed-model
destination/time ablation. With only one destination, those restrictions can
collapse to equivalent choices; they do not prove geographic shifting.

Supply `--arms path/to/arms.json` for an explicit list. Each arm includes
`arm_id`, `kind` (`fixed` or `planned`), `policy`, `custom_weights`,
`allowed_pairs`, `allow_summary_deferral`, `start_offsets_seconds`, and
`selection_source`. `allowed_pairs` maps each of classify/retrieve/draft/summarize
to a nonempty list of exact `{deployment_id, model_id}` pairs. Fixed arms require
one pair per step, policy `fixed`, no custom weights, no deferral and offsets
`[0]`. This supports both single-model and frozen per-step baselines.

`--calibrated-fixed-metric latency|cost|energy` adds a baseline selected from
review-covered training medians under the same quality floor. It uses exact
production settings and no evaluation outputs. The selector and source profile
fingerprint are retained and recomputed during validation. Unknown or inadequate
quality prevents this selection; arbitrary fixed assignments cannot masquerade
as that calibration-selected baseline.

All strategies share actual ticket/KB content, response/final budgets, output
limits, disabled thinking, provider timeout, accounting inputs and quality floor.
The common disabled-thinking setting currently targets the native Ollama path;
cloud adapters that cannot honor it retain a preflight failure. Planned pair
restrictions filter candidates before dependency-ready start closure. Disabling
summary deferral requires the earliest available slot; merely supplying a zero
offset is not used as a substitute for disabling time shifting.

Trials run serially in seeded randomized case/repeat blocks, with randomized
arm order within each block. Every case/repeat retains all arms. Arrival times
and real carbon conditions can differ: these are nonsimultaneous paired trials,
not simultaneous causal counterfactuals. Model residency, cold/warm state and
unrelated host load are uncontrolled and unobserved by this runner; no warm-up
exclusion or idle subtraction is implied. The recorded timestamps and source
scope remain available for interpretation. Dedicated controlled live studies
are still required for final measurement claims.

## Durable execution and interruption

The evaluation journal is updated atomically. An OS lock permits one runner per
evaluation directory. Each trial owns `queues/TRIAL_ID.sqlite3`; global queue
recovery or replanning in a later trial cannot alter earlier exported outcomes.
The frozen KB is passed to both worker paths. Deterministic workflow IDs let a
restart adopt a committed submission even if its journal update was interrupted.

Completed steps are not regenerated. A submitted trial requires its original
queue; missing storage is not permission to repeat inference. The resumed clock
must follow retained evidence. A crash or returned failure with uncertain
provider completion stops the serial experiment and retains remaining pending
trials. It cannot silently overlap another trial or automatically retry the
ambiguous call. Inspect and resolve the underlying provider/queue state before
designing further trials; frozen exports are not rewritten by this runner.

Optional accounting failures preserve raw output, cancel remaining undispatched
steps of that trial, and retain metric gaps. Infeasibility at submission is a
finished experimental outcome rather than an invitation to keep probing until a
favorable result appears. Existing worker behavior outside evaluation remains
available for operational replanning and explicit retries.

## Reports and portable replay

```powershell
python -m workflow.evaluate_policies export --evaluation .runtime/evaluations/local-pilot-v1 --output results/benchmarks/my-new-comparison
python -m workflow.evaluate_policies inspect --bundle results/benchmarks/my-new-comparison
```

Export copies the frozen protocol/journal/source and each finished queue bundle,
then writes the computed comparison and checksum manifest. It excludes runtime
databases and refuses an existing output directory. Prepared and interrupted
evaluations can be exported with their unfinished coverage intact. Portable
packages are immutable inspection artifacts; run the original evaluation
directory when resuming work.

Offline loading verifies checksums and supported paths/shapes, reconstructs queue
semantics, binds every trial to the exact protocol and serial schedule, checks
timestamps, and recomputes the report. Rehashed incompatible inputs and unrelated
workflows are rejected. Hashes establish content consistency, not reviewer or
provider authenticity. Report/export/inspect import no worker transports, make
no network calls, and never wait for historical delays.

Reports include all outcome denominators, response/final time, deadline success,
attempt/accounting coverage, per-trial evidence, resource subtotals/provenance and
paired baseline comparisons. Means, sample deviations and interpolated p50/p90
are descriptive. Absolute deltas are treatment minus baseline. Percentage
reductions require a positive known baseline; zero local API fees have no
percentage denominator. Missing resources, uncertain calls, and incompatible CO2
and CO2e scopes stay unavailable for complete comparisons. Failed workflows do
not disappear into successful-pair averages.

New protocols freeze an end-to-end rubric and review policy. Export blank packets,
validate/import attributable human approvals, and retain quality coverage and
paired score differences using [workflow quality review](workflow-quality-review.md).
Actual quality stays unknown until reviewed. Per-step profile quality is a
scheduling proxy. Release eligibility remains false until outstanding release
validation is complete. Real judgments and final live trials remain open work.
Positive, zero and negative observations are all valid outcomes.

## Retained implementation check

A local real-input pilot was frozen with eight arms, six reused held-out tickets
and one repeat (48 scheduled trials). No execution queue or inference was created.
Its prepared comparison validates offline and reports zero completed trials,
unknown quality/resources and no release evidence. See the current progress log
for the retained paths. Synthetic execution and failure tests are explicitly
labeled `simulation` in protocol, durable queue configuration and planning
reports. They require injected providers/health observations; live evaluation
rejects those fixtures.
