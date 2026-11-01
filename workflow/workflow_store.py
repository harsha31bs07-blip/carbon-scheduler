"""Atomic state for one connected workflow, with explicit restart boundaries.

This is a single-run journal, not a durable worker queue. Hold ``exclusive()``
for the entire run. A process interrupted after ``begin_step`` may already have
sent a provider request; loading that state requires reconciliation and never
automatically repeats the request. Completed output and its attempt share one
atomic JSON replacement so they cannot become separated on restart.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from shared.scheduling import ExecutionAttempt, SCHEMA_VERSION, StepRequest, WorkflowRequest


STATUSES = frozenset({
    "pending", "running", "response_ready", "completed", "failed",
    "deadline_missed", "needs-reconciliation",
})
STEP_STATUSES = frozenset({"pending", "running", "completed", "failed"})
_STATE_FIELDS = {
    "schema_version", "record_type", "store_version", "request",
    "registry_fingerprint", "workload_fingerprint", "execution_config",
    "status", "error_code", "steps", "attempts",
}
_STEP_FIELDS = {
    "status", "rendered_request", "source_ids", "lookup_seconds",
    "input_fingerprint", "attempt_id", "output_valid", "validation_error",
}


class WorkflowLockedError(RuntimeError):
    """An active or stale lock must be resolved explicitly by the operator."""


class WorkflowReconciliationError(RuntimeError):
    """A previous dispatch may have run and cannot be retried automatically."""


def _text(value, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _seconds(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite nonnegative seconds")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite nonnegative seconds")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object field")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Nonfinite JSON values are unsupported")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object,
                          parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid workflow state JSON") from exc


def _write(path: Path, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".workflow-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _config(value) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "deployment_id", "model_id", "timeout_seconds", "runner_version",
    }:
        raise ValueError("execution_config requires only deployment, model, timeout and runner version")
    for name in ("deployment_id", "model_id", "runner_version"):
        _text(value[name], name)
    _seconds(value["timeout_seconds"], "timeout_seconds")
    if value["timeout_seconds"] == 0:
        raise ValueError("timeout_seconds must be positive")


def _rendered_matches(original: StepRequest, rendered: StepRequest) -> None:
    # Prompt and token count change when dependency outputs are materialized;
    # routing constraints, deadlines and output limits remain the request's own.
    mutable = {"prompt", "input_tokens", "input_size_source"}
    before, after = original.to_dict(), rendered.to_dict()
    if any(before[key] != after[key] for key in before.keys() - mutable):
        raise ValueError("Rendered step changed immutable request constraints")


def _known_deadline_violation(value: dict) -> bool:
    """Audit recorded facts only; elapsed time after a crash is not a finish time."""
    for item in value["attempts"]:
        attempt = ExecutionAttempt.from_dict(item)
        if attempt.error == "step-deadline-exceeded":
            return True
        rendered_value = value["steps"][attempt.planned_candidate.step_id]["rendered_request"]
        if rendered_value is not None and attempt.actual_finish is not None:
            rendered = StepRequest.from_dict(rendered_value)
            if attempt.actual_finish > rendered.latest_finish:
                return True
    return False


def _validate(value: dict) -> WorkflowRequest:
    if not isinstance(value, dict) or set(value) != _STATE_FIELDS:
        raise ValueError("Invalid workflow state fields")
    if (type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION
            or value["record_type"] != "WorkflowState"
            or type(value["store_version"]) is not int or value["store_version"] != 1):
        raise ValueError("Unsupported workflow state schema")
    request = WorkflowRequest.from_dict(value["request"])
    for name in ("registry_fingerprint", "workload_fingerprint"):
        _text(value[name], name)
    _config(value["execution_config"])
    if not isinstance(value["status"], str) or value["status"] not in STATUSES:
        raise ValueError("Unsupported workflow status")
    if value["error_code"] is not None and (
            not isinstance(value["error_code"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", value["error_code"])):
        raise ValueError("error_code must be a safe machine code")
    by_id = {step.step_id: step for step in request.steps}
    if not isinstance(value["steps"], dict) or set(value["steps"]) != set(by_id):
        raise ValueError("Workflow state steps must match the immutable request")
    if any(not isinstance(step, dict) or set(step) != _STEP_FIELDS
           for step in value["steps"].values()):
        raise ValueError("Invalid persisted step fields")
    if not isinstance(value["attempts"], list):
        raise ValueError("Workflow attempts must be an array")
    attempts = [ExecutionAttempt.from_dict(item) for item in value["attempts"]]
    ids = [attempt.attempt_id for attempt in attempts]
    if len(ids) != len(set(ids)):
        raise ValueError("Workflow attempt IDs must be unique")
    latest = {}
    for attempt in attempts:
        step_id = attempt.planned_candidate.step_id
        if attempt.workflow_id != request.workflow_id or step_id not in by_id:
            raise ValueError("Attempt belongs to a different workflow or step")
        config = value["execution_config"]
        if config is not None and (
                attempt.planned_candidate.model_id != config["model_id"]
                or attempt.planned_candidate.deployment_id != config["deployment_id"]):
            raise ValueError("Attempt changed the saved model or deployment selection")
        if (attempt.executed_deployment_id is not None
                and attempt.executed_deployment_id != attempt.planned_candidate.deployment_id):
            raise ValueError("Attempt executed on a different deployment")
        if attempt.status not in {"completed", "failed", "cancelled", "needs-reconciliation"}:
            raise ValueError("Only terminal attempts may be journaled")
        if step_id in latest:
            previous = latest[step_id]
            if (previous.actual_finish is not None and attempt.actual_start is not None
                    and attempt.actual_start < previous.actual_finish):
                raise ValueError("Step attempt chronology is inconsistent")
        latest[step_id] = attempt
    running = 0
    for step_id, step in value["steps"].items():
        if not isinstance(step, dict) or set(step) != _STEP_FIELDS:
            raise ValueError("Invalid persisted step fields")
        status = step["status"]
        if not isinstance(status, str) or status not in STEP_STATUSES:
            raise ValueError("Invalid persisted step status")
        if not isinstance(step["source_ids"], list) or any(
                not isinstance(source, str) or not source.strip() for source in step["source_ids"]):
            raise ValueError("source_ids must be an array of nonempty strings")
        if len(step["source_ids"]) != len(set(step["source_ids"])):
            raise ValueError("source_ids must be unique")
        _seconds(step["lookup_seconds"], "lookup_seconds")
        if step["validation_error"] is not None:
            _text(step["validation_error"], "validation_error")
        if step["output_valid"] is not None and type(step["output_valid"]) is not bool:
            raise ValueError("output_valid must be boolean or unknown")
        rendered = None
        if step["rendered_request"] is not None:
            rendered = StepRequest.from_dict(step["rendered_request"])
            _rendered_matches(by_id[step_id], rendered)
            _text(step["input_fingerprint"], "input_fingerprint")
        elif step["input_fingerprint"] is not None:
            raise ValueError("Input identity requires its rendered step")
        if status == "pending":
            if (rendered is not None or step["source_ids"] or step["lookup_seconds"] != 0
                    or step["attempt_id"] is not None or step["output_valid"] is not None
                    or step["validation_error"] is not None or step_id in latest):
                raise ValueError("Pending step contains execution facts")
            continue
        if rendered is None:
            raise ValueError("Started steps require their rendered request")
        for dependency in rendered.dependencies:
            if value["steps"][dependency].get("status") != "completed":
                raise ValueError("Started step has an incomplete dependency")
        if status == "running":
            running += 1
            if any(step[name] is not None for name in ("attempt_id", "output_valid", "validation_error")):
                raise ValueError("Running step contains terminal facts")
            continue
        attempt = latest.get(step_id)
        if attempt is None or step["attempt_id"] != attempt.attempt_id:
            raise ValueError("Terminal step must reference its latest attempt")
        if (attempt.output_limit_tokens != rendered.output_limit_tokens
                or attempt.enable_thinking != rendered.enable_thinking):
            raise ValueError("Attempt changed the rendered generation settings")
        if attempt.actual_start is not None and attempt.actual_start < rendered.earliest_start:
            raise ValueError("Attempt started before the workflow step window")
        for dependency in rendered.dependencies:
            predecessor = latest.get(dependency)
            if (predecessor is not None and predecessor.actual_finish is not None
                    and attempt.actual_start is not None and attempt.actual_start < predecessor.actual_finish):
                raise ValueError("Attempt started before its dependency completed")
        if status == "completed":
            if (attempt.status != "completed" or not attempt.output.strip()
                    or step["output_valid"] is not True or step["validation_error"] is not None):
                raise ValueError("Completed output must have a successful, validated attempt")
        elif step["output_valid"] is not False:
            raise ValueError("Failed steps cannot expose accepted output")
    if running > 1:
        raise ValueError("Single-run store cannot contain concurrent running steps")
    completed = {key for key, step in value["steps"].items() if step["status"] == "completed"}
    if value["status"] == "completed" and completed != set(by_id):
        raise ValueError("Completed workflow has incomplete steps")
    if value["status"] == "response_ready" and not {
            step.step_id for step in request.steps if step.required_for_response} <= completed:
        raise ValueError("Response-ready workflow has incomplete response steps")
    if value["status"] == "pending" and (attempts or running or completed):
        raise ValueError("Pending workflow contains execution facts")
    return request


class WorkflowStore:
    """One workflow's immutable inputs, rendered prompts, attempts and outputs."""

    def __init__(self, directory: Path, value: dict):
        self.directory = directory
        self.path = directory / "workflow.json"
        self._lock_path = directory / ".workflow.lock"
        self._lock_token = None
        self._request = _validate(value)
        self._value = value

    @classmethod
    def create(cls, directory, request: WorkflowRequest, registry_fingerprint: str,
               workload_fingerprint: str, execution_config: dict | None = None):
        if not isinstance(request, WorkflowRequest):
            raise ValueError("request must be a WorkflowRequest")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        value = {
            "schema_version": SCHEMA_VERSION, "record_type": "WorkflowState", "store_version": 1,
            "request": request.to_dict(), "registry_fingerprint": registry_fingerprint,
            "workload_fingerprint": workload_fingerprint, "execution_config": copy.deepcopy(execution_config),
            "status": "pending", "error_code": None, "steps": {
                step.step_id: {"status": "pending", "rendered_request": None, "source_ids": [],
                               "lookup_seconds": 0.0, "input_fingerprint": None, "attempt_id": None,
                               "output_valid": None, "validation_error": None}
                for step in request.steps
            }, "attempts": [],
        }
        store = cls(directory, value)
        with store._lock():
            if store.path.exists():
                raise FileExistsError("Workflow state already exists; load it to resume")
            _write(store.path, value)
        return store

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        store = cls(directory, _read(directory / "workflow.json"))
        store._recover_view()
        return store

    @property
    def request(self) -> WorkflowRequest:
        return self._request

    def state(self) -> dict:
        """Return a defensive copy, including the safe execution selection."""
        return copy.deepcopy(self._value)

    def completed_outputs(self) -> dict[str, str]:
        latest = {item["planned_candidate"]["step_id"]: item for item in self._value["attempts"]}
        return {key: latest[key]["output"] for key, step in self._value["steps"].items()
                if step["status"] == "completed"}

    def _recover_view(self) -> None:
        if any(step["status"] == "running" for step in self._value["steps"].values()):
            self._value["status"] = "needs-reconciliation"
        elif _known_deadline_violation(self._value):
            # Older journals may have been interrupted between recording their
            # terminal attempt and marking the workflow's missed deadline.
            # Derive the safe view without rewriting historical evidence.
            self._value["status"] = "deadline_missed"
            self._value["error_code"] = "step-deadline-exceeded"

    @contextmanager
    def _lock(self):
        if self._lock_token is not None:
            raise WorkflowLockedError("Workflow lock is already held by this store")
        token = uuid.uuid4().hex
        try:
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkflowLockedError("Workflow is locked; stale locks require explicit reconciliation") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            self._lock_token = token
            yield
        finally:
            self._lock_token = None
            # Never break a replacement lock owned by another process.
            if self._lock_path.exists() and self._lock_path.read_text(encoding="ascii") == token:
                self._lock_path.unlink()

    @contextmanager
    def exclusive(self):
        """Hold the lock throughout inference; refresh after acquiring it.

        Lock files are deliberately not expired automatically: both active and
        stale owners must be treated as potentially having dispatched inference.
        """
        with self._lock():
            value = _read(self.path)
            self._request = _validate(value)
            self._value = value
            self._recover_view()
            yield self

    def _mutate(self, value: dict) -> None:
        if self._lock_token is None:
            raise WorkflowLockedError("Hold store.exclusive() before mutating workflow state")
        _validate(value)
        _write(self.path, value)
        self._value = value

    def begin_step(self, rendered: StepRequest, *, source_ids: tuple[str, ...],
                   lookup_seconds: float, input_fingerprint: str) -> None:
        if self._value["status"] == "needs-reconciliation":
            raise WorkflowReconciliationError("Interrupted workflow requires explicit reconciliation")
        if not isinstance(rendered, StepRequest) or rendered.step_id not in self._value["steps"]:
            raise ValueError("Rendered request must be a known workflow step")
        if not isinstance(source_ids, tuple):
            raise ValueError("source_ids must be a tuple")
        value = self.state()
        step = value["steps"][rendered.step_id]
        if value["status"] not in {"pending", "running", "response_ready", "failed"}:
            raise ValueError("Workflow status does not permit a new dispatch")
        if step["status"] not in {"pending", "failed"}:
            raise ValueError("Only pending or explicitly failed steps can begin")
        if step["status"] == "failed" and (
                step["rendered_request"] != rendered.to_dict()
                or step["source_ids"] != list(source_ids)
                or step["input_fingerprint"] != input_fingerprint):
            raise ValueError("An explicit retry must preserve the failed step's rendered input identity")
        if any(item["status"] == "running" for item in value["steps"].values()):
            raise WorkflowReconciliationError("A workflow step is already running")
        step.update({"status": "running", "rendered_request": rendered.to_dict(),
                     "source_ids": list(source_ids), "lookup_seconds": lookup_seconds,
                     "input_fingerprint": input_fingerprint, "attempt_id": None,
                     "output_valid": None, "validation_error": None})
        value["status"] = "running"
        value["error_code"] = None
        self._mutate(value)

    def finish_step(self, attempt: ExecutionAttempt, *, output_valid: bool = True,
                    validation_error: str | None = None,
                    deadline_exceeded: bool = False) -> None:
        """Atomically retain terminal evidence and any known missed deadline.

        ``deadline_exceeded`` lets the runner include observed validation time.
        Provider finish timestamps and explicit preflight expiry are audited
        independently. No inference is made about an unobserved commit instant
        if the process stops during or immediately after the atomic replacement.
        """
        if not isinstance(attempt, ExecutionAttempt):
            raise ValueError("attempt must be an ExecutionAttempt")
        if self._value["status"] == "needs-reconciliation":
            raise WorkflowReconciliationError("Interrupted workflow requires explicit reconciliation")
        value = self.state()
        step_id = attempt.planned_candidate.step_id
        if step_id not in value["steps"] or value["steps"][step_id]["status"] != "running":
            raise ValueError("Attempt must finish its currently running step")
        if type(output_valid) is not bool:
            raise ValueError("output_valid must be boolean")
        if type(deadline_exceeded) is not bool:
            raise ValueError("deadline_exceeded must be boolean")
        if validation_error is not None:
            _text(validation_error, "validation_error")
        accepted = output_valid and attempt.status == "completed"
        if accepted and validation_error is not None:
            raise ValueError("Accepted output cannot have a validation error")
        step = value["steps"][step_id]
        step.update({"status": "completed" if accepted else "failed", "attempt_id": attempt.attempt_id,
                     "output_valid": accepted, "validation_error": validation_error})
        value["attempts"].append(attempt.to_dict())
        if not accepted:
            value["status"] = "needs-reconciliation" if attempt.status == "needs-reconciliation" else "failed"
        if deadline_exceeded or _known_deadline_violation(value):
            value["status"] = "deadline_missed"
            value["error_code"] = "step-deadline-exceeded"
        self._mutate(value)

    def mark_status(self, status: str, *, error_code: str | None = None) -> None:
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("Unsupported workflow status")
        if self._value["status"] == "needs-reconciliation" and status != "needs-reconciliation":
            raise WorkflowReconciliationError("Interrupted workflow requires explicit reconciliation")
        if self._value["status"] == "completed" and status != "completed":
            raise ValueError("Completed workflows cannot be reopened")
        if self._value["status"] == "deadline_missed" and status != "deadline_missed":
            raise ValueError("Deadline-missed workflows cannot be reopened")
        if any(step["status"] == "running" for step in self._value["steps"].values()) and status != "needs-reconciliation":
            raise ValueError("Finish or reconcile the running step before changing workflow status")
        value = self.state()
        value["status"] = status
        value["error_code"] = error_code
        self._mutate(value)
