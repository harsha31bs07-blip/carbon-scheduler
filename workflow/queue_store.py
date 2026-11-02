"""SQLite coordination for scheduled work, independent of provider calls.

A lease first reserves an undispatched job. ``mark_running`` is the durable
boundary before an external call. An expired running lease is ambiguous and is
never automatically redispatched. Late results from its unchanged owner/token
are accepted so incurred usage survives cancellation and recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
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


class PlanningConflict(ValueError):
    """The durable workflow or shared occupancy changed during preparation."""


class SubmissionConflict(ValueError):
    """A submission token was already bound to different operator inputs."""


def _workflow_status(statuses, required, cancelled, report=None):
    values = set(statuses.values())
    status = ("cancelled" if cancelled else "needs-reconciliation" if "needs-reconciliation" in values
        else "deadline_missed" if "deadline_missed" in values else "failed" if values & {"failed", "cancelled"}
        else "completed" if values == {"completed"} else "running" if "running" in values
        else "response_ready" if all(statuses.get(step) == "completed" for step in required) else "scheduled")
    if report is not None and QueueStore._decision_winner(report) is None and status not in {
            "cancelled", "needs-reconciliation", "deadline_missed", "failed", "completed", "running"}:
        status = QueueStore._planning_status(report)
    return status


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
               "worker_version", "timeout_seconds", "max_retries", "accounting_version"}
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
        elif key == "accounting_version":
            if item != "execution-accounting.v1":
                raise ValueError("Unsupported execution accounting version")
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
    planning_revision: int | None = None


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
    planning_context: dict | None = None
    decisions: tuple[dict, ...] = ()
    accounting_context: dict | None = None
    accounting: tuple[dict, ...] = ()

    def completed_outputs(self):
        return {job.step_request.step_id: job.output for job in self.jobs if job.status == "completed"}


@dataclass(frozen=True)
class PlanningSnapshot:
    workflow: WorkflowSnapshot
    token: str
    reservations: tuple
    revision: int


class QueueStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        # Missing accounting tables are a supported legacy database. A partial
        # extension must be rejected before CREATE IF NOT EXISTS could hide it.
        self._has_accounting_tables()
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
            CREATE TABLE IF NOT EXISTS planning_meta(version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_contexts(
                workflow_id TEXT PRIMARY KEY REFERENCES workflows(id), context TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planning_decisions(
                workflow_id TEXT NOT NULL REFERENCES workflows(id), revision INTEGER NOT NULL,
                recorded_at TEXT NOT NULL, report TEXT NOT NULL, snapshot_token TEXT,
                PRIMARY KEY(workflow_id,revision));
            CREATE TABLE IF NOT EXISTS accounting_meta(version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS accounting_contexts(
                workflow_id TEXT PRIMARY KEY REFERENCES workflows(id), context TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempt_accounting(
                attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
                workflow_id TEXT NOT NULL REFERENCES workflows(id), payload TEXT NOT NULL,
                recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS queue_submissions(
                token TEXT PRIMARY KEY, payload_fingerprint TEXT NOT NULL,
                workflow_id TEXT NOT NULL UNIQUE REFERENCES workflows(id),
                version INTEGER NOT NULL CHECK(version=1));
            CREATE TABLE IF NOT EXISTS worker_presence(
                worker_id TEXT PRIMARY KEY,state TEXT NOT NULL,last_seen TEXT NOT NULL,
                workflow_id TEXT,version INTEGER NOT NULL CHECK(version=1));
        """)
        with self._transaction():
            versions = self.connection.execute("SELECT * FROM queue_meta").fetchall()
            if not versions:
                self.connection.execute("INSERT INTO queue_meta VALUES (?,?)", (QUEUE_VERSION, SCHEMA_VERSION))
            elif len(versions) != 1 or tuple(versions[0]) != (QUEUE_VERSION, SCHEMA_VERSION):
                raise ValueError("Unsupported queue/schema version")
            planning_versions = self.connection.execute("SELECT version FROM planning_meta").fetchall()
            if not planning_versions:
                self.connection.execute("INSERT INTO planning_meta VALUES (1)")
            elif len(planning_versions) != 1 or planning_versions[0][0] != 1:
                raise ValueError("Unsupported planning metadata version")
            accounting_versions = self.connection.execute("SELECT version FROM accounting_meta").fetchall()
            if not accounting_versions:
                self.connection.execute("INSERT INTO accounting_meta VALUES (1)")
            elif len(accounting_versions) != 1 or accounting_versions[0][0] != 1:
                raise ValueError("Unsupported accounting metadata version")
            if [row[1] for row in self.connection.execute("PRAGMA table_info(queue_submissions)")] != [
                    "token", "payload_fingerprint", "workflow_id", "version"]:
                raise ValueError("Unsupported submission table schema")
            if self.connection.execute("SELECT 1 FROM queue_submissions WHERE version != 1 LIMIT 1").fetchone():
                raise ValueError("Unsupported submission record version")

    @staticmethod
    def _submission_fields(submission):
        if submission is None:
            return None
        if not isinstance(submission, dict) or set(submission) != {"token", "payload"}:
            raise ValueError("Submission requires token and original inputs")
        token = submission["token"]
        if not isinstance(token, str) or not 16 <= len(token) <= 128 or not all(
                char.isascii() and (char.isalnum() or char in "-_") for char in token):
            raise ValueError("Invalid submission token")
        if not isinstance(submission["payload"], dict):
            raise ValueError("Submission inputs must be an object")
        return token, hashlib.sha256(_dump(submission["payload"]).encode()).hexdigest()

    def submission_result(self, submission):
        fields = self._submission_fields(submission)
        if fields is None:
            return None
        row = self.connection.execute("SELECT payload_fingerprint,workflow_id,version FROM queue_submissions WHERE token=?",
                                      (fields[0],)).fetchone()
        if row is None:
            return None
        if row[2] != 1:
            raise ValueError("Unsupported submission record version")
        if row[0] != fields[1]:
            raise SubmissionConflict("This submission token belongs to different inputs")
        self.read(row[1])
        return row[1]

    def _record_submission(self, submission, workflow_id):
        fields = self._submission_fields(submission)
        if fields is not None:
            self.connection.execute("INSERT INTO queue_submissions VALUES (?,?,?,1)", (*fields, workflow_id))

    def workflow_summaries(self, *, query="", status=None, before=None, since=None, limit=20):
        """Keyset pagination of light operator summaries; opening a run validates its full evidence."""
        if not isinstance(query, str) or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid workflow search")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("Invalid workflow cursor")
        allowed = STATUSES | {"response_ready", "infeasible", "planning-failed"}
        if status is not None and status not in allowed:
            raise ValueError("Invalid workflow status filter")
        sql = "SELECT rowid,id,request,cancelled_at FROM workflows WHERE (instr(lower(id),lower(?))>0 OR instr(lower(json_extract(request,'$.input_text')),lower(?))>0)"
        args = [query, query]
        if before is not None:
            sql += " AND rowid<?"
            args.append(before)
        if since is not None:
            sql += " AND julianday(json_extract(request,'$.arrived_at'))>=julianday(?)"
            args.append(_stamp(since))
        result = []
        planning_available = self._has_planning_tables()
        for row in self.connection.execute(sql + " ORDER BY rowid DESC", args):
            request = WorkflowRequest.from_dict(_load(row["request"]))
            jobs = list(self.connection.execute("SELECT step_id,status,error_code FROM jobs WHERE workflow_id=?", (row["id"],)))
            latest = self.connection.execute("SELECT report FROM planning_decisions WHERE workflow_id=? ORDER BY revision DESC LIMIT 1", (row["id"],)).fetchone() if planning_available else None
            current = _workflow_status({job[0]: job[1] for job in jobs},
                [step.step_id for step in request.steps if step.required_for_response], row["cancelled_at"],
                _load(latest[0]) if latest else None)
            if status is not None and current != status:
                continue
            result.append({"cursor": row["rowid"], "workflow_id": row["id"],
                "ticket": " ".join(request.input_text.split())[:160], "policy": request.policy,
                "arrived_at": request.arrived_at.isoformat(), "status": current,
                "completed": sum(job[1] == "completed" for job in jobs), "steps": len(request.steps),
                "issue": next((job[2] for job in jobs if job[2]), None)})
            if len(result) == limit:
                break
        return result

    def recent_workflow_ids(self, *, limit=100):
        """Return recent identities without exposing SQL to operator views."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Workflow list limit must be from one to 1000")
        return tuple(row[0] for row in self.connection.execute(
            "SELECT id FROM workflows ORDER BY rowid DESC LIMIT ?", (limit,)))

    def worker_seen(self, worker_id, *, state, now, workflow_id=None):
        """Observability only: never grants ownership, expires a lease or enables dispatch."""
        _text(worker_id, "worker_id")
        if len(worker_id) > 256 or state not in {"polling", "running", "idle", "stopped"}:
            raise ValueError("Invalid worker presence")
        if workflow_id is not None:
            _text(workflow_id, "workflow_id")
        with self._transaction():
            self.connection.execute("INSERT INTO worker_presence VALUES (?,?,?,?,1) ON CONFLICT(worker_id) DO UPDATE SET state=excluded.state,last_seen=excluded.last_seen,workflow_id=excluded.workflow_id",
                (worker_id, state, _stamp(now), workflow_id))

    def presence_report(self, *, now, max_age_seconds=30):
        now = _utc(now)
        _seconds(max_age_seconds, "presence age")
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='worker_presence'").fetchone():
            return []
        result = []
        for row in self.connection.execute("SELECT * FROM worker_presence ORDER BY worker_id"):
            if row["version"] != 1 or row["state"] not in {"polling", "running", "idle", "stopped"}:
                raise ValueError("Invalid worker presence record")
            age = (now - _time(row["last_seen"])).total_seconds()
            result.append({"worker_id": row["worker_id"], "reported_state": row["state"],
                "state": row["state"] if 0 <= age <= max_age_seconds else "stale",
                "last_seen": row["last_seen"], "workflow_id": row["workflow_id"]})
        return result

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

    def enqueue(self, request: WorkflowRequest, assignments: tuple[PlannedCandidate, ...], *,
                configuration: dict, accounting_context: dict | None = None, submission=None) -> str:
        if not isinstance(request, WorkflowRequest):
            raise ValueError("request must be WorkflowRequest")
        config = _configuration(configuration)
        accounting_context = self._validated_accounting_context(accounting_context, request, config)
        plans = self._plans(request, assignments)
        if "registry_snapshot" in config:
            registry = DeploymentRegistry.from_dict(config["registry_snapshot"])
            for candidate in plans.values():
                registry.require_model(candidate.deployment_id, candidate.model_id)
        with self._transaction():
            existing = self.submission_result(submission)
            if existing is not None:
                return existing
            self.connection.execute("INSERT INTO workflows VALUES (?,?,?,NULL)",
                                    (request.workflow_id, _dump(request.to_dict()), _dump(config)))
            self._insert_accounting_context(request.workflow_id, accounting_context)
            for step in request.steps:
                candidate = plans[step.step_id]
                self.connection.execute("""INSERT INTO jobs
                    (workflow_id,step_id,candidate,planned_start,deadline,status) VALUES (?,?,?,?,?,?)""",
                    (request.workflow_id, step.step_id, _dump(candidate.to_dict()), _stamp(candidate.planned_start),
                     _stamp(step.latest_finish), "scheduled"))
                self.connection.execute("INSERT INTO plan_history VALUES (?,?,?)",
                    (request.workflow_id, _dump(candidate.to_dict()), _stamp(request.arrived_at)))
            self._record_submission(submission, request.workflow_id)
        return request.workflow_id

    def _has_accounting_tables(self):
        # Read-only export instances bypass __init__ and retain legacy support.
        names = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('accounting_meta','accounting_contexts','attempt_accounting')")}
        if not names:
            return False
        if len(names) != 3:
            raise ValueError("Incomplete accounting metadata tables")
        versions = self.connection.execute("SELECT version FROM accounting_meta").fetchall()
        if len(versions) != 1 or type(versions[0][0]) is not int or versions[0][0] != 1:
            raise ValueError("Unsupported accounting metadata version")
        return True

    @staticmethod
    def _validated_accounting_context(value, request, configuration):
        if value is None:
            if "accounting_version" in configuration:
                raise ValueError("Execution accounting version requires an accounting context")
            return None
        if configuration.get("accounting_version") != "execution-accounting.v1":
            raise ValueError("Accounting context requires an execution accounting version")
        if "registry_snapshot" not in configuration:
            raise ValueError("Accounting context requires a registry snapshot")
        from workflow.execution_accounting import validate_accounting_context

        registry = DeploymentRegistry.from_dict(configuration["registry_snapshot"])
        return validate_accounting_context(value, request, registry)

    def _insert_accounting_context(self, workflow_id, context):
        if context is not None:
            self.connection.execute("INSERT INTO accounting_contexts VALUES (?,?)", (workflow_id, _dump(context)))

    def _accounting_records(self, workflow_id, request, configuration, attempts):
        if not self._has_accounting_tables():
            self._validated_accounting_context(None, request, configuration)
            return None, ()
        row = self.connection.execute("SELECT context FROM accounting_contexts WHERE workflow_id=?", (workflow_id,)).fetchone()
        records = self.connection.execute("SELECT * FROM attempt_accounting WHERE workflow_id=?", (workflow_id,)).fetchall()
        if row is None:
            if records:
                raise ValueError("Attempt accounting lacks an accounting context")
            self._validated_accounting_context(None, request, configuration)
            return None, ()
        context = self._validated_accounting_context(_load(row[0]), request, configuration)
        registry = DeploymentRegistry.from_dict(configuration["registry_snapshot"])
        by_id = {attempt.attempt_id: attempt for attempt in attempts}
        raw_times = {item["attempt_id"]: _time(item["recorded_at"]) for item in self.connection.execute(
            "SELECT attempt_id,recorded_at FROM attempts WHERE workflow_id=?", (workflow_id,))}
        validated = {}
        from workflow.execution_accounting import validate_accounting_record

        for record in records:
            identity = record["attempt_id"]
            if identity not in by_id or identity in validated:
                raise ValueError("Unknown or duplicate accounting attempt")
            value = validate_accounting_record(_load(record["payload"]), by_id[identity], registry, context)
            if (value["workflow_id"] != workflow_id or value["attempt_id"] != identity
                    or _time(value["accounted_at"]) > _time(record["recorded_at"])
                    or _time(value["journal_recorded_at"]) < raw_times[identity]):
                raise ValueError("Stored accounting identity or journal chronology mismatch")
            validated[identity] = value
        return context, tuple(validated[attempt.attempt_id] for attempt in attempts if attempt.attempt_id in validated)

    def record_accounting(self, workflow_id, record, *, now):
        """Append enrichment only after the immutable raw attempt is durable.

        Accounting has no lease capability and never changes provider output,
        job state, retries, or planner revisions. Repeating identical evidence
        is idempotent; an attempt's accepted evidence cannot be overwritten.
        """
        now = _utc(now)
        with self._transaction():
            state = self.read(workflow_id)
            if state.accounting_context is None:
                raise ValueError("Workflow has no accounting context")
            identity = record.get("attempt_id") if isinstance(record, dict) else None
            attempt = next((item for item in state.attempts if item.attempt_id == identity), None)
            if attempt is None:
                raise ValueError("Accounting requires an already durable raw attempt")
            from workflow.execution_accounting import validate_accounting_record

            registry = DeploymentRegistry.from_dict(state.configuration["registry_snapshot"])
            value = validate_accounting_record(record, attempt, registry, state.accounting_context)
            raw = self.connection.execute("SELECT recorded_at FROM attempts WHERE attempt_id=?", (identity,)).fetchone()
            if (_time(value["accounted_at"]) > now
                    or _time(value["journal_recorded_at"]) < _time(raw[0])):
                raise ValueError("Accounting must follow raw journal persistence and precede its recording")
            payload = _dump(value)
            existing = self.connection.execute("SELECT workflow_id,payload FROM attempt_accounting WHERE attempt_id=?", (identity,)).fetchone()
            if existing is not None:
                if existing["workflow_id"] != workflow_id or existing["payload"] != payload:
                    raise ValueError("Attempt accounting is already associated with different facts")
                return
            self.connection.execute("INSERT INTO attempt_accounting VALUES (?,?,?,?)",
                (identity, workflow_id, payload, _stamp(now)))

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

    def _has_planning_tables(self):
        # Strict exports open old databases read-only without calling __init__.
        names = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('planning_meta','planning_contexts','planning_decisions')")}
        if not names:
            return False
        if len(names) != 3:
            raise ValueError("Incomplete planning metadata tables")
        versions = self.connection.execute("SELECT version FROM planning_meta").fetchall()
        if len(versions) != 1 or versions[0][0] != 1:
            raise ValueError("Unsupported planning metadata version")
        return True

    @staticmethod
    def _decision_winner(report):
        result = report.get("result")
        return result.get("winner") if isinstance(result, dict) else None

    @staticmethod
    def _planning_status(report):
        return "planning-failed" if report.get("record_type") == "PlanningPreparationFailure" else "infeasible"

    def _planning_records(self, workflow_id, request, configuration):
        if not self._has_planning_tables():
            return None, ()
        row = self.connection.execute("SELECT context FROM planning_contexts WHERE workflow_id=?", (workflow_id,)).fetchone()
        records = self.connection.execute("SELECT * FROM planning_decisions WHERE workflow_id=? ORDER BY revision", (workflow_id,)).fetchall()
        if row is None:
            if records:
                raise ValueError("Planning decisions lack a planning context")
            return None, ()
        context = self._validate_persisted_planning("context", row[0], request, configuration)
        decisions = []
        previous = request.arrived_at
        for index, record in enumerate(records, 1):
            if type(record["revision"]) is not int or record["revision"] != index:
                raise ValueError("Planning revisions must be contiguous")
            stamp = _time(record["recorded_at"])
            report = self._validate_persisted_planning("report", record["report"], request, configuration, context)
            token = record["snapshot_token"]
            if (stamp < previous or _time(report["now"]) > stamp
                    or (index == 1 and token is not None)
                    or (index > 1 and (not isinstance(token, str) or len(token) != 64
                        or any(c not in "0123456789abcdef" for c in token)))):
                raise ValueError("Invalid planning decision timestamp or snapshot token")
            previous = stamp
            self._validate_incumbent_link(report, [item["report"] for item in decisions])
            decisions.append({"revision": index, "recorded_at": record["recorded_at"],
                              "report": report, "snapshot_token": token})
        if not decisions:
            raise ValueError("Planning context lacks an initial decision")
        return context, tuple(decisions)

    @staticmethod
    def _validate_incumbent_link(report, previous_reports):
        """Bind an incumbent explanation to an actually retained earlier plan."""
        if report.get("record_type") != "PreparedWorkflowPlan":
            return
        previous = report["previous_plan_fingerprint"]
        if previous is None:
            return
        from data.quality_reviews import fingerprint
        from scheduler.joint import PlanResult

        prior = {}
        for saved in previous_reports:
            if saved.get("record_type") == "PreparedWorkflowPlan":
                winner = PlanResult.from_dict(saved["result"]).winner
                if winner is not None:
                    prior[fingerprint(winner.to_dict())] = winner
        if previous not in prior:
            raise ValueError("Decision incumbent has no retained previous winner")
        result = PlanResult.from_dict(report["result"])
        if result.winner is not None and any(note.startswith("Retained feasible incumbent:") for note in result.notes):
            candidates = {item.candidate.step_id: item.candidate for item in prior[previous].assignments}
            if any(item.candidate != candidates.get(item.candidate.step_id) for item in result.winner.assignments):
                raise ValueError("Retained incumbent differs from its previous winner")

    def _validate_persisted_planning(self, kind, raw, request, configuration, context=None):
        """Cache expensive semantic validation by exact durable input bytes.

        Strict JSON parsing and decision/state chronology run on every read.
        Changed bytes or caller context cannot reuse a previous validation, and
        copied results prevent a snapshot consumer from modifying the cache.
        Read-only export instances deliberately start with an empty cache too.
        """
        from workflow.planning_records import validate_context, validate_report

        value = _load(raw)
        key = (kind, raw, _dump(request.to_dict()), _dump(configuration), _dump(context))
        cache = getattr(self, "_planning_validation_cache", None)
        if cache is None:
            self._planning_validation_cache = cache = {}
        if key not in cache:
            validated = (validate_context(value, request=request, configuration=configuration)
                         if kind == "context" else validate_report(value, request=request,
                            configuration=configuration, context=context))
            if len(cache) >= 32:
                cache.pop(next(iter(cache)))
            cache[key] = deepcopy(validated)
        return deepcopy(cache[key])

    def _validated_planning_input(self, request, assignments, configuration, context, decision, now):
        context = self._validate_persisted_planning("context", _dump(context), request, configuration)
        decision = self._validate_persisted_planning("report", _dump(decision), request, configuration, context)
        now = _utc(now)
        if now < request.arrived_at or _time(decision["now"]) > now:
            raise ValueError("Decision cannot be installed before preparation or arrival")
        winner = self._decision_winner(decision)
        if winner is None:
            if assignments is not None:
                raise ValueError("An infeasible decision cannot install assignments")
            return {}, context, decision
        plans = self._plans(request, assignments)
        from scheduler.joint import PlanResult

        typed_winner = PlanResult.from_dict(decision["result"]).winner
        expected = {item.candidate.step_id: item.candidate for item in typed_winner.assignments}
        if plans != expected:
            raise ValueError("Installed assignments must exactly match the decision winner")
        if "registry_snapshot" in configuration:
            registry = DeploymentRegistry.from_dict(configuration["registry_snapshot"])
            for candidate in plans.values():
                deployment = registry.require_model(candidate.deployment_id, candidate.model_id)
                if candidate.region is not None and candidate.region != deployment.region:
                    raise ValueError("Planned region differs from the configured deployment")
        return plans, context, decision

    def _insert_jobs(self, request, plans, now):
        for step in request.steps:
            candidate = plans[step.step_id]
            self.connection.execute("""INSERT INTO jobs
                (workflow_id,step_id,candidate,planned_start,deadline,status) VALUES (?,?,?,?,?,?)""",
                (request.workflow_id, step.step_id, _dump(candidate.to_dict()), _stamp(candidate.planned_start),
                 _stamp(step.latest_finish), "scheduled"))
            self.connection.execute("INSERT INTO plan_history VALUES (?,?,?)",
                (request.workflow_id, _dump(candidate.to_dict()), _stamp(now)))

    def enqueue_planned(self, request, assignments, *, configuration, planning_context, decision, now,
                        accounting_context=None, submission=None):
        """Atomically retain a request and decision, including honest infeasibility."""
        if not isinstance(request, WorkflowRequest):
            raise ValueError("request must be WorkflowRequest")
        config = _configuration(configuration)
        accounting_context = self._validated_accounting_context(accounting_context, request, config)
        plans, context, report = self._validated_planning_input(
            request, assignments, config, planning_context, decision, now)
        if report.get("record_type") == "PreparedWorkflowPlan" and (
                report["accepted_outputs"] or report["committed_assignments"]):
            raise ValueError("Initial decisions cannot claim previously executed workflow steps")
        self._validate_incumbent_link(report, ())
        with self._transaction():
            existing = self.submission_result(submission)
            if existing is not None:
                return existing
            self.connection.execute("INSERT INTO workflows VALUES (?,?,?,NULL)",
                (request.workflow_id, _dump(request.to_dict()), _dump(config)))
            self._insert_accounting_context(request.workflow_id, accounting_context)
            self.connection.execute("INSERT INTO planning_contexts VALUES (?,?)", (request.workflow_id, _dump(context)))
            self.connection.execute("INSERT INTO planning_decisions VALUES (?,1,?,?,NULL)",
                (request.workflow_id, _stamp(now), _dump(report)))
            if plans:
                self._insert_jobs(request, plans, now)
            self._record_submission(submission, request.workflow_id)
        return request.workflow_id

    def _active_leases(self):
        return [dict(row) for row in self.connection.execute("""SELECT workflow_id,step_id,candidate,
            status,retry_index,lease_token,lease_owner,lease_until,dispatched_at FROM jobs
            WHERE lease_token IS NOT NULL AND status IN ('scheduled','running','needs-reconciliation','cancelled')
            ORDER BY workflow_id,step_id""")]

    def _planning_token(self, workflow_id):
        # Hash durable rows, rather than a lossy presentation snapshot: leases,
        # attempts, explicit retries and every decision revision invalidate it.
        target = {}
        for table, key in (("workflows", "id"), ("jobs", "workflow_id"), ("attempts", "workflow_id"),
                           ("plan_history", "workflow_id"), ("queue_events", "workflow_id"),
                           ("claim_history", "workflow_id"), ("planning_contexts", "workflow_id"),
                           ("planning_decisions", "workflow_id")):
            target[table] = [dict(row) for row in self.connection.execute(
                f"SELECT * FROM {table} WHERE {key}=? ORDER BY rowid", (workflow_id,))]
        return hashlib.sha256(_dump({"workflow": target, "active_leases": self._active_leases()}).encode("utf-8")).hexdigest()

    def planning_snapshot(self, workflow_id, *, now):
        from scheduler.joint import Reservation

        now = _utc(now)
        with self._transaction():
            self._recover(now)
            state = self.read(workflow_id)
            if state.planning_context is None:
                raise ValueError("Workflow has no planning context")
            finish = max(state.request.completion_deadline, now + timedelta(microseconds=1))
            reservations = tuple(Reservation(PlannedCandidate.from_dict(_load(row["candidate"])).deployment_id,
                now, finish) for row in self._active_leases() if row["workflow_id"] != workflow_id)
            return PlanningSnapshot(state, self._planning_token(workflow_id), reservations, state.decisions[-1]["revision"])

    def planned_workflow_ids(self):
        if not self._has_planning_tables():
            return ()
        return tuple(row[0] for row in self.connection.execute(
            "SELECT workflow_id FROM planning_contexts ORDER BY rowid"))

    def install_plan(self, workflow_id, assignments, decision, *, expected_token, now,
                     worker_id=None, lease_seconds=60):
        """Compare-and-swap one complete decision, optionally claiming due ready work."""
        now = _utc(now)
        if worker_id is not None:
            _text(worker_id, "worker_id")
            _expiry(now, lease_seconds)
        with self._transaction():
            self._recover(now)
            state = self.read(workflow_id)
            if state.planning_context is None:
                raise ValueError("Workflow has no planning context")
            if expected_token != self._planning_token(workflow_id):
                raise PlanningConflict("Workflow or shared occupancy changed during planning")
            if state.cancelled_at is not None or state.status in {"completed", "failed", "deadline_missed", "needs-reconciliation"}:
                raise PlanningConflict("Workflow is not eligible for a new decision")
            if any(row["workflow_id"] == workflow_id for row in self._active_leases()):
                raise PlanningConflict("An active claim freezes the installed decision")
            plans, _, report = self._validated_planning_input(state.request, assignments, state.configuration,
                                                             state.planning_context, decision, now)
            if _time(report["now"]) < _time(state.decisions[-1]["report"]["now"]):
                raise PlanningConflict("A new decision cannot precede the previous preparation")
            if report.get("record_type") == "PreparedWorkflowPlan":
                self._validate_plan_facts(state, report, now)
            self._validate_incumbent_link(report, [item["report"] for item in state.decisions])
            revision = state.decisions[-1]["revision"] + 1
            if plans and not state.jobs:
                if now >= state.request.completion_deadline:
                    raise PlanningConflict("Workflow completion deadline has expired")
                self._insert_jobs(state.request, plans, now)
            elif plans:
                for job in state.jobs:
                    candidate = plans[job.step_request.step_id]
                    if candidate == job.candidate:
                        continue
                    if job.status != "scheduled" or job.dispatched_at is not None:
                        raise PlanningConflict("Dispatched or terminal assignments cannot change")
                    if now >= job.step_request.latest_finish:
                        raise PlanningConflict("Step deadline has expired")
                    self.connection.execute("UPDATE jobs SET candidate=?,planned_start=? WHERE workflow_id=? AND step_id=?",
                        (_dump(candidate.to_dict()), _stamp(candidate.planned_start), workflow_id, candidate.step_id))
                    self.connection.execute("INSERT INTO plan_history VALUES (?,?,?)",
                        (workflow_id, _dump(candidate.to_dict()), _stamp(now)))
            self.connection.execute("INSERT INTO planning_decisions VALUES (?,?,?,?,?)",
                (workflow_id, revision, _stamp(now), _dump(report), expected_token))
            if not plans or worker_id is None:
                return None
            outputs = state.completed_outputs()
            dispatchable = set(report["dispatchable_step_ids"])
            for step in state.request.steps:
                if step.step_id not in dispatchable or not set(step.dependencies) <= outputs.keys():
                    continue
                row = self.connection.execute("SELECT * FROM jobs WHERE workflow_id=? AND step_id=?",
                                              (workflow_id, step.step_id)).fetchone()
                candidate = plans[step.step_id]
                if (row["status"] != "scheduled" or row["lease_token"] is not None
                        or candidate.planned_start > now or now >= step.latest_finish
                        or self._occupied_slots(candidate.deployment_id) >= self._capacity(candidate, state.configuration)):
                    continue
                return self._reserve(workflow_id, step, candidate, state.configuration, row["retry_index"],
                    outputs, worker_id, now, lease_seconds, planning_revision=revision)
        return None

    def _validate_plan_facts(self, state, report, now):
        from collections import Counter
        from scheduler.joint import CommittedAssignment, Reservation

        if report["accepted_outputs"] != state.completed_outputs():
            raise PlanningConflict("Decision inputs differ from accepted durable outputs")
        expected = {}
        for job in state.jobs:
            if job.status != "completed":
                continue
            attempt = next((item for item in reversed(state.attempts)
                if item.planned_candidate == job.candidate and item.retry_index == job.retry_index
                and item.status == "completed" and item.output == job.output), None)
            if attempt is None or attempt.actual_start is None or job.finished_at is None:
                raise PlanningConflict("Completed work lacks accepted execution facts")
            expected[job.step_request.step_id] = CommittedAssignment(
                job.candidate, attempt.actual_start, job.finished_at)
        committed = {item.candidate.step_id: item for item in
                     (CommittedAssignment.from_dict(value) for value in report["committed_assignments"])}
        if committed != expected:
            raise PlanningConflict("Decision committed assignments differ from durable execution")
        if self._decision_winner(report) is None:
            return
        # A current token is not permission to omit reservations from the
        # corresponding report. Each active external call occupies its slot
        # conservatively through this workflow's remaining horizon.
        active = Counter(PlannedCandidate.from_dict(_load(row["candidate"])).deployment_id
            for row in self._active_leases() if row["workflow_id"] != state.request.workflow_id)
        reported = Counter()
        for value in report["reservations"]:
            reservation = Reservation.from_dict(value)
            if (reservation.start > _time(report["now"])
                    or reservation.finish < state.request.completion_deadline):
                raise PlanningConflict("Decision reservation does not cover the conservative queue horizon")
            reported[reservation.deployment_id] += reservation.slots
        if reported != active:
            raise PlanningConflict("Decision reservations differ from current shared occupancy")

    def _reserve(self, workflow_id, step, candidate, configuration, retry_index, outputs,
                 worker_id, now, lease_seconds, *, planning_revision=None):
        token, expiry = uuid.uuid4().hex, _expiry(now, lease_seconds)
        self.connection.execute("UPDATE jobs SET lease_token=?,lease_owner=?,lease_until=? WHERE workflow_id=? AND step_id=?",
            (token, worker_id, _stamp(expiry), workflow_id, step.step_id))
        self.connection.execute("INSERT INTO claim_history VALUES (?,?,?,?,?,?,?,NULL)",
            (token, workflow_id, step.step_id, worker_id, _dump(candidate.to_dict()), retry_index, _stamp(now)))
        return Claim(workflow_id, step, candidate, worker_id, token, expiry,
                     {key: outputs[key] for key in step.dependencies}, configuration, retry_index, planning_revision)

    def release_undispatched(self, claim, *, now):
        with self._transaction():
            row = self._owned(claim, now)
            if row["status"] != "scheduled" or row["dispatched_at"] is not None:
                raise LeaseError("Only an owned undispatched claim can be released")
            self.connection.execute("UPDATE jobs SET lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE workflow_id=? AND step_id=?",
                                    (claim.workflow_id, claim.candidate.step_id))
            self.connection.execute("INSERT INTO queue_events VALUES (?,?,?,?)",
                (claim.workflow_id, claim.candidate.step_id, "released_undispatched", _stamp(now)))

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
        planning_context, decisions = self._planning_records(workflow_id, request, configuration)
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
        if {job.step_request.step_id for job in jobs} != set(steps) and not (
                not jobs and planning_context is not None and decisions
                and self._decision_winner(decisions[-1]["report"]) is None):
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
        status = _workflow_status({job.step_request.step_id: job.status for job in jobs},
            [step.step_id for step in request.steps if step.required_for_response], row["cancelled_at"],
            decisions[-1]["report"] if decisions else None)
        history = tuple(PlannedCandidate.from_dict(_load(record[0])) for record in self.connection.execute(
            "SELECT candidate FROM plan_history WHERE workflow_id=? ORDER BY rowid", (workflow_id,)))
        if any(candidate.step_id not in steps for candidate in history):
            raise ValueError("Stored plan history identity mismatch")
        if any(attempt.planned_candidate not in history for attempt in attempts):
            raise ValueError("Stored attempt has no corresponding plan history")
        if planning_context is not None and jobs:
            from scheduler.joint import PlanResult

            last_winner = next((decision["report"] for decision in reversed(decisions)
                                if self._decision_winner(decision["report"]) is not None), None)
            if last_winner is None:
                raise ValueError("Planned jobs lack a feasible installed decision")
            selected = {item.candidate.step_id: item.candidate
                        for item in PlanResult.from_dict(last_winner["result"]).winner.assignments}
            if any(job.candidate != selected.get(job.step_request.step_id) for job in jobs):
                raise ValueError("Current planned assignments differ from the installed decision")
        events = tuple({"step_id": event[0], "kind": event[1], "recorded_at": _time(event[2])}
            for event in self.connection.execute("SELECT step_id,kind,recorded_at FROM queue_events WHERE workflow_id=? ORDER BY rowid", (workflow_id,)))
        accounting_context, accounting = self._accounting_records(workflow_id, request, configuration, attempts)
        return WorkflowSnapshot(request, configuration, status,
                                tuple(jobs), tuple(attempts), _time(row["cancelled_at"]), history, events,
                                planning_context, decisions, accounting_context, accounting)

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
                AND j.deadline>? AND w.cancelled_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM planning_contexts p WHERE p.workflow_id=j.workflow_id)
                ORDER BY j.planned_start,j.workflow_id,j.rowid""",
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
            if state.planning_context is not None:
                if (claim.planning_revision is None or claim.planning_revision != state.decisions[-1]["revision"]):
                    raise LeaseError("Planned dispatch requires the latest decision revision")
                report = state.decisions[-1]["report"]
                context = next((item for item in report.get("contexts", ()) if item["step_id"] == original.step_id), None)
                if (original.step_id not in report.get("dispatchable_step_ids", ()) or context is None
                        or context.get("input_mode") != "materialized" or rendered_step is None
                        or context.get("materialized_step") != rendered_step.to_dict()
                        or context.get("input_fingerprint") != input_fingerprint
                        or context.get("source_ids") != list(source_ids)):
                    raise LeaseError("Planned dispatch must match its materialized decision context")
                if (now - _time(report["now"])).total_seconds() > state.planning_context["max_decision_age_seconds"]:
                    raise LeaseError("Planned decision expired before dispatch")
                health = next((item for item in report["health_snapshot"]
                               if item["deployment_id"] == claim.candidate.deployment_id), None)
                if health is None or (now - _time(health["checked_at"])).total_seconds() > report["health_max_age_seconds"]:
                    raise LeaseError("Deployment health expired before dispatch")
            elif claim.planning_revision is not None:
                raise LeaseError("Fixed dispatch cannot carry a planning revision")
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
            if state.planning_context is not None:
                raise ValueError("Planned workflows require atomic install_plan")
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
