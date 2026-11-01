"""Connected, resumable support-ticket workflow on one explicit deployment.

M2 executes a fixed model/deployment immediately. Joint optimization and delayed
multiworker scheduling are later milestones; neither is simulated by this CLI.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from shared.deployments import DeploymentRegistry, load_deployments
from shared.scheduling import Clock, ExecutionAttempt, MetricEstimate, PlannedCandidate, StepEstimates, SystemClock
from workflow.dispatch import dispatch_planned_candidate
from workflow.workload import KnowledgeBase, build_workflow_request, render_step, validate_step_output
from workflow.workflow_store import WorkflowLockedError, WorkflowStore


RUNNER_VERSION = "connected-fixed-v1"
TERMINAL = frozenset({"completed", "failed", "deadline_missed", "needs-reconciliation"})
ROOT = Path(__file__).resolve().parent.parent


def workload_fingerprint(request, kb: KnowledgeBase) -> str:
    """Bind actual input and prompt/KB versions, independently of run IDs."""
    payload = {"input_text": request.input_text, "workload_version": request.workload_version,
               "knowledge_base": kb.fingerprint()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _now(clock: Clock) -> datetime:
    instant = clock.now()
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Workflow clock must return timezone-aware timestamps")
    return instant.astimezone(timezone.utc)


def _summary(store: WorkflowStore) -> dict:
    state = store.state()
    outputs = store.completed_outputs()
    return {"workflow_id": store.request.workflow_id, "status": state["status"],
            "execution_mode": "fixed-immediate", "completed_steps": list(outputs),
            "response": outputs.get("draft"), "summary": outputs.get("summarize"),
            "error_code": state.get("error_code")}


def execute_workflow(
    store: WorkflowStore,
    registry: DeploymentRegistry,
    kb: KnowledgeBase,
    *,
    until_response: bool = False,
    clock: Clock | None = None,
    dispatcher=None,
) -> dict:
    """Run ready steps once, retaining dependency outputs and dispatch evidence.

    A run left dispatching needs reconciliation after restart. A completed step
    is never regenerated. The exclusive lock covers this single runner; it is
    not the durable leased queue planned for M5.
    """
    if type(until_response) is not bool:
        raise ValueError("until_response must be a boolean")
    active_clock = clock or SystemClock()
    dispatch = dispatcher or dispatch_planned_candidate
    with store.exclusive():
        state = store.state()
        request = store.request
        if request.policy != "fixed":
            raise ValueError("Connected runner currently requires the fixed policy")
        if state["registry_fingerprint"] != registry.config_fingerprint():
            raise ValueError("Deployment configuration differs from the saved run")
        if state["workload_fingerprint"] != workload_fingerprint(request, kb):
            raise ValueError("Workload or knowledge base differs from the saved run")
        config = state["execution_config"]
        if not isinstance(config, dict) or config.get("runner_version") != RUNNER_VERSION:
            raise ValueError("Saved run requires a different runner version")
        deployment = registry.require_model(config["deployment_id"], config["model_id"])
        timeout = config["timeout_seconds"]
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise ValueError("Saved timeout is outside the supported range")
        if state["status"] in TERMINAL:
            return _summary(store)
        store.mark_status("running")
        while True:
            outputs = store.completed_outputs()
            pending = [step for step in request.steps if step.step_id not in outputs]
            if not pending:
                store.mark_status("completed")
                return _summary(store)
            if until_response and all(step.step_id in outputs for step in request.steps
                                      if step.required_for_response):
                store.mark_status("response_ready")
                return _summary(store)
            ready = [step for step in pending if set(step.dependencies) <= outputs.keys()]
            if not ready:
                store.mark_status("failed", error_code="dependency-state-invalid")
                return _summary(store)
            step = ready[0]
            now = _now(active_clock)
            if now < step.earliest_start:
                # No hidden wall-clock wait in M2. The durable worker owns this.
                store.mark_status("failed", error_code="step-not-due")
                return _summary(store)
            if now >= step.latest_finish:
                store.mark_status("deadline_missed", error_code="step-deadline-exceeded")
                return _summary(store)
            try:
                rendered = render_step(request, step.step_id, outputs, kb=kb)
            except ValueError:
                store.mark_status("failed", error_code="prompt-materialization-failed")
                return _summary(store)
            # Lookup/prompt construction consumes the same deadline budget.
            start = _now(active_clock)
            remaining = (rendered.step.latest_finish - start).total_seconds()
            if remaining <= 0:
                store.mark_status("deadline_missed", error_code="step-deadline-exceeded")
                return _summary(store)
            unknown = lambda unit: MetricEstimate(None, unit, "unknown")
            candidate = PlannedCandidate(
                candidate_id=uuid4().hex, step_id=step.step_id, model_id=config["model_id"],
                deployment_id=deployment.deployment_id, planned_start=start, region=deployment.region,
                estimates=StepEstimates(unknown("s"), unknown("ratio"), unknown("USD"),
                                        unknown("kWh"), unknown("gCO2e")),
            )
            store.begin_step(rendered.step, source_ids=rendered.source_ids,
                             lookup_seconds=rendered.lookup_seconds,
                             input_fingerprint=rendered.input_fingerprint)
            # The journal write itself consumes deadline budget. Retain a
            # terminal preflight attempt if it leaves no time for inference.
            remaining = (rendered.step.latest_finish - _now(active_clock)).total_seconds()
            if remaining <= 0:
                expired = ExecutionAttempt(
                    uuid4().hex, request.workflow_id, candidate, "failed",
                    error="step-deadline-exceeded",
                    output_limit_tokens=rendered.step.output_limit_tokens,
                    enable_thinking=rendered.step.enable_thinking,
                )
                store.finish_step(expired, output_valid=False)
                store.mark_status("deadline_missed", error_code="step-deadline-exceeded")
                return _summary(store)
            # Persisted 'running' survives any exception/crash during dispatch.
            # No automatic retry can duplicate an ambiguous external request.
            attempt = dispatch(
                candidate, registry, rendered.step.prompt, workflow_id=request.workflow_id,
                clock=active_clock, timeout_seconds=min(timeout, remaining),
                output_limit_tokens=rendered.step.output_limit_tokens,
                enable_thinking=rendered.step.enable_thinking,
                latest_finish=rendered.step.latest_finish,
            )
            valid, validation_error = True, None
            if attempt.status == "completed":
                if attempt.completion_reason == "length":
                    valid, validation_error = False, "step-output-truncated"
                elif attempt.completion_reason not in {None, "stop"}:
                    valid, validation_error = False, "step-generation-incomplete"
                else:
                    try:
                        validate_step_output(step.step_id, attempt.output, source_ids=rendered.source_ids)
                    except ValueError:
                        valid, validation_error = False, "step-output-invalid"
            validated_late = _now(active_clock) > rendered.step.latest_finish
            store.finish_step(attempt, output_valid=valid, validation_error=validation_error,
                              deadline_exceeded=validated_late)
            finished = max(attempt.actual_finish or _now(active_clock), _now(active_clock))
            if (store.state()["status"] == "deadline_missed" or finished > rendered.step.latest_finish
                    or attempt.error == "step-deadline-exceeded"):
                store.mark_status("deadline_missed", error_code="step-deadline-exceeded")
                return _summary(store)
            if attempt.status != "completed" or not valid:
                store.mark_status("failed", error_code=attempt.error or validation_error or "step-failed")
                return _summary(store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--ticket", help="Support-ticket text")
    inputs.add_argument("--ticket-file", type=Path, help="UTF-8 text file containing the ticket")
    inputs.add_argument("--resume", type=Path, help="Existing connected run directory")
    parser.add_argument("--run-dir", type=Path, help="New output directory")
    parser.add_argument("--config", help="Explicit deployment registry JSON")
    parser.add_argument("--kb", help="Versioned knowledge base JSON")
    parser.add_argument("--deployment", help="Deployment ID (new runs default: local-ollama)")
    parser.add_argument("--model", help="Canonical model ID (new runs default: local-llama3.2)")
    parser.add_argument("--timeout-seconds", type=float, help="Per-request timeout, at most 300 (default: 120)")
    parser.add_argument("--thinking", choices=("provider-default", "off", "on"))
    parser.add_argument("--response-seconds", type=int, help="Response deadline after arrival (default: 300)")
    parser.add_argument("--completion-seconds", type=int, help="Final deadline after arrival (default: 600)")
    parser.add_argument("--all-urgent", action="store_true", help="Require CRM summary for the urgent response")
    parser.add_argument("--response-only", action="store_true", help="Stop after urgent work; resume background work later")
    args = parser.parse_args(argv)
    try:
        registry = load_deployments(args.config)
        kb = KnowledgeBase.load(args.kb)
        if args.resume is not None:
            if any(value is not None for value in (args.run_dir, args.deployment, args.model,
                   args.timeout_seconds, args.thinking, args.response_seconds, args.completion_seconds)) or args.all_urgent:
                raise ValueError("Resume uses saved settings; omit new-run configuration arguments")
            directory = args.resume
            store = WorkflowStore.load(directory)
        else:
            deployment_id, model_id = args.deployment or "local-ollama", args.model or "local-llama3.2"
            deployment = registry.require_model(deployment_id, model_id)
            timeout = 120 if args.timeout_seconds is None else args.timeout_seconds
            if not math.isfinite(timeout) or not 0 < timeout <= 300:
                raise ValueError("Timeout must be finite and between 0 and 300 seconds")
            thinking_mode = args.thinking or ("off" if deployment.provider == "ollama" else "provider-default")
            thinking = {"provider-default": None, "off": False, "on": True}[thinking_mode]
            if deployment.provider != "ollama" and thinking is not None:
                raise ValueError("Explicit thinking controls currently require Ollama")
            ticket = args.ticket if args.ticket is not None else args.ticket_file.read_text(encoding="utf-8-sig")
            request = build_workflow_request(
                ticket, workflow_id=uuid4().hex, arrived_at=SystemClock().now(), kb=kb,
                response_budget_seconds=300 if args.response_seconds is None else args.response_seconds,
                completion_budget_seconds=600 if args.completion_seconds is None else args.completion_seconds,
                all_urgent=args.all_urgent, enable_thinking=thinking,
            )
            directory = args.run_dir or ROOT / "results" / "runs" / ("connected_" + request.workflow_id)
            store = WorkflowStore.create(
                directory, request, registry_fingerprint=registry.config_fingerprint(),
                workload_fingerprint=workload_fingerprint(request, kb),
                execution_config={"deployment_id": deployment_id, "model_id": model_id,
                                  "timeout_seconds": timeout, "runner_version": RUNNER_VERSION},
            )
        result = execute_workflow(store, registry, kb, until_response=args.response_only)
        print(json.dumps({**result, "run_directory": str(directory)}, indent=2, ensure_ascii=False))
        return 0 if result["status"] in {"completed", "response_ready"} else 1
    except WorkflowLockedError:
        print(json.dumps({"error_code": "workflow-locked-reconciliation-required"}))
        return 2
    except (ValueError, OSError):
        # Input/configuration errors never echo arbitrary provider messages.
        print(json.dumps({"error_code": "invalid-workflow-configuration-or-state"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
