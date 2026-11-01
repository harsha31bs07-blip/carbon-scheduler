"""Persistent local worker: submit, inspect, cancel and execute due SQLite jobs.

Fixed submission verifies execution mechanics. It makes no optimization claim:
unknown quality and resource estimates remain unknown in the saved candidates.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import timedelta
import json
import math
from pathlib import Path
import threading
import time
from uuid import uuid4

from shared.deployments import DeploymentRegistry, load_deployments
from shared.scheduling import MetricEstimate, PlannedCandidate, StepEstimates, SystemClock
from workflow.connected import _now, workload_fingerprint
from workflow.dispatch import dispatch_planned_candidate
from workflow.queue_store import LeaseError, QueueStore
from workflow.workload import KnowledgeBase, build_workflow_request, render_step, validate_step_output


WORKER_VERSION = "sqlite-worker-v1"


def _seconds(value, name, *, positive=False):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0 and (not positive or value > 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite {'positive' if positive else 'nonnegative'} seconds")


def submit_fixed(store, registry, kb, ticket, *, deployment_id="local-ollama",
                 model_id="local-llama3.2", clock=None, workflow_id=None,
                 summary_delay_seconds=0, response_seconds=300, completion_seconds=600,
                 timeout_seconds=120):
    """Persist a real input and explicit assignment without dispatching anything."""
    _seconds(summary_delay_seconds, "summary_delay_seconds")
    _seconds(timeout_seconds, "timeout_seconds", positive=True)
    if timeout_seconds > 300:
        raise ValueError("Timeout must be at most 300 seconds")
    deployment = registry.require_model(deployment_id, model_id)
    request = build_workflow_request(
        ticket, workflow_id=workflow_id or uuid4().hex, arrived_at=_now(clock or SystemClock()), kb=kb,
        response_budget_seconds=response_seconds, completion_budget_seconds=completion_seconds,
        enable_thinking=False if deployment.provider == "ollama" else None,
    )
    if summary_delay_seconds >= (request.completion_deadline - request.arrived_at).total_seconds():
        raise ValueError("Summary delay must precede the completion deadline")
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    estimates = StepEstimates(unknown("s"), unknown("ratio"), unknown("USD"), unknown("kWh"), unknown("gCO2e"))
    candidates = tuple(PlannedCandidate(
        uuid4().hex, step.step_id, model_id, deployment_id,
        step.earliest_start + timedelta(seconds=summary_delay_seconds if step.step_id == "summarize" else 0),
        estimates, region=deployment.region,
    ) for step in request.steps)
    return store.enqueue(request, candidates, configuration={
        "registry_snapshot": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint(),
        "kb_fingerprint": kb.fingerprint(), "workload_fingerprint": workload_fingerprint(request, kb),
        "worker_version": WORKER_VERSION, "policy_version": "fixed-explicit-v1",
        "experiment_mode": "live", "timeout_seconds": timeout_seconds,
        "max_retries": 3,
    })


class _Heartbeat:
    """Renew using a separate SQLite connection; never redispatch on lease loss."""

    def __init__(self, path, claim, clock, lease_seconds, enabled):
        self.path, self.claim, self.clock = path, claim, clock
        self.lease_seconds, self.enabled = lease_seconds, enabled
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        if self.enabled:
            self.thread = threading.Thread(target=self._run, daemon=True, name="queue-lease-heartbeat")
            self.thread.start()
        return self

    def _run(self):
        interval = min(self.lease_seconds / 3, 10)
        store = None
        try:
            store = QueueStore(self.path)
            while not self.stop.wait(interval):
                self.claim = store.renew(self.claim, now=_now(self.clock), lease_seconds=self.lease_seconds)
        except Exception:
            # An in-flight call cannot safely be repeated or cancelled here.
            # complete() verifies ownership; expiry remains visible on recovery.
            return
        finally:
            if store is not None:
                store.close()

    def __exit__(self, *exc):
        self.stop.set()
        if self.thread:
            self.thread.join()


def _retain_orphan(store, claim, attempt):
    directory = store.path.parent / (store.path.name + ".orphan-attempts")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (uuid4().hex + ".json")
    payload = {"schema_version": 1, "record_type": "UnreconciledWorkerAttempt",
               "workflow_id": claim.workflow_id, "worker_id": claim.worker_id,
               "lease_token": claim.lease_token, "attempt": attempt.to_dict()}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def work_once(store, *, worker_id, registry=None, kb=None, clock=None, dispatcher=None,
              lease_seconds=60, heartbeat=True):
    """Claim one due job, persist its prompt, dispatch once and atomically save output.

    Unexpected interruption after mark_running leaves ambiguous durable work.
    Recovery never automatically invokes that provider call again.
    """
    _seconds(lease_seconds, "lease_seconds", positive=True)
    if type(heartbeat) is not bool:
        raise ValueError("heartbeat must be boolean")
    active_clock = clock or SystemClock()
    claim = store.claim(worker_id, now=_now(active_clock), lease_seconds=lease_seconds)
    if claim is None:
        return {"status": "idle"}
    try:
        config = claim.configuration
        if (config.get("worker_version") != WORKER_VERSION
                or config.get("experiment_mode") != "live"):
            raise ValueError("Unsupported saved worker configuration")
        saved_registry = DeploymentRegistry.from_dict(config["registry_snapshot"])
        active_registry = registry or saved_registry
        active_kb = kb or KnowledgeBase.load()
        state = store.read(claim.workflow_id)
        if (active_registry.config_fingerprint() != config["registry_fingerprint"]
                or active_kb.fingerprint() != config["kb_fingerprint"]
                or workload_fingerprint(state.request, active_kb) != config["workload_fingerprint"]):
            raise ValueError("Saved execution inputs differ")
        timeout = config["timeout_seconds"]
        _seconds(timeout, "timeout_seconds", positive=True)
        if timeout > 300:
            raise ValueError("Timeout must be at most 300 seconds")
        rendered = render_step(state.request, claim.candidate.step_id, state.completed_outputs(), kb=active_kb)
        ready_at = max([state.request.arrived_at] + [job.finished_at for job in state.jobs
            if job.step_request.step_id in rendered.step.dependencies])
    except (ValueError, KeyError, OSError):
        try:
            store.fail_before_dispatch(claim, "worker-input-configuration-invalid", now=_now(active_clock))
        except LeaseError:
            store.recover(now=_now(active_clock))
        return {"status": "preflight-failed", "workflow_id": claim.workflow_id, "step_id": claim.candidate.step_id}
    try:
        claim = store.mark_running(claim, now=_now(active_clock), rendered_step=rendered.step,
            source_ids=rendered.source_ids, input_fingerprint=rendered.input_fingerprint,
            lookup_seconds=rendered.lookup_seconds)
    except LeaseError:
        store.recover(now=_now(active_clock))
        return {"status": "claim-lost", "workflow_id": claim.workflow_id, "step_id": claim.candidate.step_id}
    with _Heartbeat(store.path, claim, active_clock, lease_seconds, heartbeat):
        attempt = (dispatcher or dispatch_planned_candidate)(
            claim.candidate, active_registry, rendered.step.prompt, workflow_id=claim.workflow_id,
            clock=active_clock, timeout_seconds=timeout, output_limit_tokens=rendered.step.output_limit_tokens,
            enable_thinking=rendered.step.enable_thinking, latest_finish=rendered.step.latest_finish)
        attempt = replace(attempt, retry_index=claim.retry_index,
            wait_seconds=max(0.0, (attempt.actual_start - ready_at).total_seconds()) if attempt.actual_start else None)
        valid, error = True, None
        if attempt.status == "completed":
            if attempt.completion_reason == "length":
                valid, error = False, "step-output-truncated"
            elif attempt.completion_reason not in {None, "stop"}:
                valid, error = False, "step-generation-incomplete"
            else:
                try:
                    validate_step_output(claim.candidate.step_id, attempt.output, source_ids=rendered.source_ids)
                except ValueError:
                    valid, error = False, "step-output-invalid"
        try:
            store.complete(claim, attempt, now=_now(active_clock), output_valid=valid, validation_error=error)
        except Exception as failure:
            if isinstance(failure, LeaseError):
                try:
                    store.archive_late_attempt(claim, attempt, now=_now(active_clock))
                    return {"status": "late-attempt-archived", "workflow_id": claim.workflow_id,
                            "step_id": claim.candidate.step_id}
                except Exception:
                    pass
            orphan = _retain_orphan(store, claim, attempt)
            return {"status": "needs-reconciliation", "workflow_id": claim.workflow_id,
                    "step_id": claim.candidate.step_id, "orphan_attempt": orphan}
    return {"status": store.read(claim.workflow_id).status, "workflow_id": claim.workflow_id,
            "step_id": claim.candidate.step_id, "attempt_id": attempt.attempt_id}


def snapshot_summary(snapshot):
    outputs = snapshot.completed_outputs()
    return {"workflow_id": snapshot.request.workflow_id, "status": snapshot.status,
            "execution_mode": "fixed-durable" if snapshot.request.policy == "fixed" else "planned-durable",
            "attempt_count": len(snapshot.attempts),
            "response": outputs.get("draft"), "summary": outputs.get("summarize"),
            "jobs": [{"step_id": job.step_request.step_id, "status": job.status,
                      "planned_start": job.candidate.planned_start.isoformat(),
                      "dispatched_at": job.dispatched_at.isoformat() if job.dispatched_at else None,
                      "error_code": job.error_code} for job in snapshot.jobs]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--ticket", required=True)
    submit.add_argument("--config")
    submit.add_argument("--kb")
    submit.add_argument("--deployment", default="local-ollama")
    submit.add_argument("--model", default="local-llama3.2")
    submit.add_argument("--summary-delay-seconds", type=float, default=0)
    submit.add_argument("--response-seconds", type=int, default=300)
    submit.add_argument("--completion-seconds", type=int, default=600)
    submit.add_argument("--timeout-seconds", type=float, default=120)
    status = commands.add_parser("status")
    status.add_argument("workflow_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("workflow_id")
    retry = commands.add_parser("retry", help="Explicit retry within the saved retry limit")
    retry.add_argument("workflow_id")
    retry.add_argument("step_id")
    retry.add_argument("--acknowledge-possible-duplicate", action="store_true",
                       help="Required when the old request may already have reached the provider")
    run = commands.add_parser("run")
    run.add_argument("--worker-id", default="worker-" + uuid4().hex)
    run.add_argument("--config")
    run.add_argument("--kb")
    run.add_argument("--once", action="store_true")
    run.add_argument("--max-jobs", type=int, default=0, help="0 means no count limit")
    run.add_argument("--lease-seconds", type=float, default=60)
    run.add_argument("--poll-seconds", type=float, default=0.5)
    run.add_argument("--idle-exit-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    store = None
    try:
        if args.command == "run":
            for value, name in ((args.lease_seconds, "lease"), (args.poll_seconds, "poll")):
                _seconds(value, name, positive=True)
            _seconds(args.idle_exit_seconds, "idle exit")
            if args.max_jobs < 0:
                raise ValueError("max-jobs cannot be negative")
        store = QueueStore(args.db)
        if args.command == "submit":
            identity = submit_fixed(store, load_deployments(args.config), KnowledgeBase.load(args.kb), args.ticket,
                deployment_id=args.deployment, model_id=args.model,
                summary_delay_seconds=args.summary_delay_seconds, response_seconds=args.response_seconds,
                completion_seconds=args.completion_seconds, timeout_seconds=args.timeout_seconds)
            print(json.dumps(snapshot_summary(store.read(identity)), indent=2))
        elif args.command in {"status", "cancel", "retry"}:
            if args.command == "cancel":
                store.cancel(args.workflow_id, now=_now(SystemClock()))
            elif args.command == "retry":
                store.retry(args.workflow_id, args.step_id, now=_now(SystemClock()),
                            acknowledge_ambiguous=args.acknowledge_possible_duplicate)
            print(json.dumps(snapshot_summary(store.read(args.workflow_id)), indent=2))
        else:
            registry = load_deployments(args.config) if args.config else None
            kb = KnowledgeBase.load(args.kb) if args.kb else None
            count, last_work = 0, time.monotonic()
            while True:
                result = work_once(store, worker_id=args.worker_id, registry=registry, kb=kb,
                                   lease_seconds=args.lease_seconds)
                if result["status"] != "idle":
                    print(json.dumps(result), flush=True)
                    count += 1
                    last_work = time.monotonic()
                if args.once or (args.max_jobs and count >= args.max_jobs):
                    break
                if result["status"] == "idle":
                    if time.monotonic() - last_work >= args.idle_exit_seconds:
                        break
                    time.sleep(args.poll_seconds)
        return 0
    except (ValueError, OSError, KeyError):
        print(json.dumps({"error_code": "invalid-worker-configuration-or-state"}))
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
