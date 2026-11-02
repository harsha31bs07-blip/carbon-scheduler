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
import sqlite3
from pathlib import Path
import threading
import time
from uuid import uuid4

from shared.deployments import DeploymentRegistry, load_deployments
from shared.energy import EnergySession
from shared.scheduling import MetricEstimate, PlannedCandidate, StepEstimates, SystemClock
from workflow.connected import _now, workload_fingerprint
from workflow.dispatch import dispatch_planned_candidate
from workflow.queue_store import LeaseError, QueueStore
from workflow.workload import KnowledgeBase, build_workflow_request, render_step, validate_step_output


WORKER_VERSION = "sqlite-worker-v1"
_AUTOMATIC_THINKING = object()


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
                 timeout_seconds=120, tariff_book=None, carbon_provider=None,
                 energy_mode="none", energy_country=None, assignments=None,
                 allow_summary_deferral=True, enable_thinking=_AUTOMATIC_THINKING,
                 experiment_mode="live", submission=None):
    """Persist a real input and explicit assignment without dispatching anything."""
    _seconds(summary_delay_seconds, "summary_delay_seconds")
    _seconds(timeout_seconds, "timeout_seconds", positive=True)
    if timeout_seconds > 300:
        raise ValueError("Timeout must be at most 300 seconds")
    if type(allow_summary_deferral) is not bool or experiment_mode not in {"live", "simulation"}:
        raise ValueError("Explicit deferral and experiment settings are required")
    if not allow_summary_deferral and summary_delay_seconds:
        raise ValueError("A nondeferrable summary cannot request a deliberate delay")
    if assignments is None:
        pairs = {step: {"deployment_id": deployment_id, "model_id": model_id}
                 for step in ("classify", "retrieve", "draft", "summarize")}
    else:
        from workflow.workload import STEP_ORDER
        if not isinstance(assignments, dict) or set(assignments) != set(STEP_ORDER):
            raise ValueError("Fixed assignments must include every workflow step")
        pairs = assignments
    for pair in pairs.values():
        if not isinstance(pair, dict) or set(pair) != {"deployment_id", "model_id"}:
            raise ValueError("Fixed assignments require exact model/deployment pairs")
        registry.require_model(pair["deployment_id"], pair["model_id"])
    if enable_thinking is _AUTOMATIC_THINKING:
        enable_thinking = False if all(registry.get(pair["deployment_id"]).provider == "ollama"
                                      for pair in pairs.values()) else None
    request = build_workflow_request(
        ticket, workflow_id=workflow_id or uuid4().hex, arrived_at=_now(clock or SystemClock()), kb=kb,
        response_budget_seconds=response_seconds, completion_budget_seconds=completion_seconds,
        enable_thinking=enable_thinking,
    )
    if not allow_summary_deferral:
        request = replace(request, steps=tuple(replace(step, allow_deferral=False)
            if step.step_id == "summarize" else step for step in request.steps))
    if summary_delay_seconds >= (request.completion_deadline - request.arrived_at).total_seconds():
        raise ValueError("Summary delay must precede the completion deadline")
    unknown = lambda unit: MetricEstimate(None, unit, "unknown")
    estimates = StepEstimates(unknown("s"), unknown("ratio"), unknown("USD"), unknown("kWh"), unknown("gCO2e"))
    candidates = tuple(PlannedCandidate(
        uuid4().hex, step.step_id, pairs[step.step_id]["model_id"], pairs[step.step_id]["deployment_id"],
        step.earliest_start + timedelta(seconds=summary_delay_seconds if step.step_id == "summarize" else 0),
        estimates, region=registry.get(pairs[step.step_id]["deployment_id"]).region,
    ) for step in request.steps)
    from workflow.execution_accounting import make_accounting_context
    accounting_context = make_accounting_context(registry, request, tariff_book=tariff_book,
        carbon_provider=carbon_provider, energy_mode=energy_mode, energy_country=energy_country)
    return store.enqueue(request, candidates, accounting_context=accounting_context, submission=submission, configuration={
        "registry_snapshot": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint(),
        "kb_fingerprint": kb.fingerprint(), "workload_fingerprint": workload_fingerprint(request, kb),
        "worker_version": WORKER_VERSION, "policy_version": "fixed-explicit-v1",
        "accounting_version": "execution-accounting.v1",
        "experiment_mode": experiment_mode, "timeout_seconds": timeout_seconds,
        "max_retries": 3,
    })


