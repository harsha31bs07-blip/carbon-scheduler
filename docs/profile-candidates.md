# Profile-backed candidate generation

`scheduler.profile_candidates.generate_profile_candidates` bridges the M3 empirical
profiles and the M4 finite planner. It accepts one **materialized** `StepRequest`,
the workload version, a `DeploymentRegistry`, the profile document, and an explicit
list or tuple of timezone-aware start timestamps. Pass `as_of` as the information
cutoff and `now` as the planning time; `as_of > now` is rejected.

```python
from scheduler.profile_candidates import generate_profile_candidates

generated = generate_profile_candidates(
    rendered_step, workload_version, registry, profiles, start_grid,
    as_of=reference_time, now=reference_time, carbon_provider=carbon_trace,
)
candidate_pool = generated.candidates
audit = generated.to_dict()
```

The result contains typed `PlannedCandidate` values and a diagnostic for every
configured deployment/model/start combination. Diagnostics distinguish excluded
start/deadline windows from retained candidates with unknown metrics. They retain
the profile source, carbon sample, unavailable-carbon reason and availability
source. Registry support is configuration evidence only; supply timestamped health
observations to `plan_workflow` and revalidate before worker dispatch.

Profiles must match the exact deployment configuration fingerprint, canonical
model, step ID, task type, workload version, output limit and thinking setting.
The generator recomputes the actual rendered prompt's UTF-8 byte size and requires
observed size coverage. It does not substitute provider token counts, extrapolate
outside the observed range or replace unknown semantic quality with a format
score. The planner consequently returns explicit infeasibility when required
metrics are unknown, even when a policy assigns those metrics zero weight.

Carbon mass is recomputed for each start from estimated energy in kWh and the
deployment's explicitly configured grid sample. The sample must have been issued
by `as_of`, cover the start, and remain valid through the complete predicted
duration. The feed adapter owns its documented freshness policy. An interval
crossing a sample boundary stays unknown; this implementation does not assume
how inference energy is distributed across multiple intensity windows. Existing
historical carbon mass in a profile is never reused for a new start. CO2 and
CO2-equivalent units remain distinct, and simulated inputs remain labeled.

The generator processes one step and cannot infer predecessor completion times.
The DAG caller must add exact predicted dependency-ready starts to its finite
grid before generating dependent candidates. Moving an existing candidate to a
different time would invalidate its carbon sample and requires regeneration.

Generation performs no inference or health probes and does not establish that a
configured geographic destination was actually used. Tests use synthetic profile
and carbon fixtures; their numeric trade-offs are not measured savings.
