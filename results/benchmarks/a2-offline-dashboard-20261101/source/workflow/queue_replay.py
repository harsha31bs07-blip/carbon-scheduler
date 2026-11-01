"""Export and inspect one durable workflow's stored evidence, entirely offline.

Replay validates evidence; it neither dispatches inference nor waits for its
original scheduled times. It does not import worker or provider transports and
does not turn a fixed queue run into a policy comparison.
"""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from shared.scheduling import ExecutionAttempt, PlannedCandidate, SCHEMA_VERSION, StepRequest, WorkflowRequest
from workflow.queue_store import (
    QUEUE_VERSION, STATUSES, QueueJob, QueueStore, WorkflowSnapshot,
    _configuration, _expected_model, _load, _text, _undispatched_failure,
)


BUNDLE_VERSION = 1
PLANNED_BUNDLE_VERSION = 2
ACCOUNTING_BUNDLE_VERSION = 3
_ENVELOPE_FIELDS = {"schema_version", "bundle_version", "record_type", "request", "configuration", "status",
                    "jobs", "attempts", "cancelled_at", "plan_history", "events"}
_PLANNED_ENVELOPE_FIELDS = _ENVELOPE_FIELDS | {"planning_context", "decisions"}
_ACCOUNTING_ENVELOPE_FIELDS = _PLANNED_ENVELOPE_FIELDS | {"accounting_context", "accounting"}
_MANIFEST_FIELDS = {"schema_version", "bundle_version", "record_type", "workflow_id", "files"}
_EVENT_KINDS = {"cancelled", "retry_acknowledged_possible_duplicate_usage", "retry_known_not_dispatched",
                "archived_late_superseded_attempt", "released_undispatched"}


def _timestamp(value):
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _time(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Replay timestamps must be timezone-aware ISO-8601 strings")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Replay timestamp is not valid ISO-8601") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Replay timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def _version(value, record_type):
    if (type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION
            or type(value.get("bundle_version")) is not int
            or value["bundle_version"] not in {BUNDLE_VERSION, PLANNED_BUNDLE_VERSION, ACCOUNTING_BUNDLE_VERSION}
            or value.get("record_type") != record_type):
        raise ValueError("Unsupported durable replay record/schema/bundle version")
    return value["bundle_version"]


def _job_dict(job):
    values = {field.name: getattr(job, field.name) for field in fields(QueueJob)}
    for name in ("step_request", "candidate", "rendered_step"):
        values[name] = values[name].to_dict() if values[name] is not None else None
    for name in ("dispatched_at", "finished_at"):
        values[name] = _timestamp(values[name])
    values["source_ids"] = list(values["source_ids"])
    return values


def _snapshot_dict(snapshot):
    context = getattr(snapshot, "planning_context", None)
    decisions = getattr(snapshot, "decisions", ())
    accounting_context = getattr(snapshot, "accounting_context", None)
    accounting = getattr(snapshot, "accounting", ())
    if context is None and (decisions or snapshot.configuration.get("worker_version") == "sqlite-planned-worker-v1"):
        raise ValueError("Planned replay cannot omit its planning evidence")
    version = (ACCOUNTING_BUNDLE_VERSION if accounting_context is not None or accounting
               else PLANNED_BUNDLE_VERSION if context is not None else BUNDLE_VERSION)
    payload = {"schema_version": SCHEMA_VERSION, "bundle_version": version,
        "record_type": "DurableWorkflowReplay", "request": snapshot.request.to_dict(),
        "configuration": snapshot.configuration, "status": snapshot.status,
        "jobs": [_job_dict(job) for job in snapshot.jobs],
        "attempts": [attempt.to_dict() for attempt in snapshot.attempts],
        "cancelled_at": _timestamp(snapshot.cancelled_at),
        "plan_history": [candidate.to_dict() for candidate in snapshot.plan_history],
        "events": [{**event, "recorded_at": _timestamp(event["recorded_at"])} for event in snapshot.events]}
    if context is not None or version == ACCOUNTING_BUNDLE_VERSION:
        payload.update(planning_context=context, decisions=list(decisions))
    if version == ACCOUNTING_BUNDLE_VERSION:
        payload.update(accounting_context=accounting_context, accounting=list(accounting))
    return payload