def _note_presence(store, worker_id, state, clock=None, *, workflow_id=None):
    try:
        store.worker_seen(worker_id, state=state, now=_now(clock or SystemClock()), workflow_id=workflow_id)
    except (ValueError, OSError, sqlite3.Error):
        # Optional telemetry cannot change provider execution or lease semantics.
        pass


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
                _note_presence(store, self.claim.worker_id, "running", self.clock,
                               workflow_id=self.claim.workflow_id)
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
              lease_seconds=60, heartbeat=True, health_probe=None, tracker_factory=None):
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
        from workflow.planned_worker import work_planned_once
        return work_planned_once(store, worker_id=worker_id, registry=registry, kb=kb, clock=active_clock,
            dispatcher=dispatcher, lease_seconds=lease_seconds, heartbeat=heartbeat, health_probe=health_probe,
            tracker_factory=tracker_factory)
    return _execute_claim(store, claim, registry=registry, kb=kb, clock=active_clock,
                          dispatcher=dispatcher, lease_seconds=lease_seconds, heartbeat=heartbeat,
                          tracker_factory=tracker_factory)


def _execute_claim(store, claim, *, registry=None, kb=None, clock=None, dispatcher=None,
                   lease_seconds=60, heartbeat=True, worker_version=WORKER_VERSION, pre_dispatch=None,
                   tracker_factory=None):
    """Use the same durable provider boundary for fixed and planned claims."""
    active_clock = clock or SystemClock()
    try:
        config = claim.configuration
        if (config.get("worker_version") != worker_version
                or config.get("experiment_mode") not in {"live", "simulation"}
                or config.get("experiment_mode") == "simulation" and dispatcher is None):
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
    if pre_dispatch is not None:
        reason = pre_dispatch(claim, rendered, _now(active_clock))
        if reason is not None:
            try:
                store.release_undispatched(claim, now=_now(active_clock))
            except LeaseError:
                store.recover(now=_now(active_clock))
                return {"status": "claim-lost", "workflow_id": claim.workflow_id,
                        "step_id": claim.candidate.step_id, "job_processed": False}
            return {"status": "replan-required", "workflow_id": claim.workflow_id,
                    "step_id": claim.candidate.step_id, "reason": reason, "job_processed": False}
    context = state.accounting_context
    deployment = active_registry.get(claim.candidate.deployment_id)
    factory = _energy_factory(context, tracker_factory)
    if config["experiment_mode"] == "simulation" and tracker_factory is None:
        factory = None
    session = EnergySession(deployment.endpoint, tracker_factory=factory)
    measurement_start = _now(active_clock)
    result = None
    raw_persisted = False
    with _Heartbeat(store.path, claim, active_clock, lease_seconds, heartbeat), session:
        # Starting host telemetry can take time. Check eligibility again after
        # startup and before marking a provider call as possibly dispatched.
        if pre_dispatch is not None:
            reason = pre_dispatch(claim, rendered, _now(active_clock))
            if reason is not None:
                try:
                    store.release_undispatched(claim, now=_now(active_clock))
                except LeaseError:
                    store.recover(now=_now(active_clock))
                    return {"status": "claim-lost", "workflow_id": claim.workflow_id,
                            "step_id": claim.candidate.step_id, "job_processed": False}
                return {"status": "replan-required", "workflow_id": claim.workflow_id,
                        "step_id": claim.candidate.step_id, "reason": reason, "job_processed": False}
        try:
            claim = store.mark_running(claim, now=_now(active_clock), rendered_step=rendered.step,
                source_ids=rendered.source_ids, input_fingerprint=rendered.input_fingerprint,
                lookup_seconds=rendered.lookup_seconds)
        except LeaseError:
            store.recover(now=_now(active_clock))
            return {"status": "claim-lost", "workflow_id": claim.workflow_id, "step_id": claim.candidate.step_id}
        _note_presence(store, claim.worker_id, "running", active_clock, workflow_id=claim.workflow_id)
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
                    raw_persisted = True
                    result = {"status": "late-attempt-archived", "workflow_id": claim.workflow_id,
                              "step_id": claim.candidate.step_id}
                except Exception:
                    pass
            if result is None:
                orphan = _retain_orphan(store, claim, attempt)
                result = {"status": "needs-reconciliation", "workflow_id": claim.workflow_id,
                          "step_id": claim.candidate.step_id, "orphan_attempt": orphan}
        else:
            raw_persisted = True
        # This timestamp follows the actual journal commit (or orphan write).
        # Telemetry shutdown and all optional enrichment happen afterwards.
        journal_recorded_at = _now(active_clock)
    measurement_finish = _now(active_clock)
    if result is None:
        result = {"status": store.read(claim.workflow_id).status, "workflow_id": claim.workflow_id,
                  "step_id": claim.candidate.step_id, "attempt_id": attempt.attempt_id}
    if context is not None:
        _account_returned_attempt(store, attempt, active_registry, context, session,
            measurement_start, measurement_finish, journal_recorded_at, active_clock, raw_persisted, result)
    return result


