# Joint workflow planning

`scheduler.joint` selects model, deployment and absolute UTC start together for a small DAG. It is a pure planner: it does not call providers, move running work, estimate semantic quality or execute delayed jobs. The durable worker currently exposes a separate fixed-assignment path. Joint planning is not evidence of live routing to two locations.

## APIs

```python
from scheduler.joint import plan_workflow, replan_workflow

result = plan_workflow(
    request, candidates_by_step,
    {"latency": .2, "accuracy": .2, "cost": .2, "energy": .2, "carbon": .2},
    now=reference_utc,
    registry=registry,
    health=health_by_deployment,
    quality_floor=.8,
    search_limit=100_000,
)
```

Inputs are schema-3 `WorkflowRequest` and per-step lists of `PlannedCandidate`. Each candidate identifies a configured model/deployment pair and an absolute start. Its latency must include the documented expected transfer/load/inference scope; the planner invents no overhead. All five metrics must be known, including zero-weight metrics, to keep the eligible comparison set common across policies. Unknown quality remains infeasible even when the quality floor is zero.

The planner checks dependencies, earliest starts, step/response/completion deadlines, deployment/model support, configured region consistency, timestamped health when provided, capacity and existing `Reservation` intervals. Nondeferrable steps start as soon as dependencies and capacity permit. Passing no registry or health is an explicit caller eligibility responsibility; the CLI below requires either saved inventory or a declared simulation assumption.

The objective uses total USD cost, kWh energy, carbon mass and workflow arrival-to-completion latency. Parallel durations are not summed. Accuracy is the mean of per-step quality estimates, a scheduling proxy rather than measured final-output quality. CO2-only and CO2-equivalent masses cannot be pooled.

`PlanResult` retains a winner or explicit infeasibility, candidate/constraint rejections, search counts, normalized metrics, weighted contributions, a normalization reference and planner wall time. Weights must cover the five metrics, be finite and nonnegative, and have a positive finite total. Policy and workflow run ID do not change the matched normalization reference; workload, constraints, candidates, eligibility, calibration and time do. Comparisons require the same reference.

Search enumerates a deterministic finite Cartesian product, bounded by `search_limit`. A truncated result makes no global optimality claim, and its normalization covers only the examined prefix. Even an exhaustive result is optimal only within the supplied finite candidates, not continuous time or arbitrary infrastructure.

`replan_workflow` accepts `CommittedAssignment` records for dispatched/completed work. Selected identities and actual occupancy are frozen; the caller must supply an honest expected finish for running work. Actual overruns reduce the remaining deadline budget. A normalized-score improvement threshold can retain a still-feasible incumbent. Resource/quality values for committed assignments remain their recorded forecasts unless observed facts are supplied separately; they are not relabeled as measurements.

Planner contracts support strict schema-3 JSON persistence through `workflow.scheduling_records.save_record` / `load_record`. Persistence validates nested times, metric aggregates, weights, score explanations, count/truncation invariants and carbon scope before accepting evidence.

## Profile preparation and offline scenario CLI

`scheduler.profile_candidates.generate_profile_candidates` matches materialized prompt size, task, exact deployment fingerprint, model, workload version and generation settings to frozen empirical profiles. Unsupported profiles remain unknown. Carbon is recomputed from the candidate's energy estimate and an issued, valid sample covering its predicted interval; historical trial carbon mass is never reused for a new start.

The CLI materializes production prompts from a versioned ticket and **frozen calibration fixture context**. Its downstream prompts are not outputs from an executed workflow. The report remains labeled `frozen-context-planning-scenario` / `simulation`, including when its latency and energy profiles came from real local trials. It closes the supplied finite start grid with exact prerequisite finish estimates, including fractional seconds, rather than shifting a candidate after carbon estimation.

Run from the repository root with the tested environment:

```powershell
.\.venv-a2\Scripts\python.exe -m scheduler.plan_profiles --profiles results/benchmarks/m3-local-frozen-20261019/profiles.json --config config/deployments.local.json --case cal-account-01 --carbon-estimate config/carbon/india-cea-v22-static.json --reference-utc 2026-10-19T14:00:00Z --start-offset-seconds 0,60,120 --scenario-eligibility --output results/scratch/m4-frozen-profile-plan-20261019.json
```

`--scenario-eligibility` explicitly assumes configured models are available for this simulation. To use saved observed availability instead, replace that flag with `--health-inventory path/to/inventory.json`. The inventory must match the registry; missing, stale, future or negative observations remain ineligible. This CLI never probes endpoints or dispatches inference. A timestamped trace can replace the static carbon file using `--carbon-trace`.

The report retains profile/calibration, registry, workload, KB, case, frozen-context and carbon fingerprints; health and carbon snapshots; actual rendered prompt metadata; every generated candidate/diagnostic; and the typed planning result. An infeasible report exits successfully because infeasibility is a valid result. Invalid input exits with code 2 and does not replace a previous report.

## Verified result and limits

The command above was exercised on 2026-10-19 against the frozen local profiles with fingerprint `aa0229fb699669e032055fb8e3ab3de4af5aa7f69cd903f611bb74eb2e0cb8c3`. It generated 36 candidate options across four steps and returned **infeasible**: all 36 lacked reviewed semantic quality, leaving four empty feasible step pools. Latency, cost, energy and derived carbon were present. Carbon retained its configured CO2-only `gCO2` scope and estimated provenance. No network calls or inference were performed.

Local report: `results/scratch/m4-frozen-profile-plan-20261019.json` (ignored transient evidence). The configured static annual carbon proxy does not establish time-varying local-grid savings. Current semantic-quality gaps, unresolved second execution destination, live joint dispatch and policy evaluation remain release gates.

Focused validation:

```powershell
$env:PYTHON_DOTENV_DISABLED='1'
.\.venv-a2\Scripts\python.exe -m pytest -q tests/test_joint_planner.py tests/test_plan_profiles.py tests/test_profile_candidates.py tests/test_profiles.py tests/test_planned_execution.py
```

Coverage includes independent simulated model/location/time shifts; urgent and deferrable work; dependency, deadline and capacity failures; parallel critical paths; unknown profiles and quality; carbon scope/freshness; deterministic ties; bounded search; an independent exhaustive oracle; frozen-work replanning; malformed persisted evidence; and an offline CLI that fails if it attempts HTTP or inference. These controlled fixtures establish behavior, not live measured savings.
