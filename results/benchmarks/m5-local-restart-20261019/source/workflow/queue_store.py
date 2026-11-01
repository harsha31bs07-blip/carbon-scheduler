"""SQLite coordination for scheduled work, independent of provider calls.

A lease first reserves an undispatched job. ``mark_running`` is the durable
boundary before an external call. An expired running lease is ambiguous and is
never automatically redispatched. Late results from its unchanged owner/token
are accepted so incurred usage survives cancellation and recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import uuid

from shared.deployments import DeploymentRegistry
from shared.scheduling import ExecutionAttempt, PlannedCandidate, SCHEMA_VERSION, StepRequest, WorkflowRequest


QUEUE_VERSION = 1
STATUSES = frozenset({"scheduled", "running", "completed", "failed", "cancelled",
                      "needs-reconciliation", "deadline_missed"})


class LeaseError(RuntimeError):
    """The claim is stale, expired, or belongs to another worker."""


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite nonnegative seconds")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite nonnegative seconds")


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _utc(value).isoformat(timespec="microseconds")


def _expiry(now, seconds):
    _seconds(seconds, "lease_seconds")
    if seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    try:
        return _utc(now) + timedelta(seconds=seconds)
    except OverflowError:
        raise ValueError("Lease exceeds the supported datetime range") from None


def _time(value):
    return _utc(datetime.fromisoformat(value)) if value is not None else None


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate stored JSON field")
            result[key] = item
        return result
    def nonfinite(value):
        raise ValueError("Nonfinite stored JSON value")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=nonfinite)


def _configuration(value):
    allowed = {"registry_snapshot", "registry_fingerprint", "calibration_id", "policy_version",
               "planner_version", "experiment_mode", "workload_fingerprint", "kb_fingerprint",
               "worker_version", "timeout_seconds", "max_retries"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Queue configuration permits only registry snapshots and version identifiers")
    result = dict(value)
    for key, item in result.items():
        if key == "registry_snapshot":
            result[key] = DeploymentRegistry.from_dict(item).to_dict()
        elif key == "timeout_seconds":
            _seconds(item, key)
            if not 0 < item <= 300:
                raise ValueError("timeout_seconds must be positive and at most 300")
        elif key == "max_retries":
            if type(item) is not int or not 0 <= item <= 3:
                raise ValueError("max_retries must be an integer between 0 and 3")
        else:
            _text(item, key)
    if result.get("experiment_mode", "live") not in {"live", "simulation", "replay"}:
        raise ValueError("Unsupported experiment mode")
    if "registry_snapshot" in result and "registry_fingerprint" in result:
        if DeploymentRegistry.from_dict(result["registry_snapshot"]).config_fingerprint() != result["registry_fingerprint"]:
            raise ValueError("Registry fingerprint does not match its snapshot")
    return result


def _expected_model(candidate, configuration):
    if "registry_snapshot" in configuration:
        deployment = DeploymentRegistry.from_dict(configuration["registry_snapshot"]).require_model(
            candidate.deployment_id, candidate.model_id)
        return deployment.provider_model(candidate.model_id)
    return candidate.model_id


def _undispatched_failure(attempt):
    """A provider preflight rejection carries no fabricated execution facts."""
    return attempt.status == "failed" and all(getattr(attempt, name) is None for name in (
        "actual_start", "actual_finish", "executed_model_id", "executed_deployment_id",
        "provider_reported_model_id", "wait_seconds", "provider_seconds", "input_tokens",
        "output_tokens", "observations", "output", "completion_reason"))


@dataclass(frozen=True)
class Claim:
    workflow_id: str
    step_request: StepRequest
    candidate: PlannedCandidate
    worker_id: str
    lease_token: str
    lease_expires_at: datetime
    dependency_outputs: dict[str, str]
    configuration: dict
    retry_index: int


@dataclass(frozen=True)
class QueueJob:
    step_request: StepRequest
    candidate: PlannedCandidate
    status: str
    retry_index: int
    output: str | None
    error_code: str | None
    dispatched_at: datetime | None
    finished_at: datetime | None
    rendered_step: StepRequest | None
    source_ids: tuple[str, ...]
    input_fingerprint: str | None
    lookup_seconds: float
    output_valid: bool | None
    validation_error: str | None


@dataclass(frozen=True)
class WorkflowSnapshot:
    request: WorkflowRequest
    configuration: dict
    status: str
    jobs: tuple[QueueJob, ...]
    attempts: tuple[ExecutionAttempt, ...]
    cancelled_at: datetime | None
    plan_history: tuple[PlannedCandidate, ...]
    events: tuple[dict, ...]

    def completed_outputs(self):
        return {job.step_request.step_id: job.output for job in self.jobs if job.status == "completed"}


class QueueStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS queue_meta(version INTEGER NOT NULL, schema_version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS workflows(
                id TEXT PRIMARY KEY, request TEXT NOT NULL, configuration TEXT NOT NULL,
                cancelled_at TEXT);
            CREATE TABLE IF NOT EXISTS jobs(
                workflow_id TEXT NOT NULL REFERENCES workflows(id), step_id TEXT NOT NULL,
                candidate TEXT NOT NULL, planned_start TEXT NOT NULL, deadline TEXT NOT NULL,
                status TEXT NOT NULL, retry_index INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT, lease_owner TEXT, lease_until TEXT, dispatched_at TEXT,
                output TEXT, error_code TEXT, finished_at TEXT, rendered_step TEXT,
                output_valid INTEGER, validation_error TEXT,
                metadata TEXT NOT NULL DEFAULT '{"source_ids":[],"input_fingerprint":null,"lookup_seconds":0}',
                PRIMARY KEY(workflow_id, step_id));
            CREATE TABLE IF NOT EXISTS attempts(
                attempt_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, step_id TEXT NOT NULL,
                payload TEXT NOT NULL, recorded_at TEXT NOT NULL,
                FOREIGN KEY(workflow_id, step_id) REFERENCES jobs(workflow_id, step_id));
            CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status,planned_start);
            CREATE TABLE IF NOT EXISTS plan_history(
                workflow_id TEXT NOT NULL REFERENCES workflows(id), candidate TEXT NOT NULL, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS queue_events(
                workflow_id TEXT NOT NULL REFERENCES workflows(id), step_id TEXT,
                kind TEXT NOT NULL, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS claim_history(
                lease_token TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, step_id TEXT NOT NULL,
                worker_id TEXT NOT NULL, candidate TEXT NOT NULL, retry_index INTEGER NOT NULL,
                claimed_at TEXT NOT NULL, dispatched_at TEXT,
                FOREIGN KEY(workflow_id,step_id) REFERENCES jobs(workflow_id,step_id));
        """)
        with self._transaction():
            versions = self.connection.execute("SELECT * FROM queue_meta").fetchall()
            if not versions:
                self.connection.execute("INSERT INTO queue_meta VALUES (?,?)", (QUEUE_VERSION, SCHEMA_VERSION))
            elif len(versions) != 1 or tuple(versions[0]) != (QUEUE_VERSION, SCHEMA_VERSION):
                raise ValueError("Unsupported queue/schema version")

    def close(self):
        self.connection.close()

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def enqueue(self, request: WorkflowRequest, assignments: tuple[PlannedCandidate, ...], *, configuration: dict) -> str:
        if not isinstance(request, WorkflowRequest):
            raise ValueError("request must be WorkflowRequest")
        config = _configuration(configuration)
        plans = self._plans(request, assignments)
        if "registry_snapshot" in config:
            registry = DeploymentRegistry.from_dict(config["registry_snapshot"])
            for candidate in plans.values():
                registry.require_model(candidate.deployment_id, candidate.model_id)
        with self._transaction():
            self.connection.execute("INSERT INTO workflows VALUES (?,?,?,NULL)",
                                    (request.workflow_id, _dump(request.to_dict()), _dump(config)))
            for step in request.steps:
                candidate = plans[step.step_id]
                self.connection.execute("""INSERT INTO jobs
                    (workflow_id,step_id,candidate,planned_start,deadline,status) VALUES (?,?,?,?,?,?)""",
                    (request.workflow_id, step.step_id, _dump(candidate.to_dict()), _stamp(candidate.planned_start),
                     _stamp(step.latest_finish), "scheduled"))
                self.connection.execute("INSERT INTO plan_history VALUES (?,?,?)",
                    (request.workflow_id, _dump(candidate.to_dict()), _stamp(request.arrived_at)))
        return request.workflow_id

    @staticmethod
    def _plans(request, assignments):
        if not isinstance(assignments, tuple) or any(not isinstance(p, PlannedCandidate) for p in assignments):
            raise ValueError("assignments must be a tuple of PlannedCandidate values")
        plans = {p.step_id: p for p in assignments}
        if len(plans) != len(assignments) or set(plans) != {s.step_id for s in request.steps}:
            raise ValueError("Exactly one assignment is required for every step")
        for step in request.steps:
            if not step.earliest_start <= plans[step.step_id].planned_start < step.latest_finish:
                raise ValueError("Planned start is outside the step deadline window")
        return plans

    def read(self, workflow_id: str) -> WorkflowSnapshot:
        if self.connection.in_transaction:
            return self._read(workflow_id)
        self.connection.execute("BEGIN")
        try:
            result = self._read(workflow_id)
        except BaseException:
            self.connection.rollback()
            raise
        self.connection.commit()
        return result

    def _read(self, workflow_id: str) -> WorkflowSnapshot:
        _text(workflow_id, "workflow_id")
        row = self.connection.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        request = WorkflowRequest.from_dict(_load(row["request"]))
        if request.workflow_id != workflow_id:
            raise ValueError("Stored workflow identity mismatch")
        configuration = _configuration(_load(row["configuration"]))
        steps = {step.step_id: step for step in request.steps}
        jobs = []
        for job in self.connection.execute("SELECT * FROM jobs WHERE workflow_id=? ORDER BY rowid", (workflow_id,)):
            candidate = PlannedCandidate.from_dict(_load(job["candidate"]))
            if job["step_id"] not in steps or candidate.step_id != job["step_id"] or job["status"] not in STATUSES:
                raise ValueError("Malformed stored job identity/status")
            step = steps[job["step_id"]]
            if not step.earliest_start <= candidate.planned_start < step.latest_finish:
                raise ValueError("Stored planned start is outside the step deadline window")
            if _time(job["planned_start"]) != candidate.planned_start or _time(job["deadline"]) != step.latest_finish:
                raise ValueError("Stored job timing mismatch")
            if type(job["retry_index"]) is not int or job["retry_index"] < 0:
                raise ValueError("Invalid stored retry index")
            rendered = StepRequest.from_dict(_load(job["rendered_step"])) if job["rendered_step"] else None
            metadata = _load(job["metadata"])
            if not isinstance(metadata, dict) or set(metadata) != {"source_ids", "input_fingerprint", "lookup_seconds"}:
                raise ValueError("Invalid stored rendering metadata")
            self._render(step, rendered, **metadata)
            if job["status"] == "completed" and (job["output"] is None or job["finished_at"] is None):
                raise ValueError("Completed job lacks persisted output/finish")
            if job["output_valid"] is not None and (type(job["output_valid"]) is not int or job["output_valid"] not in (0, 1)):
                raise ValueError("Malformed stored output validation")
            if job["validation_error"] is not None:
                _text(job["validation_error"], "validation_error")
            if job["status"] == "completed" and job["output_valid"] != 1:
                raise ValueError("Completed job lacks accepted output")
            lease = (job["lease_token"], job["lease_owner"], job["lease_until"])
            if any(value is None for value in lease) and any(value is not None for value in lease):
                raise ValueError("Incomplete stored lease")
            if job["status"] in {"running", "needs-reconciliation"} and (not all(lease) or job["dispatched_at"] is None):
                raise ValueError("Dispatched job lacks a lease/start")
            jobs.append(QueueJob(step, candidate, job["status"], job["retry_index"], job["output"],
                                 job["error_code"], _time(job["dispatched_at"]), _time(job["finished_at"]),
                                 rendered, tuple(metadata["source_ids"]), metadata["input_fingerprint"],
                                 metadata["lookup_seconds"], None if job["output_valid"] is None else bool(job["output_valid"]),
                                 job["validation_error"]))
        if {job.step_request.step_id for job in jobs} != set(steps):
            raise ValueError("Stored workflow jobs are incomplete")
        attempts = []
        by_id = {job.step_request.step_id: job for job in jobs}
        for record in self.connection.execute("SELECT * FROM attempts WHERE workflow_id=? ORDER BY rowid", (workflow_id,)):
            attempt = ExecutionAttempt.from_dict(_load(record["payload"]))
            nondispatched = _undispatched_failure(attempt)
            if (attempt.workflow_id != workflow_id or attempt.attempt_id != record["attempt_id"]
                    or attempt.planned_candidate.step_id != record["step_id"] or record["step_id"] not in by_id
                    or (not nondispatched and attempt.executed_model_id != _expected_model(attempt.planned_candidate, configuration))
                    or (not nondispatched and attempt.executed_deployment_id != attempt.planned_candidate.deployment_id)
                    or attempt.retry_index > by_id[record["step_id"]].retry_index
                    or attempt.status not in {"completed", "failed", "cancelled", "needs-reconciliation"}
                    or (not nondispatched and (attempt.actual_finish is None or _time(record["recorded_at"]) < attempt.actual_finish))):
                raise ValueError("Stored attempt identity mismatch")
            attempts.append(attempt)
        statuses = {job.status for job in jobs}
        status = ("cancelled" if row["cancelled_at"] else "needs-reconciliation" if "needs-reconciliation" in statuses
                  else "deadline_missed" if "deadline_missed" in statuses else "failed" if statuses & {"failed", "cancelled"}
                  else "completed" if statuses == {"completed"} else "running" if "running" in statuses
                  else "response_ready" if all(job.status == "completed" for job in jobs if job.step_request.required_for_response)
                  else "scheduled")
        history = tuple(PlannedCandidate.from_dict(_load(record[0])) for record in self.connection.execute(
            "SELECT candidate FROM plan_history WHERE workflow_id=? ORDER BY rowid", (workflow_id,)))
        if any(candidate.step_id not in steps for candidate in history):
            raise ValueError("Stored plan history identity mismatch")
        if any(attempt.planned_candidate not in history for attempt in attempts):
            raise ValueError("Stored attempt has no corresponding plan history")
        events = tuple({"step_id": event[0], "kind": event[1], "recorded_at": _time(event[2])}
            for event in self.connection.execute("SELECT step_id,kind,recorded_at FROM queue_events WHERE workflow_id=? ORDER BY rowid", (workflow_id,)))
        return WorkflowSnapshot(request, configuration, status,
                                tuple(jobs), tuple(attempts), _time(row["cancelled_at"]), history, events)

    def _recover(self, now):
        stamp = _stamp(now)
        self.connection.execute("""UPDATE jobs SET lease_token=NULL,lease_owner=NULL,lease_until=NULL
            WHERE status='scheduled' AND lease_until<=?""", (stamp,))
        self.connection.execute("""UPDATE jobs SET status='needs-reconciliation',error_code='lease_expired_after_dispatch'
            WHERE status='running' AND lease_until<=?""", (stamp,))
        self.connection.execute("""UPDATE jobs SET status='deadline_missed',error_code='deadline_missed',finished_at=?,
            lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE status='scheduled' AND deadline<=?""", (stamp, stamp))

    def recover(self, *, now: datetime):
        with self._transaction():
            self._recover(_utc(now))

    def claim(self, worker_id: str, *, now: datetime, lease_seconds: float) -> Claim | None:
        _text(worker_id, "worker_id")
        now = _utc(now)
        expiry = _expiry(now, lease_seconds)
        with self._transaction():
            self._recover(now)
            rows = self.connection.execute("""SELECT j.* FROM jobs j JOIN workflows w ON w.id=j.workflow_id
                WHERE j.status='scheduled' AND j.lease_token IS NULL AND j.planned_start<=?
                AND j.deadline>? AND w.cancelled_at IS NULL ORDER BY j.planned_start,j.workflow_id,j.rowid""",
                (_stamp(now), _stamp(now))).fetchall()
            for row in rows:
                state = self.read(row["workflow_id"])
                job = next(job for job in state.jobs if job.step_request.step_id == row["step_id"])
                outputs = state.completed_outputs()
                if not set(job.step_request.dependencies) <= outputs.keys():
                    continue
                if self._occupied_slots(job.candidate.deployment_id) >= self._capacity(job.candidate, state.configuration):
                    continue
                token = uuid.uuid4().hex
                self.connection.execute("UPDATE jobs SET lease_token=?,lease_owner=?,lease_until=? WHERE workflow_id=? AND step_id=?",
                                        (token, worker_id, _stamp(expiry), row["workflow_id"], row["step_id"]))
                self.connection.execute("INSERT INTO claim_history VALUES (?,?,?,?,?,?,?,NULL)", (token,
                    row["workflow_id"], row["step_id"], worker_id, _dump(job.candidate.to_dict()), job.retry_index, _stamp(now)))
                return Claim(row["workflow_id"], job.step_request, job.candidate, worker_id, token, expiry,
                             {key: outputs[key] for key in job.step_request.dependencies}, state.configuration, job.retry_index)
        return None

    @staticmethod
    def _capacity(candidate, configuration):
        if "registry_snapshot" in configuration:
            return DeploymentRegistry.from_dict(configuration["registry_snapshot"]).require_model(
                candidate.deployment_id, candidate.model_id).max_concurrency
        return 1

    def _occupied_slots(self, deployment_id):
        # A reservation consumes a slot before dispatch. Expired running and
        # cancelled in-flight calls retain occupancy until reconciled/completed.
        rows = self.connection.execute("""SELECT candidate FROM jobs WHERE lease_token IS NOT NULL
            AND status IN ('scheduled','running','needs-reconciliation','cancelled')""")
        return sum(PlannedCandidate.from_dict(_load(row[0])).deployment_id == deployment_id for row in rows)

    def _owned(self, claim, now, *, live=True):
        if not isinstance(claim, Claim):
            raise ValueError("claim must be Claim")
        self.read(claim.workflow_id)  # Fail closed on malformed durable state before mutation.
        row = self.connection.execute("SELECT * FROM jobs WHERE workflow_id=? AND step_id=?",
                                      (claim.workflow_id, claim.candidate.step_id)).fetchone()
        if (row is None or row["lease_token"] != claim.lease_token or row["lease_owner"] != claim.worker_id
                or PlannedCandidate.from_dict(_load(row["candidate"])) != claim.candidate
                or row["retry_index"] != claim.retry_index or claim.step_request.step_id != claim.candidate.step_id
                or (live and _time(row["lease_until"]) <= _utc(now))):
            raise LeaseError("Claim does not own the current lease")
        return row

    @staticmethod
    def _render(original, rendered, source_ids=(), input_fingerprint=None, lookup_seconds=0):
        if rendered is not None:
            if not isinstance(rendered, StepRequest) or replace(rendered, prompt=original.prompt,
                    input_tokens=original.input_tokens, input_size_source=original.input_size_source) != original:
                raise ValueError("Rendered request must preserve step constraints and generation controls")
        if not isinstance(source_ids, (tuple, list)):
            raise ValueError("source_ids must be unique text values")
        for source in source_ids:
            _text(source, "source_id")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_ids must be unique text values")
        if input_fingerprint is not None:
            _text(input_fingerprint, "input_fingerprint")
        _seconds(lookup_seconds, "lookup_seconds")
        return {"source_ids": list(source_ids), "input_fingerprint": input_fingerprint, "lookup_seconds": lookup_seconds}

    def mark_running(self, claim: Claim, *, now: datetime, rendered_step: StepRequest | None = None,
                     source_ids=(), input_fingerprint=None, lookup_seconds=0) -> Claim:
        now = _utc(now)
        with self._transaction():
            row = self._owned(claim, now)
            state = self.read(claim.workflow_id)
            original = next(s for s in state.request.steps if s.step_id == claim.candidate.step_id)
            metadata = self._render(original, rendered_step, source_ids, input_fingerprint, lookup_seconds)
            if row["status"] != "scheduled" or state.cancelled_at is not None:
                raise LeaseError("Job cannot enter dispatch")
            if self._occupied_slots(claim.candidate.deployment_id) > self._capacity(claim.candidate, state.configuration):
                raise LeaseError("Deployment has no available dispatch capacity")
            if now < claim.candidate.planned_start or now >= original.latest_finish:
                raise LeaseError("Dispatch is outside its allowed time window")
            self.connection.execute("""UPDATE jobs SET status='running',dispatched_at=?,rendered_step=?,metadata=?
                WHERE workflow_id=? AND step_id=?""", (_stamp(now), _dump((rendered_step or original).to_dict()),
                _dump(metadata), claim.workflow_id, original.step_id))
            self.connection.execute("UPDATE claim_history SET dispatched_at=? WHERE lease_token=?", (_stamp(now), claim.lease_token))
        return replace(claim, step_request=rendered_step or original)

    def renew(self, claim: Claim, *, now: datetime, lease_seconds: float) -> Claim:
        expiry = _expiry(now, lease_seconds)
        with self._transaction():
            row = self._owned(claim, now)
            if row["status"] not in {"scheduled", "running"}:
                raise LeaseError("Only reserved or running jobs can renew")
            self.connection.execute("UPDATE jobs SET lease_until=? WHERE workflow_id=? AND step_id=?",
                                    (_stamp(expiry), claim.workflow_id, claim.candidate.step_id))
        return replace(claim, lease_expires_at=expiry)

    def complete(self, claim: Claim, attempt: ExecutionAttempt, *, now: datetime,
                 output_valid: bool = True, validation_error: str | None = None):
        if type(output_valid) is not bool:
            raise ValueError("output_valid must be boolean")
        if validation_error is not None:
            _text(validation_error, "validation_error")
        if output_valid and validation_error is not None:
            raise ValueError("Accepted output cannot have a validation error")
        if not output_valid and validation_error is None:
            raise ValueError("Rejected output requires a validation error")
        now = _utc(now)
        with self._transaction():
            row = self._owned(claim, now, live=False)
            if row["status"] not in {"running", "needs-reconciliation", "cancelled"} or row["dispatched_at"] is None:
                raise LeaseError("Job was not dispatched under this lease")
            nondispatched = isinstance(attempt, ExecutionAttempt) and _undispatched_failure(attempt)
            if (not isinstance(attempt, ExecutionAttempt) or attempt.workflow_id != claim.workflow_id
                    or attempt.planned_candidate != claim.candidate or attempt.retry_index != claim.retry_index
                    or attempt.status not in {"completed", "failed", "cancelled", "needs-reconciliation"}
                    or (not nondispatched and (
                        attempt.executed_model_id != _expected_model(claim.candidate, self.read(claim.workflow_id).configuration)
                        or attempt.executed_deployment_id != claim.candidate.deployment_id
                        or attempt.actual_start is None or attempt.actual_start < _time(row["dispatched_at"])
                        or attempt.actual_start > now or attempt.actual_finish is None or attempt.actual_finish > now))):
                raise ValueError("Attempt identity, status, or timing does not match the dispatched job")
            rendered = StepRequest.from_dict(_load(row["rendered_step"]))
            if ((attempt.output_limit_tokens is not None and attempt.output_limit_tokens != rendered.output_limit_tokens)
                    or attempt.enable_thinking != rendered.enable_thinking):
                raise ValueError("Attempt controls do not match the dispatched request")
            self.connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?)", (attempt.attempt_id, claim.workflow_id,
                claim.candidate.step_id, _dump(attempt.to_dict()), _stamp(now)))
            cancelled = self.connection.execute("SELECT cancelled_at FROM workflows WHERE id=?", (claim.workflow_id,)).fetchone()[0]
            late = now > _time(row["deadline"]) or attempt.error == "step-deadline-exceeded"
            if attempt.status == "completed" and (attempt.output is None or not attempt.output.strip()):
                output_valid, validation_error = False, "empty_output"
            status = ("cancelled" if cancelled else "deadline_missed" if late
                      else "failed" if attempt.status == "completed" and not output_valid else attempt.status)
            retain_lease = status == "needs-reconciliation"
            self.connection.execute("""UPDATE jobs SET status=?,output=?,error_code=?,finished_at=?,output_valid=?,validation_error=?,
                lease_token=?,lease_owner=?,lease_until=?,dispatched_at=? WHERE workflow_id=? AND step_id=?""",
                (status, attempt.output, "deadline_missed" if late else validation_error or attempt.error, _stamp(now),
                 int(output_valid) if attempt.status == "completed" else None, validation_error,
                 row["lease_token"] if retain_lease else None, row["lease_owner"] if retain_lease else None,
                 row["lease_until"] if retain_lease else None,
                 None if nondispatched else row["dispatched_at"],
                 claim.workflow_id, claim.candidate.step_id))

    def fail_before_dispatch(self, claim: Claim, error_code: str, *, now: datetime):
        _text(error_code, "error_code")
        with self._transaction():
            row = self._owned(claim, now)
            if row["status"] != "scheduled":
                raise LeaseError("Job has already entered dispatch")
            late = _utc(now) >= _time(row["deadline"])
            self.connection.execute("""UPDATE jobs SET status=?,error_code=?,finished_at=?,
                lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE workflow_id=? AND step_id=?""",
                ("deadline_missed" if late else "failed", "deadline_missed" if late else error_code,
                 _stamp(now), claim.workflow_id, claim.candidate.step_id))

    def archive_late_attempt(self, claim: Claim, attempt: ExecutionAttempt, *, now: datetime):
        """Archive usage from an explicitly superseded call without accepting output.

        The old lease capability must match durable history; it cannot complete,
        fail or otherwise change the current retry. Repeating the same archival
        record is idempotent, while an ID reused with different facts is rejected.
        """
        now = _utc(now)
        if not isinstance(claim, Claim) or not isinstance(attempt, ExecutionAttempt):
            raise ValueError("Archival requires a claim and execution attempt")
        with self._transaction():
            state = self.read(claim.workflow_id)
            old = self.connection.execute("SELECT * FROM claim_history WHERE lease_token=?", (claim.lease_token,)).fetchone()
            job = next((job for job in state.jobs if job.step_request.step_id == claim.candidate.step_id), None)
            if (old is None or job is None or old["workflow_id"] != claim.workflow_id
                    or old["step_id"] != claim.candidate.step_id or old["worker_id"] != claim.worker_id
                    or PlannedCandidate.from_dict(_load(old["candidate"])) != claim.candidate
                    or old["retry_index"] != claim.retry_index or old["dispatched_at"] is None
                    or job.retry_index <= claim.retry_index):
                raise LeaseError("Only a verified superseded dispatched claim can archive late usage")
            if (attempt.workflow_id != claim.workflow_id or attempt.planned_candidate != claim.candidate
                    or attempt.retry_index != claim.retry_index
                    or attempt.status not in {"completed", "failed", "cancelled", "needs-reconciliation"}
                    or attempt.executed_model_id != _expected_model(claim.candidate, state.configuration)
                    or attempt.executed_deployment_id != claim.candidate.deployment_id
                    or attempt.actual_start is None or attempt.actual_start < _time(old["dispatched_at"])
                    or attempt.actual_finish is None or attempt.actual_finish > now):
                raise ValueError("Late attempt does not match superseded dispatch")
            payload = _dump(attempt.to_dict())
            existing = self.connection.execute("SELECT payload FROM attempts WHERE attempt_id=?", (attempt.attempt_id,)).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise ValueError("Attempt ID is already associated with different facts")
                return
            self.connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?)", (attempt.attempt_id,
                claim.workflow_id, claim.candidate.step_id, payload, _stamp(now)))
            self.connection.execute("INSERT INTO queue_events VALUES (?,?,?,?)", (claim.workflow_id,
                claim.candidate.step_id, "archived_late_superseded_attempt", _stamp(now)))

    def cancel(self, workflow_id: str, *, now: datetime):
        stamp = _stamp(now)
        with self._transaction():
            self.read(workflow_id)
            self.connection.execute("UPDATE workflows SET cancelled_at=COALESCE(cancelled_at,?) WHERE id=?", (stamp, workflow_id))
            self.connection.execute("INSERT INTO queue_events VALUES (?,NULL,?,?)", (workflow_id, "cancelled", stamp))
            # Running claims keep their token for late usage. Undispatched claims are invalidated.
            self.connection.execute("""UPDATE jobs SET status='cancelled',
                finished_at=CASE WHEN dispatched_at IS NULL THEN ? ELSE NULL END,
                lease_token=CASE WHEN dispatched_at IS NULL THEN NULL ELSE lease_token END,
                lease_owner=CASE WHEN dispatched_at IS NULL THEN NULL ELSE lease_owner END,
                lease_until=CASE WHEN dispatched_at IS NULL THEN NULL ELSE lease_until END
                WHERE workflow_id=? AND status IN ('scheduled','running','needs-reconciliation')""", (stamp, workflow_id))

    def retry(self, workflow_id: str, step_id: str, *, now: datetime, acknowledge_ambiguous: bool = False):
        if type(acknowledge_ambiguous) is not bool:
            raise ValueError("acknowledge_ambiguous must be boolean")
        with self._transaction():
            state = self.read(workflow_id)
            row = self.connection.execute("SELECT * FROM jobs WHERE workflow_id=? AND step_id=?", (workflow_id, step_id)).fetchone()
            if (row is None or state.cancelled_at is not None or row["status"] not in {"failed", "needs-reconciliation"}
                    or _utc(now) >= _time(row["deadline"])):
                raise ValueError("Job is not eligible for explicit retry")
            if row["retry_index"] >= state.configuration.get("max_retries", 3):
                raise ValueError("Saved retry limit has been reached")
            if row["dispatched_at"] is not None and not acknowledge_ambiguous:
                raise ValueError("Dispatched work requires acknowledgement of possible duplicate usage")
            self.connection.execute("INSERT INTO queue_events VALUES (?,?,?,?)", (workflow_id, step_id,
                "retry_acknowledged_possible_duplicate_usage" if row["dispatched_at"] else "retry_known_not_dispatched", _stamp(now)))
            self.connection.execute("""UPDATE jobs SET status='scheduled',retry_index=retry_index+1,
                lease_token=NULL,lease_owner=NULL,lease_until=NULL,dispatched_at=NULL,output=NULL,
                error_code=NULL,finished_at=NULL,rendered_step=NULL,output_valid=NULL,validation_error=NULL
                WHERE workflow_id=? AND step_id=?""", (workflow_id, step_id))

    def replan(self, workflow_id: str, assignments: tuple[PlannedCandidate, ...], *, now: datetime):
        with self._transaction():
            state = self.read(workflow_id)
            plans = self._plans(state.request, assignments)
            if "registry_snapshot" in state.configuration:
                registry = DeploymentRegistry.from_dict(state.configuration["registry_snapshot"])
                for candidate in plans.values():
                    registry.require_model(candidate.deployment_id, candidate.model_id)
            if state.cancelled_at is not None:
                raise ValueError("Cancelled workflows cannot be replanned")
            for job in state.jobs:
                candidate = plans[job.step_request.step_id]
                if candidate == job.candidate:
                    continue
                row = self.connection.execute("SELECT lease_token FROM jobs WHERE workflow_id=? AND step_id=?",
                    (workflow_id, candidate.step_id)).fetchone()
                if job.status != "scheduled" or row[0] is not None or candidate.planned_start < _utc(now):
                    raise ValueError("Only unclaimed, undispatched scheduled jobs can be replanned")
                self.connection.execute("UPDATE jobs SET candidate=?,planned_start=? WHERE workflow_id=? AND step_id=?",
                    (_dump(candidate.to_dict()), _stamp(candidate.planned_start), workflow_id, candidate.step_id))
                self.connection.execute("INSERT INTO plan_history VALUES (?,?,?)", (workflow_id, _dump(candidate.to_dict()), _stamp(now)))
