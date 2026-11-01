# Calibration and measurement methodology

The connected workflow uses explicit deployment/model pairs and schema-3
evidence. Legacy model tables and regional curves remain scenario assumptions;
they are not automatically promoted into calibrated profiles.

## Per-task pilot

`python -m workflow.calibrate` uses the production prompt builder, KB version,
generation limits, thinking settings and deployment adapter. Each task receives
explicitly frozen, validated dependency context assembled from calibration
fixtures. This prevents upstream model failures from quietly removing harder
downstream trials. It is not a claim that the model generated that context or
that the complete workflow succeeded.

The initial protocol requests one warm-up and ten trials per model/task profile.
Cases cycle through the calibration split. Order, actual settings, warm-up flag,
model residency observations and provider-reported load duration are retained.
Warm-up and held-out records cannot train the profile. Ten trials are a pilot,
not evidence for stable tail latency, population quality or energy savings.

Every trial is written before dispatch and again immediately after the adapter
returns, before optional telemetry/cost/carbon enrichment. Failures, invalid
format, truncation, outage periods and interrupted trials remain visible.
Accounting failure cannot repeat inference or discard already returned output
and usage. Calibration makes one provider attempt per trial.

Each run retains a manifest, hardware capability snapshot, exact source snapshot
and source hashes. Ordinary code revision labels include dirty-tree status;
source snapshots establish the actual experiment implementation. Hardware power
queries are point-in-time capability observations, not energy measurements.

## Timing and workload size

Latency is measured at the explicit adapter boundary and includes network and
model loading. Provider-reported load, total, prefill and decode durations are
retained separately when available; they are not independently verified TTFT.
Trial wall time additionally includes telemetry setup/cleanup. Do not compare
one scope with the other as if they were identical.

Prompt size uses the labeled UTF-8 byte proxy from the actual rendered context.
Actual provider input/output tokens remain separate. Profiles are keyed to the
exact deployment fingerprint, model, task, prompt/KB version, output limit,
thinking setting, temperature and carbon scope. Estimates outside observed
configurations or size ranges remain explicitly unknown by default.

Valid natural completions support descriptive median, sample standard deviation
and interpolated p90. Failures/truncations remain in reliability counts. A simple
nonnegative input-size latency fit is used only with enough distinct sizes;
otherwise the descriptive median is used. Cold/warm observations stay separate.
Held-out error reports use frozen training profiles, retain range/configuration
exclusions and report sample counts rather than silently extrapolating.

## Quality

Classification format, citations, nonempty output and natural completion are
validation checks. Passing them does not establish semantic quality. The M2 live
draft demonstrated this by implying an account-status review and suggesting
irrelevant MFA guidance despite valid citations.

Profile quality remains unknown until explicit rubric review. Keyword/format
heuristics from the legacy 16-item evaluation are estimated proxies, even when
the responses came from live inference. They must not become measured accuracy.
Calibration and held-out tickets, rubrics and outputs remain separate. A planner
requiring known quality must report infeasibility while that evidence is absent.

`workflow.review_quality` exports exact retained outputs, source fixtures and
blank human-review templates. `workflow.profile_report --reviews` validates
approved judgments against full trial/output/rubric hashes and joins copies
without editing raw trials. Each criterion needs a score and rationale; reviewer
and completion-consistent timestamps are required. The aggregate embeds review
evidence, coverage and a checksum, and its fitted identity includes review
content. No existing unreviewed output is promoted by running this tooling.
Reviewed aggregates freeze a versioned pilot coverage policy: at least ten
reviews across five distinct cases and complete review of valid natural
completions for each profile. Insufficient coverage preserves scores but leaves
scheduling quality unknown. This gate is separate from the semantic quality
floor and is not a claim of population accuracy. See
[the review guide](../docs/quality-review.md) for commands and current limits.

## Energy