def _energy_factory(context, injected):
    """Enable only an explicitly requested offline host tracker."""
    if context is None or context["energy_mode"] != "codecarbon":
        return None
    if injected is not None:
        return injected
    def factory(**settings):
        from codecarbon import OfflineEmissionsTracker
        return OfflineEmissionsTracker(country_iso_code=context["energy_country"], **settings)
    return factory


def _retain_accounting(store, attempt, record):
    directory = store.path.parent / (store.path.name + ".orphan-accounting")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (uuid4().hex + ".json")
    payload = {"schema_version": 1, "record_type": "UnreconciledAttemptAccounting",
               "workflow_id": attempt.workflow_id, "accounting": record}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    return str(path)


def _account_returned_attempt(store, attempt, registry, context, session, measurement_start,
                              measurement_finish, journal_recorded_at, clock, raw_persisted, result):
    """Optional accounting can neither invalidate a result nor repeat a call."""
    from workflow.execution_accounting import enrich_attempt, unknown_energy_report
    try:
        report = session.report(max(0., (measurement_finish - measurement_start).total_seconds()) * 1000)
    except Exception:
        report = unknown_energy_report("tracker-report-failed")
    try:
        record = enrich_attempt(attempt, registry, context, energy_report=report,
            measurement_start=measurement_start, measurement_finish=measurement_finish,
            journal_recorded_at=journal_recorded_at, accounted_at=_now(clock))
    except Exception:
        result["accounting_status"] = "enrichment-failed"
        return
    if raw_persisted:
        try:
            store.record_accounting(attempt.workflow_id, record, now=_now(clock))
            result["accounting_status"] = "recorded"
            return
        except Exception:
            pass
    result["accounting_status"] = "needs-reconciliation"
    try:
        result["orphan_accounting"] = _retain_accounting(store, attempt, record)
    except Exception:
        result["accounting_status"] = "persistence-failed"