def _accounting_evidence(value, request, configuration, attempts, jobs):
    """Validate append-only enrichment while retaining explicit missing records."""
    from shared.deployments import DeploymentRegistry
    from workflow.execution_accounting import validate_accounting_context, validate_accounting_record

    if "registry_snapshot" not in configuration:
        raise ValueError("Accounting replay requires its deployment registry snapshot")
    registry = DeploymentRegistry.from_dict(configuration["registry_snapshot"])
    context = validate_accounting_context(value["accounting_context"], request, registry)
    if not isinstance(value["accounting"], list):
        raise ValueError("Replay accounting records must be an array")
    indexed = {attempt.attempt_id: (index, attempt) for index, attempt in enumerate(attempts)}
    records, previous_index = [], -1
    for supplied in value["accounting"]:
        if not isinstance(supplied, dict) or supplied.get("attempt_id") not in indexed:
            raise ValueError("Replay accounting refers to an unknown raw attempt")
        index, attempt = indexed[supplied["attempt_id"]]
        if index <= previous_index:
            raise ValueError("Replay accounting must follow unique raw attempt order")
        record = validate_accounting_record(supplied, attempt, registry, context)
        journal = _time(record["journal_recorded_at"])
        if journal < request.arrived_at:
            raise ValueError("Replay accounting journal precedes workflow arrival")
        for job in jobs:
            if (job.candidate == attempt.planned_candidate and job.retry_index == attempt.retry_index
                    and job.finished_at is not None and journal < job.finished_at):
                raise ValueError("Replay accounting journal precedes its persisted job outcome")
        records.append(record)
        previous_index = index
    return context, tuple(records)


def _planning_evidence(value, request, configuration):
    """Validate additive planning evidence without importing provider transports."""
    from data.quality_reviews import fingerprint
    from scheduler.joint import PlanResult
    from workflow.planning_records import validate_context, validate_report

    context = validate_context(value["planning_context"], request=request, configuration=configuration)
    supplied = value["decisions"]
    if not isinstance(supplied, list) or not supplied:
        raise ValueError("Planned replay requires decision history")
    decisions, winners, materialized = [], [], []
    last_time = request.arrived_at
    prior_winners = {}
    for index, decision in enumerate(supplied):
        if (not isinstance(decision, dict) or set(decision) != {"revision", "recorded_at", "report", "snapshot_token"}
                or type(decision["revision"]) is not int or decision["revision"] != index + 1):
            raise ValueError("Planning decision revisions must be complete and contiguous")
        token = decision["snapshot_token"]
        if (index == 0 and token is not None or index > 0 and
                (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token))):
            raise ValueError("Planning revision lacks its queue snapshot token")
        recorded = _time(decision["recorded_at"])
        report = validate_report(decision["report"], request=request, configuration=configuration, context=context)
        prepared = _time(report["now"])
        if recorded is None or prepared is None or recorded < last_time or not request.arrived_at <= prepared <= recorded:
            raise ValueError("Planning decision chronology is inconsistent")
        last_time = recorded
        if report["record_type"] == "PreparedWorkflowPlan":
            previous = report["previous_plan_fingerprint"]
            if previous is not None and previous not in prior_winners:
                raise ValueError("Planning decision refers to an unknown previous winner")
            result = PlanResult.from_dict(report["result"])
            winner = result.winner
            if winner is not None:
                if any(note.startswith("Retained feasible incumbent:") for note in result.notes):
                    previous_candidates = {item.candidate.step_id: item.candidate
                                           for item in prior_winners[previous].assignments}
                    if any(item.candidate != previous_candidates.get(item.candidate.step_id) for item in winner.assignments):
                        raise ValueError("Retained incumbent differs from its linked previous winner")
                winners.append(winner)
                prior_winners[fingerprint(winner.to_dict())] = winner
                contexts = {item["step_id"]: item for item in report["contexts"]}
                for assignment in winner.assignments:
                    step_context = contexts[assignment.candidate.step_id]
                    if step_context["input_mode"] == "materialized":
                        materialized.append((assignment.candidate, step_context, recorded))
        decisions.append({**decision, "report": report})
    return context, tuple(decisions), tuple(winners), tuple(materialized)


def _planning_status(decisions):
    report = decisions[-1]["report"]
    if report["record_type"] == "PlanningPreparationFailure":
        return "planning-failed"
    return "infeasible" if report["result"]["winner"] is None else None