Telemetry is optional and restricted to literal loopback endpoints. Measuring
the coordinator cannot measure a remote inference host. One session covers one
trial; nested sessions are declined, and cleanup runs even on exceptions.
Telemetry failures do not trigger inference retries.

CodeCarbon can mix Windows EMI/RAPL CPU readings with modeled RAM and unverified
GPU sensor/model composition. The total therefore remains mixed or estimated;
installing a tracker does not make it wholly measured. Component values, sources,
scope and telemetry errors are retained. The scope includes the Ollama service,
idle energy, orchestration and unrelated host processes. It is not a per-process
GPU measurement. No idle subtraction or lifecycle allocation is implied.

Use `--energy codecarbon --energy-country IND` for offline tracker setup on this
user-confirmed Indian host. CodeCarbon's own carbon output is not used. The
legacy client's optional 48W fallback remains an uncalibrated power assumption;
the new calibration runner does not silently substitute it.

## Cost and carbon

API fees derive from actual reported token usage and an explicit exact-model
tariff with validity and source. Missing usage makes nonzero-rate cost unknown.
An explicit zero local API tariff covers API fees only, excluding electricity,
hardware and ownership costs. Unobserved retry usage cannot become zero.

The user located this host in Mangaluru, Karnataka, India. [SRPC's constitution](https://srpc.gov.in/en/constitution/)
places Karnataka in the Southern Region. The local configuration uses region
`IN-KA` and the Indian national-grid identifier `IN`; generic provider URLs do
not establish this location for other deployments.

[CEA's Version 22.0 guide](https://cea.nic.in/api/cdm-report-version/view-pdf/en/82),
published in August 2026, reports an all-India FY2025-26 weighted annual factor of
0.675 tCO2/MWh, or 675 gCO2/kWh, including renewables, captive injection and
cross-border transfers. This is CO2 only, not CO2-equivalent, hourly carbon or a
Karnataka-specific intensity. The report publication month, historical data
period and derived application-estimate creation time remain distinct.

`config/carbon/india-cea-v22-static.json` is an explicitly configured current-use
historical proxy with bounded validity/freshness policy. It is not a forecast or
a statement that the original observation covers those dates. Its source
metadata is stored alongside it. Factual numeric attribution is retained; the
CEA report is not bundled and no open-data licence is asserted. See [CEA's
source policy](https://cea.nic.in/api/websitepolicy/pdf/view/en/2).

Carbon calculation multiplies energy by an intensity sample covering the trial,
preserving estimated/mixed/simulated inputs. Missing energy, region, fresh sample
or full execution-time coverage leaves carbon unknown. CO2 and CO2-equivalent
units remain distinct in records and profiles and cannot be pooled. Synthetic
curves remain simulation inputs; they cannot become live measured savings.

## Local-only boundary

The user selected local development for now. These trials do not establish a
second physical destination or geographic shifting. A fixed annual factor also
cannot establish a carbon benefit from delaying a job. Later controlled timing
tests can verify queue mechanics, while geographic/time savings require suitable
real destinations and timestamped carbon evidence.

## Matched workflow evaluation

`workflow.evaluate_policies` freezes common dataset/KB/settings/constraints,
profiles, tariff/carbon inputs, candidate-pair restrictions, seed and exact source
before inference. Trials use actual output chaining through the durable worker,
serial randomized paired blocks and separate per-trial queues. Fixed single-pair,
calibration-selected per-step, model-only, joint and dimension-ablation strategies
have explicit retained choices. Missing quality/size/freshness stays infeasible.

Reports retain failures and unfinished denominators, coverage/provenance, response
and final clocks, zero-baseline handling and incompatible carbon scopes. Portable
comparisons recompute offline from protocol-bound queue bundles. Current default
examples are a reused pilot, not untouched final data; model residency and host
conditions remain uncontrolled/unobserved. Actual reviewed end-to-end quality,
independent final measurement and second-destination evidence remain required.
See [policy evaluation](../docs/policy-evaluation.md).