def snapshot_summary(snapshot):
    outputs = snapshot.completed_outputs()
    summary = {"workflow_id": snapshot.request.workflow_id, "status": snapshot.status,
            "execution_mode": "fixed-durable" if snapshot.request.policy == "fixed" else "planned-durable",
            "attempt_count": len(snapshot.attempts),
            "response": outputs.get("draft"), "summary": outputs.get("summarize"),
            "jobs": [{"step_id": job.step_request.step_id, "status": job.status,
                      "planned_start": job.candidate.planned_start.isoformat(),
                      "dispatched_at": job.dispatched_at.isoformat() if job.dispatched_at else None,
                      "error_code": job.error_code} for job in snapshot.jobs]}
    if snapshot.planning_context is not None:
        latest = snapshot.decisions[-1]
        report = latest["report"]
        summary.update(policy=snapshot.request.policy, planning_revision=latest["revision"],
            planning_error=report.get("error_code"),
            planning_rejections=report.get("result", {}).get("rejection_counts", {}),
            unavailable_contexts=[item["step_id"] for item in report.get("contexts", ())
                                  if item["input_mode"] == "unavailable"])
    from workflow.accounting_report import execution_report
    accounting = execution_report(snapshot)
    summary["accounting"] = {key: accounting[key] for key in ("coverage", "resources", "quality", "timing")}
    return summary


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
    submit.add_argument("--carbon", type=Path, help="Versioned attribution snapshot; no inferred grid")
    planned = commands.add_parser("submit-planned", help="Retain a calibrated joint plan or explicit infeasibility")
    planned.add_argument("--ticket", required=True)
    planned.add_argument("--config")
    planned.add_argument("--kb")
    planned.add_argument("--profiles", type=Path, required=True)
    planned.add_argument("--forecasts", type=Path, help="JSON list of workload-bound schema-3 size forecasts")
    planned.add_argument("--carbon", type=Path, help="Versioned static estimate or timestamped trace JSON")
    planned.add_argument("--policy", choices=("speed", "balanced", "eco", "custom"), default="balanced")
    planned.add_argument("--weights", help="Custom JSON object with all five unprefixed metric names")
    planned.add_argument("--quality-floor", type=float, default=.8)
    planned.add_argument("--start-offset-seconds", default="0", help="Comma-separated finite start offsets")
    planned.add_argument("--summary-delay-seconds", type=float, default=0)
    planned.add_argument("--response-seconds", type=int, default=300)
    planned.add_argument("--completion-seconds", type=int, default=600)
    planned.add_argument("--timeout-seconds", type=float, default=120)
    planned.add_argument("--thinking", choices=("disabled", "provider-default", "enabled"), default="disabled")
    planned.add_argument("--replan-seconds", type=float, default=10.)
    planned.add_argument("--probe-timeout-seconds", type=float, default=3.)
    planned.add_argument("--health-max-age-seconds", type=float, default=300.)
    planned.add_argument("--max-decision-age-seconds", type=float, default=5.)
    for command in (submit, planned):
        command.add_argument("--tariffs", type=Path, help="Versioned API-only tariff book JSON")
        command.add_argument("--energy", choices=("none", "codecarbon"), default="none")
        command.add_argument("--energy-country", help="Explicit ISO3 country for offline host telemetry")
    status = commands.add_parser("status")
    status.add_argument("workflow_id")
    report = commands.add_parser("report", help="Offline coverage, resources, clocks, and per-attempt evidence")
    report.add_argument("workflow_id")
    reconcile = commands.add_parser("reconcile-accounting", help="Retry storing an orphan accounting record without inference")
    reconcile.add_argument("record", type=Path)
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
        if args.command in {"submit", "submit-planned"}:
            from data.cost_accounting import TariffBook
            from data.quality_reviews import load_json
            from workflow.planning_records import decode_carbon
            accounting_args = {"tariff_book": TariffBook.load(args.tariffs) if args.tariffs else None,
                "energy_mode": args.energy, "energy_country": args.energy_country,
                "carbon_provider": decode_carbon(load_json(args.carbon)) if args.carbon else None}
        if args.command == "submit-planned":
            from data.quality_reviews import load_json
            from shared.scheduling import WorkloadSizeForecast
            from workflow.planned_worker import submit_planned
            from workflow.planning_records import decode_carbon
            from workflow.queue_store import _load

            forecasts = load_json(args.forecasts) if args.forecasts else []
            if not isinstance(forecasts, list):
                raise ValueError("Forecast file must contain an array")
            identity = submit_planned(store, load_deployments(args.config), KnowledgeBase.load(args.kb),
                args.ticket, load_json(args.profiles), forecasts=[WorkloadSizeForecast.from_dict(item) for item in forecasts],
                **accounting_args,
                policy=args.policy, custom_weights=_load(args.weights) if args.weights else None,
                quality_floor=args.quality_floor, start_offsets_seconds=tuple(float(value) for value in args.start_offset_seconds.split(",")),
                summary_delay_seconds=args.summary_delay_seconds, response_seconds=args.response_seconds,
                completion_seconds=args.completion_seconds, timeout_seconds=args.timeout_seconds,
                enable_thinking={"disabled": False, "provider-default": None, "enabled": True}[args.thinking],
                replan_interval_seconds=args.replan_seconds, probe_timeout_seconds=args.probe_timeout_seconds,
                health_max_age_seconds=args.health_max_age_seconds, max_decision_age_seconds=args.max_decision_age_seconds)
            print(json.dumps(snapshot_summary(store.read(identity)), indent=2))
        elif args.command == "submit":
            identity = submit_fixed(store, load_deployments(args.config), KnowledgeBase.load(args.kb), args.ticket,
                deployment_id=args.deployment, model_id=args.model,
                summary_delay_seconds=args.summary_delay_seconds, response_seconds=args.response_seconds,
                completion_seconds=args.completion_seconds, timeout_seconds=args.timeout_seconds, **accounting_args)
            print(json.dumps(snapshot_summary(store.read(identity)), indent=2))
        elif args.command == "report":
            from workflow.accounting_report import execution_report
            print(json.dumps(execution_report(store.read(args.workflow_id)), indent=2))
        elif args.command == "reconcile-accounting":
            from data.quality_reviews import load_json
            payload = load_json(args.record)
            if (not isinstance(payload, dict) or set(payload) != {"schema_version", "record_type", "workflow_id", "accounting"}
                    or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                    or payload["record_type"] != "UnreconciledAttemptAccounting"
                    or not isinstance(payload["accounting"], dict)
                    or payload["accounting"].get("workflow_id") != payload["workflow_id"]):
                raise ValueError("Unsupported orphan accounting envelope")
            store.record_accounting(payload["workflow_id"], payload["accounting"], now=_now(SystemClock()))
            print(json.dumps({"status": "accounting-reconciled", "workflow_id": payload["workflow_id"],
                              "attempt_id": payload["accounting"]["attempt_id"]}))
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
                _note_presence(store, args.worker_id, "polling")
                result = work_once(store, worker_id=args.worker_id, registry=registry, kb=kb,
                                   lease_seconds=args.lease_seconds)
                _note_presence(store, args.worker_id, "idle")
                if result["status"] != "idle":
                    print(json.dumps(result), flush=True)
                    if result.get("job_processed", True):
                        count += 1
                        last_work = time.monotonic()
                if args.once or (args.max_jobs and count >= args.max_jobs):
                    break
                if result["status"] == "idle" or not result.get("job_processed", True):
                    if time.monotonic() - last_work >= args.idle_exit_seconds:
                        break
                    time.sleep(args.poll_seconds)
        return 0
    except (ValueError, OSError, KeyError):
        print(json.dumps({"error_code": "invalid-worker-configuration-or-state"}))
        return 2
    finally:
        if store is not None:
            if args.command == "run":
                _note_presence(store, args.worker_id, "stopped")
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
