"""Offline executed-workflow totals with explicit gaps and separate clocks.

Resource totals include every returned attempt, including failed and superseded
calls. Duration sums describe work, never end-to-end latency or a critical path.
"""

from datetime import datetime
import math

from data.quality_reviews import fingerprint
from shared.scheduling import MetricEstimate


def _sum(values):
    try:
        result = math.fsum(values)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _resource(metrics, name, *, gaps):
    rows = [item[name] for item in metrics]
    known = [item for item in rows if item["value"] is not None]
    units = sorted({item["unit"] for item in known})
    subtotals = {unit: _sum(item["value"] for item in known if item["unit"] == unit) for unit in units}
    complete = bool(rows) and len(known) == len(rows) and not gaps and len(units) == 1
    total = subtotals[units[0]] if complete else None
    complete = complete and total is not None
    return {"total": total, "unit": units[0] if len(units) == 1 else None,
            "known_subtotals": subtotals, "complete": complete,
            "known_attempts": len(known), "unknown_attempts": len(rows) - len(known),
            "provenance_counts": {label: sum(item["provenance"] == label for item in rows)
                                  for label in sorted({item["provenance"] for item in rows})}}


def execution_report(snapshot):
    """Summarize validated queue/replay evidence without providers or clocks."""
    indexed = {record["attempt_id"]: record for record in snapshot.accounting}
    attempts = snapshot.attempts
    jobs = {job.step_request.step_id: job for job in snapshot.jobs}
    missing = [attempt.attempt_id for attempt in attempts if attempt.attempt_id not in indexed]
    retry_events = {step.step_id: [event for event in snapshot.events
        if event["step_id"] == step.step_id and event["kind"] in {
            "retry_known_not_dispatched", "retry_acknowledged_possible_duplicate_usage"}]
        for step in snapshot.request.steps}
    unrepresented = []
    confirmed_undispatched = []
    for job in snapshot.jobs:
        for retry in range(job.retry_index + 1):
            represented = any(attempt.planned_candidate.step_id == job.step_request.step_id
                              and attempt.retry_index == retry for attempt in attempts)
            events = retry_events[job.step_request.step_id]
            if not represented and retry < len(events) and events[retry]["kind"] == "retry_known_not_dispatched":
                confirmed_undispatched.append({"step_id": job.step_request.step_id, "retry_index": retry})
                continue
            if not represented and (retry < job.retry_index or job.dispatched_at is not None):
                unrepresented.append({"step_id": job.step_request.step_id, "retry_index": retry})
    gaps = bool(unrepresented)
    uncertain = [attempt.attempt_id for attempt in attempts if attempt.status == "needs-reconciliation"
                 or attempt.actual_start is not None and attempt.status in {"failed", "cancelled"}]
    windows = [(record["attempt_id"], datetime.fromisoformat(record["measurement_start"]),
                datetime.fromisoformat(record["measurement_finish"])) for record in snapshot.accounting
               if record["metrics"]["energy"]["value"] is not None]
    overlap = [{"attempt_ids": [left[0], right[0]]} for index, left in enumerate(windows)
               for right in windows[index + 1:] if max(left[1], right[1]) < min(left[2], right[2])]
    metrics, detail = [], []
    for attempt in attempts:
        record = indexed.get(attempt.attempt_id)
        if record is None:
            observed = attempt.observations
            unknown = lambda unit: MetricEstimate(None, unit, "unknown").to_dict()
            values = {name: unknown(unit) for name, unit in
                      (("latency", "s"), ("accuracy", "ratio"), ("cost", "USD"),
                       ("energy", "kWh"), ("carbon", "gCO2e"))}
            if observed is not None:
                values["latency"] = observed.latency.to_dict()
        else:
            values = record["metrics"]
        metrics.append(values)
        step = next(step for step in snapshot.request.steps if step.step_id == attempt.planned_candidate.step_id)
        dependencies = [jobs[dep].finished_at for dep in step.dependencies if dep in jobs]
        ready = max([snapshot.request.arrived_at] + [time for time in dependencies if time is not None])
        start = attempt.actual_start
        deliberate = max(0., (attempt.planned_candidate.planned_start - ready).total_seconds()) if start else None
        residual = max(0., (start - max(ready, attempt.planned_candidate.planned_start)).total_seconds()) if start else None
        detail.append({"attempt_id": attempt.attempt_id, "step_id": step.step_id,
            "retry_index": attempt.retry_index, "status": attempt.status,
            "accounting_present": record is not None, "planned_estimates": attempt.planned_candidate.estimates.to_dict(),
            "observations": values, "provider_seconds": attempt.provider_seconds,
            "provider_wall_seconds": (attempt.actual_finish - start).total_seconds()
                if start is not None and attempt.actual_finish is not None else None,
            "dependency_ready_at": ready.isoformat(), "wait_seconds": attempt.wait_seconds,
            "dependency_wait_seconds": max(0., (ready - snapshot.request.arrived_at).total_seconds()),
            "deliberate_delay_seconds": deliberate, "queue_and_preparation_wait_seconds": residual,
            "journal_seconds_after_provider": (datetime.fromisoformat(record["journal_recorded_at"])
                - attempt.actual_finish).total_seconds() if record and attempt.actual_finish else None,
            "measurement_seconds": (datetime.fromisoformat(record["measurement_finish"])
                - datetime.fromisoformat(record["measurement_start"])).total_seconds() if record else None,
            "energy_scope": record["energy_report"]["energy_scope"] if record else "unknown",
            "accounting_errors": record["errors"] if record else ["accounting-missing"]})
    required = [step.step_id for step in snapshot.request.steps if step.required_for_response]
    def finished(ids):
        if ids and all(identity in jobs and jobs[identity].status == "completed"
                       and jobs[identity].finished_at is not None for identity in ids):
            return max(jobs[identity].finished_at for identity in ids)
        return None
    response = finished(required)
    final = finished([step.step_id for step in snapshot.request.steps])
    terminal = snapshot.status in {"completed", "failed", "cancelled", "deadline_missed", "infeasible", "planning-failed"}
    preparation = [decision["report"].get("preparation_seconds") for decision in snapshot.decisions]
    probes = [decision["report"].get("health_probe_seconds") for decision in snapshot.decisions]
    report = {"schema_version": 1, "record_type": "ExecutionAccountingReport",
        "workflow_id": snapshot.request.workflow_id, "status": snapshot.status,
        "coverage": {"attempt_count": len(attempts), "accounted_attempts": len(indexed),
                     "missing_attempt_ids": missing, "unrepresented_retry_indices": unrepresented,
                     "confirmed_undispatched_retries": confirmed_undispatched,
                     "uncertain_provider_attempt_ids": uncertain, "overlapping_host_windows": overlap,
                     "context_present": snapshot.accounting_context is not None},
        "resources": {name: _resource(metrics, name, gaps=gaps or name != "cost" and bool(uncertain or overlap))
                      for name in ("cost", "energy", "carbon")},
        "quality": {"value": None, "provenance": "unknown", "reason": "end-to-end-output-review-required"},
        "timing": {"response_ready_at": response.isoformat() if response else None,
            "response_seconds": (response - snapshot.request.arrived_at).total_seconds() if response else None,
            "response_deadline_met": response <= snapshot.request.response_deadline if response else False if terminal else None,
            "completed_at": final.isoformat() if final else None,
            "completion_seconds": (final - snapshot.request.arrived_at).total_seconds() if final else None,
            "completion_deadline_met": final <= snapshot.request.completion_deadline if final else False if terminal else None,
            "planner_preparation_seconds": _sum(value for value in preparation if value is not None) if preparation else None,
            "planner_preparation_records": sum(value is not None for value in preparation),
            "health_probe_seconds": _sum(probes) if probes and all(value is not None for value in probes) else None,
            "health_probe_records": sum(value is not None for value in probes),
            "decision_count": len(snapshot.decisions)}, "attempts": detail,
        "notes": ["Per-attempt durations overlap end-to-end latency and must not be added to it.",
                  "API fees exclude electricity and machine ownership; host energy includes idle and other processes.",
                  "Missing retry evidence may represent pre-dispatch failure or an unreturned provider call.",
                  "Failed or cancelled dispatched calls may continue beyond the observed host window.",
                  "Overlapping host-window subtotals can double count shared energy; they have no complete total.",
                  "CO2-only and CO2e subtotals are separate; incompatible scopes have no pooled total."]}
    report["fingerprint"] = fingerprint(report)
    return report