def _candidate(candidate, step, configuration):
    if candidate.step_id != step.step_id or not step.earliest_start <= candidate.planned_start < step.latest_finish:
        raise ValueError("Replay candidate does not belong to its step/window")
    _expected_model(candidate, configuration)  # Also validates registry/model membership when supplied.
    if "registry_snapshot" in configuration:
        from shared.deployments import DeploymentRegistry
        deployment = DeploymentRegistry.from_dict(configuration["registry_snapshot"]).get(candidate.deployment_id)
        if candidate.region != deployment.region:
            raise ValueError("Replay candidate geography differs from configured deployment")


def _decode_snapshot(value):
    if not isinstance(value, dict):
        raise ValueError("Durable workflow replay requires exactly its supported fields")
    version = _version(value, "DurableWorkflowReplay")
    expected = {BUNDLE_VERSION: _ENVELOPE_FIELDS, PLANNED_BUNDLE_VERSION: _PLANNED_ENVELOPE_FIELDS,
                ACCOUNTING_BUNDLE_VERSION: _ACCOUNTING_ENVELOPE_FIELDS}[version]
    if set(value) != expected:
        raise ValueError("Durable workflow replay requires exactly its supported fields")
    request = WorkflowRequest.from_dict(value["request"])
    configuration = _configuration(value["configuration"])
    if configuration.get("accounting_version") == "execution-accounting.v1" and version != ACCOUNTING_BUNDLE_VERSION:
        raise ValueError("Replay cannot strip configured execution accounting evidence")
    if version == ACCOUNTING_BUNDLE_VERSION and configuration.get("accounting_version") != "execution-accounting.v1":
        raise ValueError("Accounting replay requires its execution accounting version marker")
    planning_context, decisions, winners, materialized = None, (), (), ()
    if version == PLANNED_BUNDLE_VERSION or version == ACCOUNTING_BUNDLE_VERSION and value["planning_context"] is not None:
        planning_context, decisions, winners, materialized = _planning_evidence(value, request, configuration)
    elif version == ACCOUNTING_BUNDLE_VERSION and value["decisions"] != []:
        raise ValueError("Unplanned accounting replay cannot contain planning decisions")
    elif configuration.get("worker_version") == "sqlite-planned-worker-v1":
        raise ValueError("Replay cannot strip planned workflow evidence")
    steps = {step.step_id: step for step in request.steps}
    for name in ("jobs", "attempts", "plan_history", "events"):
        if not isinstance(value[name], list):
            raise ValueError("Replay collections must be arrays")
    history = tuple(PlannedCandidate.from_dict(item) for item in value["plan_history"])
    for candidate in history:
        if candidate.step_id not in steps:
            raise ValueError("Replay history refers to another workflow step")
        _candidate(candidate, steps[candidate.step_id], configuration)
    jobs, seen = [], set()
    for item in value["jobs"]:
        if not isinstance(item, dict) or set(item) != {field.name for field in fields(QueueJob)}:
            raise ValueError("Replay job has unsupported/missing fields")
        original = StepRequest.from_dict(item["step_request"])
        if original.step_id not in steps or original != steps[original.step_id] or original.step_id in seen:
            raise ValueError("Replay job identity/request mismatch")
        seen.add(original.step_id)
        candidate = PlannedCandidate.from_dict(item["candidate"])
        _candidate(candidate, original, configuration)
        if candidate not in history:
            raise ValueError("Replay job has no corresponding plan history")
        if item["status"] not in STATUSES or type(item["retry_index"]) is not int or not 0 <= item["retry_index"] <= configuration.get("max_retries", 3):
            raise ValueError("Replay job has invalid status/retry index")
        for name in ("output", "error_code", "input_fingerprint", "validation_error"):
            if item[name] is not None and not isinstance(item[name], str):
                raise ValueError("Replay job text fields must be text or unknown")
        if item["output_valid"] is not None and type(item["output_valid"]) is not bool:
            raise ValueError("Replay output validity must be boolean or unknown")
        if item["validation_error"] is not None:
            _text(item["validation_error"], "validation_error")
            if item["output_valid"] is not False:
                raise ValueError("Replay validation error requires rejected output")
        rendered = StepRequest.from_dict(item["rendered_step"]) if item["rendered_step"] is not None else None
        QueueStore._render(original, rendered, item["source_ids"], item["input_fingerprint"], item["lookup_seconds"])
        dispatched, finished = _time(item["dispatched_at"]), _time(item["finished_at"])
        if dispatched is not None and (rendered is None or not candidate.planned_start <= dispatched < original.latest_finish):
            raise ValueError("Replay dispatch lacks a valid rendered request/start")
        if finished is not None and (finished < request.arrived_at or dispatched is not None and finished < dispatched):
            raise ValueError("Replay finish precedes workflow/dispatch")
        if item["status"] == "scheduled" and any(item[name] is not None for name in
                ("dispatched_at", "finished_at", "output", "output_valid", "validation_error", "error_code")):
            raise ValueError("Scheduled replay job contains terminal/dispatch facts")
        if item["status"] == "running" and (dispatched is None or any(item[name] is not None for name in
                ("finished_at", "output", "output_valid", "validation_error", "error_code"))):
            raise ValueError("Replay running state contains invalid execution facts")
        if item["status"] == "needs-reconciliation" and dispatched is None:
            raise ValueError("Ambiguous replay job must retain dispatch boundary")
        if item["status"] in {"completed", "failed", "deadline_missed"} and finished is None:
            raise ValueError("Terminal replay job lacks finish")
        if item["status"] == "completed" and (dispatched is None or not isinstance(item["output"], str) or not item["output"].strip()
                or item["output_valid"] is not True or item["error_code"] is not None or finished > original.latest_finish):
            raise ValueError("Completed replay job lacks accepted, on-time output")
        jobs.append(QueueJob(original, candidate, item["status"], item["retry_index"], item["output"],
            item["error_code"], dispatched, finished, rendered, tuple(item["source_ids"]), item["input_fingerprint"],
            item["lookup_seconds"], item["output_valid"], item["validation_error"]))
    empty_planned = planning_context is not None and not jobs and not winners
    if seen != set(steps) and not empty_planned:
        raise ValueError("Replay requires exactly one job per workflow step")
    if empty_planned and (history or value["attempts"]):
        raise ValueError("Unassigned planned replay cannot contain execution history")
    by_id = {job.step_request.step_id: job for job in jobs}
    attempts, attempt_ids = [], set()
    for item in value["attempts"]:
        attempt = ExecutionAttempt.from_dict(item)
        key = attempt.planned_candidate.step_id
        if (attempt.workflow_id != request.workflow_id or key not in by_id or attempt.attempt_id in attempt_ids
                or attempt.planned_candidate not in history or attempt.retry_index > by_id[key].retry_index
                or attempt.status not in {"completed", "failed", "cancelled", "needs-reconciliation"}):
            raise ValueError("Replay attempt identity/history mismatch")
        attempt_ids.add(attempt.attempt_id)
        original = steps[key]
        if ((attempt.output_limit_tokens is not None and attempt.output_limit_tokens != original.output_limit_tokens)
                or attempt.enable_thinking != original.enable_thinking):
            raise ValueError("Replay attempt generation controls differ from request")
        if not _undispatched_failure(attempt):
            if (attempt.executed_model_id != _expected_model(attempt.planned_candidate, configuration)
                    or attempt.executed_deployment_id != attempt.planned_candidate.deployment_id
                    or attempt.actual_start is None or attempt.actual_finish is None
                    or attempt.actual_start < attempt.planned_candidate.planned_start):
                raise ValueError("Replay attempt executed identity/timing mismatch")
        attempts.append(attempt)
    for job in jobs:
        current = [attempt for attempt in attempts if attempt.planned_candidate == job.candidate and attempt.retry_index == job.retry_index]
        for attempt in current:
            if attempt.actual_start is not None and (job.dispatched_at is None or attempt.actual_start < job.dispatched_at):
                raise ValueError("Replay attempt precedes its durable dispatch boundary")
            if attempt.actual_finish is not None and job.finished_at is not None and attempt.actual_finish > job.finished_at:
                raise ValueError("Replay attempt finishes after its persisted job outcome")
        if job.status == "completed" and not any(attempt.status == "completed" and attempt.output == job.output
                and attempt.actual_finish <= job.finished_at for attempt in current):
            raise ValueError("Accepted replay output has no matching persisted successful attempt")
        if job.status == "deadline_missed" and (job.error_code != "deadline_missed" or job.finished_at < job.step_request.latest_finish
                and not any(attempt.error == "step-deadline-exceeded" for attempt in current)):
            raise ValueError("Replay deadline miss lacks timing/attempt evidence")
        if job.dispatched_at is not None:
            for key in job.step_request.dependencies:
                dependency = by_id[key]
                if dependency.status != "completed" or dependency.finished_at > job.dispatched_at:
                    raise ValueError("Replay dispatch precedes accepted prerequisites")
        if job.output is not None and not any(attempt.output == job.output for attempt in current):
            raise ValueError("Replay job output has no corresponding attempt")
    if planning_context is not None:
        offered = tuple(item.candidate for winner in winners for item in winner.assignments)
        if any(candidate not in offered for candidate in history) or any(candidate not in history for candidate in offered):
            raise ValueError("Planned candidate history disagrees with retained winners")
        if jobs:
            if not winners:
                raise ValueError("Planned jobs require a retained feasible winner")
            latest = {item.candidate.step_id: item.candidate for item in winners[-1].assignments}
            if any(job.candidate != latest[job.step_request.step_id] for job in jobs):
                raise ValueError("Planned jobs disagree with the latest feasible decision")
        for job in jobs:
            if job.dispatched_at is None:
                continue
            matching = [(context, recorded) for candidate, context, recorded in materialized
                        if candidate == job.candidate and recorded <= job.dispatched_at]
            if not any(StepRequest.from_dict(context["materialized_step"]) == job.rendered_step
                       and context["input_fingerprint"] == job.input_fingerprint
                       and tuple(context["source_ids"]) == job.source_ids for context, _ in matching):
                raise ValueError("Planned dispatch has no matching materialized decision")
        for attempt in attempts:
            matching = [(context, recorded) for candidate, context, recorded in materialized
                        if candidate == attempt.planned_candidate
                        and (attempt.actual_start is None or recorded <= attempt.actual_start)]
            if not matching:
                raise ValueError("Planned attempt lacks a prior materialized decision")
    cancelled = _time(value["cancelled_at"])
    if cancelled is not None and cancelled < request.arrived_at:
        raise ValueError("Replay cancellation precedes workflow arrival")
    events = []
    for item in value["events"]:
        if not isinstance(item, dict) or set(item) != {"step_id", "kind", "recorded_at"}:
            raise ValueError("Replay event has unsupported fields")
        if item["kind"] not in _EVENT_KINDS or item["step_id"] is not None and item["step_id"] not in steps:
            raise ValueError("Replay event identity/type mismatch")
        if (item["kind"] == "cancelled") != (item["step_id"] is None):
            raise ValueError("Replay cancellation/retry event has invalid step scope")
        instant = _time(item["recorded_at"])
        if instant is None or instant < request.arrived_at:
            raise ValueError("Replay event has invalid time")
        events.append({**item, "recorded_at": instant})
    cancellations = [event["recorded_at"] for event in events if event["kind"] == "cancelled"]
    if empty_planned and any(event["kind"] != "cancelled" for event in events):
        raise ValueError("Unassigned planned replay cannot contain step retry/attempt events")
    if cancelled is not None and cancelled not in cancellations or cancelled is None and cancellations:
        raise ValueError("Replay cancellation state lacks matching event evidence")
    for job in jobs:
        retries = [event for event in events if event["step_id"] == job.step_request.step_id
                   and event["kind"] in {"retry_acknowledged_possible_duplicate_usage", "retry_known_not_dispatched"}]
        if len(retries) != job.retry_index:
            raise ValueError("Replay retry counter disagrees with retained retry events")
    statuses = {job.status for job in jobs}
    status = ("cancelled" if cancelled else "needs-reconciliation" if "needs-reconciliation" in statuses
        else "deadline_missed" if "deadline_missed" in statuses else "failed" if statuses & {"failed", "cancelled"}
        else "completed" if statuses == {"completed"} else "running" if "running" in statuses
        else _planning_status(decisions) if decisions and _planning_status(decisions) is not None
        else "response_ready" if all(job.status == "completed" for job in jobs if job.step_request.required_for_response)
        else "scheduled")
    if value["status"] != status:
        raise ValueError("Replay workflow status disagrees with its jobs/cancellation")
    accounting_context, accounting = None, ()
    if version == ACCOUNTING_BUNDLE_VERSION:
        accounting_context, accounting = _accounting_evidence(value, request, configuration, attempts, jobs)
    return WorkflowSnapshot(request, configuration, status, tuple(jobs), tuple(attempts), cancelled, history, tuple(events),
                            planning_context=planning_context, decisions=decisions,
                            accounting_context=accounting_context, accounting=accounting)


