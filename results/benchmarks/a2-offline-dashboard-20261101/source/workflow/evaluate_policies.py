"""Freeze, execute, and inspect paired durable-workflow evaluations.

Execution is serial in seeded, randomized case/repeat blocks. Provider failures
and infeasibility are outcomes; interrupted calls never trigger automatic retry.
Report and export commands import no provider transports and never wait.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from data.quality_reviews import fingerprint, load_json
from shared.scheduling import SystemClock, WorkloadSizeForecast
from workflow.evaluation_protocol import seal_protocol, validate_protocol, schedule_trials, validate_trial_snapshot


ROOT = Path(__file__).resolve().parents[1]
_TERMINAL = {"completed", "failed", "cancelled", "deadline_missed", "needs-reconciliation",
             "infeasible", "planning-failed"}
DEFAULT_CONTROLS = {"search_limit": 100000, "health_max_age_seconds": 300., "minimum_improvement": .01,
    "replan_interval_seconds": 10., "probe_timeout_seconds": 3., "max_decision_age_seconds": 5.}


def _now(clock):
    instant = clock.now()
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Evaluation clock must be timezone-aware")
    return instant.astimezone(timezone.utc)


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _seal_journal(journal):
    return {**journal, "fingerprint": fingerprint({key: value for key, value in journal.items() if key != "fingerprint"})}


def source_manifest(root=ROOT):
    """Capture runtime source and frozen workload definitions, excluding secrets."""
    root = Path(root).resolve()
    paths = set()
    for directory in ("shared", "data", "scheduler", "workflow"):
        paths.update(path.relative_to(root).as_posix() for path in (root / directory).rglob("*.py"))
    paths.update(path.relative_to(root).as_posix() for path in (root / "data/workloads").glob("*.json"))
    paths.update(name for name in ("requirements.txt", "requirements-dev.txt", "constraints/windows-py313.txt",
        "config/quality/pilot-v1.json") if (root / name).is_file())
    return {name: hashlib.sha256(_source_bytes(root, name)).hexdigest() for name in sorted(paths)}


def _source_bytes(root, name):
    root = Path(root).resolve()
    path = root / name
    if root not in path.resolve().parents or any(part.is_symlink() for part in (path, *path.parents) if part != root):
        raise ValueError("Source files must be regular files inside their source root")
    if not path.is_file():
        raise ValueError("Frozen source file is unavailable")
    return path.read_bytes()


def _check_sources(protocol, root):
    if protocol["experiment_mode"] == "live":
        if Path(root).resolve() != ROOT.resolve() or not set(source_manifest()) <= set(protocol["source_manifest"]):
            raise ValueError("Live execution requires the complete current runtime source snapshot")
    for name, checksum in protocol["source_manifest"].items():
        if hashlib.sha256(_source_bytes(root, name)).hexdigest() != checksum:
            raise ValueError("Current execution source differs from the frozen protocol")


def prepare_evaluation(protocol, output, *, source_root=ROOT, clock=None):
    """Freeze the protocol, exact source copies, and pending randomized schedule."""
    protocol = validate_protocol(protocol)
    _check_sources(protocol, source_root)
    instant = _now(clock or SystemClock())
    if instant < datetime.fromisoformat(protocol["created_at"].replace("Z", "+00:00")):
        raise ValueError("Evaluation cannot precede protocol creation")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # Exclusive reservation; never overwrite earlier evidence.
    _write(output / "protocol.json", protocol)
    for name in protocol["source_manifest"]:
        content = _source_bytes(source_root, name)
        if hashlib.sha256(content).hexdigest() != protocol["source_manifest"][name]:
            raise ValueError("Source changed while freezing the evaluation")
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
    stamp = instant.isoformat()
    journal = {"schema_version": 1, "record_type": "MatchedPolicyEvaluation",
        "protocol_fingerprint": protocol["fingerprint"], "status": "prepared",
        "created_at": stamp, "updated_at": stamp,
        "trials": [{**trial, "state": "pending", "bundle": None, "started_at": None,
                    "finished_at": None, "stop_reason": None} for trial in schedule_trials(protocol)]}
    _write(output / "evaluation.json", _seal_journal(journal))
    return output


@contextmanager
def _runner_lock(directory):
    """An OS lock releases on process death; no stale PID unlock is needed."""
    with (directory / ".runner.lock").open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another evaluation runner owns this directory") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _submit_trial(store, protocol, entry, clock, health_probe):
    from data.cost_accounting import TariffBook
    from shared.deployments import DeploymentRegistry
    from workflow.planning_records import decode_carbon, decode_knowledge_base
    from workflow.worker import submit_fixed

    registry = DeploymentRegistry.from_dict(protocol["registry"])
    kb = decode_knowledge_base(protocol["knowledge_base"])
    case = next(case for case in protocol["dataset"]["cases"] if case["case_id"] == entry["case_id"])
    arm = next(arm for arm in protocol["arms"] if arm["arm_id"] == entry["arm_id"])
    common = {"clock": clock, "workflow_id": entry["workflow_id"], "response_seconds": protocol["response_seconds"],
        "completion_seconds": protocol["completion_seconds"], "timeout_seconds": protocol["timeout_seconds"],
        "tariff_book": TariffBook.from_dict(protocol["tariffs"]) if protocol["tariffs"] is not None else None,
        "carbon_provider": decode_carbon(protocol["carbon"]), "energy_mode": protocol["energy_mode"],
        "energy_country": protocol["energy_country"], "enable_thinking": False,
        "allow_summary_deferral": arm["allow_summary_deferral"], "experiment_mode": protocol["experiment_mode"]}
    if arm["kind"] == "fixed":
        return submit_fixed(store, registry, kb, case["ticket"],
            assignments={step: pairs[0] for step, pairs in arm["allowed_pairs"].items()}, **common)
    from workflow.planned_worker import submit_planned
    return submit_planned(store, registry, kb, case["ticket"], protocol["profiles"],
        forecasts=[WorkloadSizeForecast.from_dict(item) for item in protocol["forecasts"][entry["case_id"]]],
        policy=arm["policy"], custom_weights=arm["custom_weights"], quality_floor=protocol["quality_floor"],
        start_offsets_seconds=arm["start_offsets_seconds"], allowed_pairs=arm["allowed_pairs"],
        health_probe=health_probe, **protocol["planning_controls"], **common)


def _serial_hazard(snapshot):
    from workflow.accounting_report import execution_report
    coverage = execution_report(snapshot)["coverage"]
    return bool(coverage["uncertain_provider_attempt_ids"] or coverage["unrepresented_retry_indices"])


def run_evaluation(directory, *, clock=None, sleeper=None, dispatcher=None, health_probe=None,
                   tracker_factory=None, heartbeat=True, max_trials=None, progress=True, source_root=ROOT):
    """Execute/resume frozen trials without retrying an ambiguous provider call."""
    from workflow.policy_comparison import load_evaluation

    directory = Path(directory)
    if (directory / "manifest.json").exists():
        raise ValueError("Portable comparisons are immutable; run the original evaluation directory")
    protocol = validate_protocol(load_json(directory / "protocol.json"))
    if protocol["experiment_mode"] == "simulation":
        if dispatcher is None or any(arm["kind"] == "planned" for arm in protocol["arms"]) and health_probe is None:
            raise ValueError("Simulation requires explicit provider and planned-health fixtures")
    elif any(item is not None for item in (clock, sleeper, dispatcher, health_probe, tracker_factory)):
        raise ValueError("Injected execution requires an explicitly simulated protocol")
    if (max_trials is not None and (type(max_trials) is not int or not 1 <= max_trials <= 10000)
            or type(progress) is not bool or type(heartbeat) is not bool):
        raise ValueError("Invalid evaluation execution controls")
    _check_sources(protocol, source_root)
    load_evaluation(directory)  # Validate frozen source copies and the entire journal before work.
    active_clock, sleep = clock or SystemClock(), sleeper or time.sleep
    if _now(active_clock) < datetime.fromisoformat(protocol["created_at"].replace("Z", "+00:00")):
        raise ValueError("Evaluation cannot precede its protocol")
    from workflow.queue_store import QueueStore
    from workflow.queue_replay import export_workflow, load_bundle
    from workflow.worker import work_once
    from workflow.planning_records import decode_knowledge_base
    execution_kb = decode_knowledge_base(protocol["knowledge_base"])

    with _runner_lock(directory):
        load_evaluation(directory)
        journal = load_json(directory / "evaluation.json")
        if journal["status"] == "completed":
            return load_evaluation(directory)
        if _now(active_clock) < datetime.fromisoformat(journal["updated_at"].replace("Z", "+00:00")):
            raise ValueError("Resume clock precedes retained evaluation evidence")
        for entry in journal["trials"]:
            if entry["state"] == "finished" and _serial_hazard(load_bundle(directory / entry["bundle"])):
                raise ValueError("Unresolved provider completion prevents another serial trial")
        store = None
        finished = 0
        halted = False
        try:
            journal.update(status="running", updated_at=_now(active_clock).isoformat())
            _write(directory / "evaluation.json", _seal_journal(journal))
            for entry in journal["trials"]:
                if entry["state"] == "finished":
                    continue
                # Each trial owns a queue. A later global worker/recovery pass
                # cannot replan or expire an earlier exported trial's jobs.
                db = directory / "queues" / (entry["trial_id"] + ".sqlite3")
                if entry["state"] == "submitted" and not db.is_file():
                    raise ValueError("Resuming submitted work requires the original durable queue")
                if store is not None:
                    store.close()
                store = QueueStore(db)
                existing = {row[0] for row in store.connection.execute("SELECT id FROM workflows")}
                if existing - {entry["workflow_id"]}:
                    raise ValueError("Trial queue contains unrelated workflows")
                if entry["workflow_id"] not in existing:
                    if entry["state"] != "pending":
                        raise ValueError("Submitted trial disappeared from durable storage")
                    trial_started = _now(active_clock).isoformat()
                    _submit_trial(store, protocol, entry, active_clock, health_probe)
                    entry["started_at"] = trial_started
                    existing.add(entry["workflow_id"])
                state = store.read(entry["workflow_id"])
                validate_trial_snapshot(protocol, entry, state)
                # Also adopt a committed submission after a crash before the
                # corresponding journal update; deterministic IDs prevent a rerun.
                entry.update(state="submitted", started_at=entry["started_at"] or state.request.arrived_at.isoformat())
                journal["updated_at"] = _now(active_clock).isoformat()
                _write(directory / "evaluation.json", _seal_journal(journal))
                trial_stop_reason = None
                while True:
                    store.recover(now=_now(active_clock))
                    state = store.read(entry["workflow_id"])
                    if state.status in _TERMINAL:
                        break
                    result = work_once(store, worker_id="evaluation-" + uuid4().hex, clock=active_clock,
                        kb=execution_kb,
                        dispatcher=dispatcher, health_probe=health_probe, tracker_factory=tracker_factory,
                        heartbeat=heartbeat)
                    if progress:
                        print(json.dumps({"trial_id": entry["trial_id"], **result}), flush=True)
                    if result["status"] == "needs-reconciliation" or result.get("accounting_status") in {
                            "needs-reconciliation", "persistence-failed", "enrichment-failed"}:
                        trial_stop_reason = result.get("accounting_status") or "needs-reconciliation"
                        break
                    if result["status"] == "idle" or not result.get("job_processed", True):
                        remaining = (state.request.completion_deadline - _now(active_clock)).total_seconds()
                        if remaining > 0:
                            before = _now(active_clock)
                            sleep(min(protocol["poll_seconds"], remaining))
                            if _now(active_clock) <= before:
                                raise ValueError("Evaluation wait did not advance its clock")
                state = store.read(entry["workflow_id"])
                if trial_stop_reason is not None and state.status not in _TERMINAL and not _serial_hazard(state):
                    # Completed inference with failed optional accounting does
                    # not justify further trial work or another provider call.
                    store.cancel(entry["workflow_id"], now=_now(active_clock))
                    state = store.read(entry["workflow_id"])
                validate_trial_snapshot(protocol, entry, state)
                relative = "bundles/" + entry["trial_id"]
                target = directory / relative
                if target.exists():
                    if load_bundle(target) != state:
                        raise ValueError("Existing trial export differs from its durable queue")
                else:
                    export_workflow(db, entry["workflow_id"], target)
                entry.update(state="finished", bundle=relative, finished_at=_now(active_clock).isoformat(),
                             stop_reason=trial_stop_reason or state.status)
                journal["updated_at"] = _now(active_clock).isoformat()
                _write(directory / "evaluation.json", _seal_journal(journal))
                finished += 1
                if _serial_hazard(state):
                    halted = True
                    break
                if max_trials is not None and finished >= max_trials:
                    break
            journal.update(status="completed" if all(entry["state"] == "finished" for entry in journal["trials"])
                           else "interrupted" if halted else "running", updated_at=_now(active_clock).isoformat())
            _write(directory / "evaluation.json", _seal_journal(journal))
        except BaseException:
            journal.update(status="interrupted", updated_at=_now(active_clock).isoformat())
            _write(directory / "evaluation.json", _seal_journal(journal))
            raise
        finally:
            if store is not None:
                store.close()
    return load_evaluation(directory)


def default_arms(registry, *, include_planned=True):
    """Explicit immediate baselines, joint policies and stated dimension ablations."""
    from workflow.workload import STEP_ORDER
    pairs = [{"deployment_id": deployment.deployment_id, "model_id": model}
             for deployment in sorted(registry, key=lambda item: item.deployment_id) for model in sorted(deployment.models)]
    if not pairs or len(pairs) > 24:
        raise ValueError("Use an explicit arm file for this destination/model inventory")
    def arm(identity, allowed, *, policy="fixed", deferral=False, offsets=None):
        return {"arm_id": identity, "kind": "fixed" if policy == "fixed" else "planned", "policy": policy,
            "custom_weights": None, "allowed_pairs": {step: deepcopy(allowed) for step in STEP_ORDER},
            "allow_summary_deferral": deferral, "start_offsets_seconds": offsets or [0],
            "selection_source": "explicit-inventory-baseline" if policy == "fixed" else "frozen-dimension-constraints"}
    result = [arm("fixed-" + str(index + 1), [pair]) for index, pair in enumerate(pairs)]
    if include_planned:
        result.extend(arm("joint-" + policy, pairs, policy=policy, deferral=True, offsets=[0, 30, 60])
                      for policy in ("speed", "balanced", "eco"))
        origin = pairs[0]["deployment_id"]
        result.append(arm("model-only", [pair for pair in pairs if pair["deployment_id"] == origin], policy="balanced"))
        result.append(arm("without-time-shift", pairs, policy="balanced"))
        model = pairs[0]["model_id"]
        result.append(arm("destination-time-only", [pair for pair in pairs if pair["model_id"] == model],
                          policy="balanced", deferral=True, offsets=[0, 30, 60]))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="Prepare inputs and source snapshots; no inference or probes")
    freeze.add_argument("--output", required=True, type=Path)
    freeze.add_argument("--protocol-id", required=True)
    freeze.add_argument("--config")
    freeze.add_argument("--kb")
    for name in ("profiles", "dataset", "arms", "forecasts", "carbon", "tariffs"):
        freeze.add_argument("--" + name, type=Path)
    freeze.add_argument("--repeats", type=int, default=3)
    freeze.add_argument("--seed", type=int, default=0)
    freeze.add_argument("--quality-floor", type=float, default=.8)
    freeze.add_argument("--response-seconds", type=int, default=300)
    freeze.add_argument("--completion-seconds", type=int, default=600)
    freeze.add_argument("--timeout-seconds", type=float, default=120)
    freeze.add_argument("--poll-seconds", type=float, default=.5)
    freeze.add_argument("--energy", choices=("none", "codecarbon"), default="none")
    freeze.add_argument("--energy-country")
    freeze.add_argument("--calibrated-fixed-metric", choices=("latency", "cost", "energy"),
                        help="Add a reviewed training-median per-step fixed baseline")
    run = commands.add_parser("run", help="Execute/resume the frozen live protocol")
    run.add_argument("--evaluation", required=True, type=Path)
    run.add_argument("--max-trials", type=int)
    report = commands.add_parser("report", help="Inspect frozen evaluation evidence offline")
    report.add_argument("--evaluation", required=True, type=Path)
    export = commands.add_parser("export", help="Create a checksummed portable offline comparison")
    export.add_argument("--evaluation", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    inspect = commands.add_parser("inspect", help="Verify and recompute a portable comparison offline")
    inspect.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            from data.cost_accounting import TariffBook
            from shared.deployments import load_deployments
            from workflow.planning_records import decode_carbon
            from workflow.workload import KnowledgeBase, DEFAULT_TICKETS_PATH
            registry, kb = load_deployments(args.config), KnowledgeBase.load(args.kb)
            dataset = load_json(args.dataset) if args.dataset else {
                "dataset_id": "support-heldout-reused-pilot-v1", "split": "pilot",
                "provenance": "Previously used held-out calibration-error examples; not untouched final evaluation",
                "cases": [{"case_id": case["id"], "ticket": case["ticket"]}
                          for case in load_json(DEFAULT_TICKETS_PATH)["tickets"] if case["split"] == "held_out"]}
            profiles = load_json(args.profiles) if args.profiles else None
            from workflow.workflow_quality import default_review_config
            arms = load_json(args.arms) if args.arms else default_arms(registry, include_planned=profiles is not None)
            if args.calibrated_fixed_metric:
                from workflow.planning_constraints import calibrated_fixed_arm
                from workflow.workload import build_workflow_request
                if profiles is None:
                    raise ValueError("Calibration-selected baseline requires frozen profiles")
                preview = build_workflow_request(dataset["cases"][0]["ticket"], workflow_id="baseline-preview",
                    arrived_at=_now(SystemClock()), kb=kb, enable_thinking=False)
                arms.append(calibrated_fixed_arm(profiles, registry, preview, quality_floor=args.quality_floor,
                                                  metric=args.calibrated_fixed_metric))
            protocol = seal_protocol({"schema_version": 1, "record_type": "EvaluationProtocol",
                "output_quality": default_review_config(),
                "protocol_id": args.protocol_id, "experiment_mode": "live", "created_at": _now(SystemClock()).isoformat(),
                "dataset": dataset, "registry": registry.to_dict(),
                "knowledge_base": {"schema_version": 1, "kb_version": kb.version, "description": kb.description,
                    "categories": list(kb.categories), "articles": [article.to_dict() for article in kb.articles]},
                "profiles": profiles, "forecasts": load_json(args.forecasts) if args.forecasts else {
                    case["case_id"]: [] for case in dataset["cases"]},
                "carbon": decode_carbon(load_json(args.carbon)).to_dict() if args.carbon else None,
                "tariffs": TariffBook.load(args.tariffs).to_dict() if args.tariffs else None,
                "energy_mode": args.energy, "energy_country": args.energy_country,
                "arms": arms, "baseline_arm_id": arms[0]["arm_id"], "response_seconds": args.response_seconds,
                "completion_seconds": args.completion_seconds, "timeout_seconds": args.timeout_seconds,
                "quality_floor": args.quality_floor, "repeats": args.repeats, "seed": args.seed,
                "poll_seconds": args.poll_seconds, "planning_controls": DEFAULT_CONTROLS, "source_manifest": source_manifest()})
            directory = prepare_evaluation(protocol, args.output)
            print(json.dumps({"status": "prepared", "directory": str(directory), "protocol_fingerprint": protocol["fingerprint"],
                              "trials": len(schedule_trials(protocol)), "inference_performed": False}))
        elif args.command == "run":
            result = run_evaluation(args.evaluation, max_trials=args.max_trials)
            print(json.dumps(result, indent=2))
        else:
            from workflow.policy_comparison import load_evaluation, export_comparison, load_comparison
            if args.command == "report":
                print(json.dumps(load_evaluation(args.evaluation), indent=2))
            elif args.command == "export":
                print(json.dumps({"bundle": str(export_comparison(args.evaluation, args.output))}))
            else:
                print(json.dumps(load_comparison(args.bundle), indent=2))
        return 0
    except (ValueError, OSError, KeyError):
        print(json.dumps({"error_code": "invalid-policy-evaluation-input-or-state"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