def _read_snapshot(db: Path, workflow_id: str):
    if not db.is_file():
        raise FileNotFoundError(db)
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        versions = connection.execute("SELECT * FROM queue_meta").fetchall()
        if len(versions) != 1 or tuple(versions[0]) != (QUEUE_VERSION, SCHEMA_VERSION):
            raise ValueError("Unsupported queue/schema version")
        reader = object.__new__(QueueStore)
        reader.path, reader.connection = db, connection
        return reader.read(workflow_id)
    except sqlite3.DatabaseError as exc:
        raise ValueError("Durable queue database is invalid") from exc
    finally:
        connection.rollback()
        connection.close()


def export_workflow(db: str | Path, workflow_id: str, output: str | Path) -> Path:
    """Export one validated consistent snapshot to a new directory; never overwrite."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    payload = _snapshot_dict(_read_snapshot(Path(db), workflow_id))
    _decode_snapshot(payload)  # Refuse malformed evidence before creating output.
    content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    manifest = {"schema_version": SCHEMA_VERSION, "bundle_version": payload["bundle_version"],
        "record_type": "DurableWorkflowReplayManifest", "workflow_id": workflow_id,
        "files": {"workflow.json": hashlib.sha256(content).hexdigest()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # Exclusive reservation, including against concurrent exporters.
    created = []
    try:
        for name, data in (("workflow.json", content), ("manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))):
            path = output / name
            with path.open("xb") as stream:
                created.append(path)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return output
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        try:
            output.rmdir()
        except OSError:
            pass
        raise


def load_bundle(bundle: str | Path) -> WorkflowSnapshot:
    """Strictly inspect saved evidence without provider imports, inference or waits."""
    bundle = Path(bundle)
    if not bundle.is_dir() or {item.name for item in bundle.iterdir()} != {"manifest.json", "workflow.json"}:
        raise ValueError("Replay bundle must contain only manifest.json and workflow.json")
    if any((bundle / name).is_symlink() or not (bundle / name).is_file() for name in ("manifest.json", "workflow.json")):
        raise ValueError("Replay files must be regular files inside the bundle")
    try:
        manifest = _load((bundle / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
            raise ValueError("Invalid durable replay manifest fields")
        _version(manifest, "DurableWorkflowReplayManifest")
        if (not isinstance(manifest["files"], dict) or set(manifest["files"]) != {"workflow.json"}
                or not isinstance(manifest["files"]["workflow.json"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", manifest["files"]["workflow.json"])):
            raise ValueError("Replay manifest requires a SHA256 for workflow.json only")
        data = (bundle / "workflow.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["files"]["workflow.json"]:
            raise ValueError("Replay checksum mismatch")
        payload = _load(data.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("bundle_version") != manifest["bundle_version"]:
            raise ValueError("Replay payload and manifest bundle versions differ")
        snapshot = _decode_snapshot(payload)
        if manifest["workflow_id"] != snapshot.request.workflow_id:
            raise ValueError("Replay manifest workflow identity mismatch")
        return snapshot
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("Malformed durable replay bundle") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export one workflow from an existing durable queue")
    export.add_argument("--db", required=True)
    export.add_argument("--workflow", required=True)
    export.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect", help="Validate and summarize stored evidence offline")
    inspect.add_argument("--bundle", required=True)
    report = commands.add_parser("report", help="Validate and report stored accounting entirely offline")
    report.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            output = export_workflow(args.db, args.workflow, args.output)
            print(json.dumps({"bundle": str(output), "workflow_id": args.workflow}))
        else:
            snapshot = load_bundle(args.bundle)
            if args.command == "report":
                from workflow.accounting_report import execution_report
                print(json.dumps(execution_report(snapshot), indent=2))
                return 0
            summary = {"schema_version": SCHEMA_VERSION, "mode": "replay",
                "workflow_id": snapshot.request.workflow_id, "status": snapshot.status,
                "step_statuses": {job.step_request.step_id: job.status for job in snapshot.jobs},
                "attempt_count": len(snapshot.attempts), "scope": "stored planned-queue evidence; no policy comparison"
                    if getattr(snapshot, "planning_context", None) is not None else "stored fixed-queue evidence; no policy comparison"}
            if snapshot.accounting_context is not None:
                accounted = {record["attempt_id"] for record in snapshot.accounting}
                summary.update(accounted_attempt_count=len(accounted),
                    unaccounted_attempt_ids=[attempt.attempt_id for attempt in snapshot.attempts
                                             if attempt.attempt_id not in accounted],
                    unrecorded_dispatched_step_ids=[job.step_request.step_id for job in snapshot.jobs
                        if job.dispatched_at is not None and not any(
                            attempt.planned_candidate == job.candidate and attempt.retry_index == job.retry_index
                            for attempt in snapshot.attempts)])
            print(json.dumps(summary))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"queue-replay: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
