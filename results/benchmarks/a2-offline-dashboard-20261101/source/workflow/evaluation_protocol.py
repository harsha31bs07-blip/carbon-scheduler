"""Frozen matched-evaluation inputs and protocol-bound trial identities.

Validation and scheduling are data-only. No provider transport, worker, mutable
configuration file or current clock is consulted by this module.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import math
from pathlib import PurePosixPath
import random
import re

from data.cost_accounting import TariffBook
from data.profiles import evaluate_profiles
from data.quality_reviews import fingerprint
from shared.deployments import DeploymentRegistry
from shared.scheduling import WorkloadSizeForecast
from workflow.execution_accounting import make_accounting_context
from workflow.planning_records import decode_carbon, decode_knowledge_base
from workflow.workload import STEP_ORDER, build_workflow_request


_FIELDS = {"schema_version", "record_type", "protocol_id", "experiment_mode", "created_at", "dataset",
    "registry", "knowledge_base", "profiles", "forecasts", "carbon", "tariffs", "energy_mode", "energy_country",
    "arms", "response_seconds", "completion_seconds", "timeout_seconds", "quality_floor", "repeats", "seed",
    "baseline_arm_id", "poll_seconds", "planning_controls", "source_manifest", "fingerprint"}
_ARM_FIELDS = {"arm_id", "kind", "policy", "custom_weights", "allowed_pairs", "allow_summary_deferral",
               "start_offsets_seconds", "selection_source"}
_CONTROLS = {"search_limit", "health_max_age_seconds", "minimum_improvement", "replan_interval_seconds",
             "probe_timeout_seconds", "max_decision_age_seconds"}
_TRIAL_FIELDS = {"trial_id", "case_id", "repeat", "arm_id", "workflow_id"}
_METRICS = {"latency", "accuracy", "cost", "energy", "carbon"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _malformed(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("Malformed evaluation protocol or trial evidence") from exc
    return checked


def _shape(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} requires exactly its supported fields")


def _identifier(value, name):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")


def _text(value, name, maximum=100000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be bounded nonempty text")


def _number(value, name, *, minimum=0, maximum=None, positive=False):
    try:
        valid = (type(value) in {int, float} and math.isfinite(value) and value >= minimum
                 and (maximum is None or value <= maximum) and (not positive or value > 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and within its allowed range")


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer within its allowed range")


def _time(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timezone-aware timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("naive timestamp")
        return instant.astimezone(timezone.utc)
    except ValueError:
        raise ValueError(f"{name} must be an ISO timezone-aware timestamp") from None


def _workload_fingerprint(request, kb):
    payload = {"input_text": request.input_text, "workload_version": request.workload_version,
               "knowledge_base": kb.fingerprint()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _source_manifest(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 4096:
        raise ValueError("Source manifest requires bounded path/checksum entries")
    seen = set()
    devices = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for name, checksum in value.items():
        if (not isinstance(name, str) or len(name) > 256 or "\\" in name or ":" in name
                or name.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or any(part in {"", ".", ".."} or part.endswith((" ", "."))
                       or part.split(".")[0].lower() in devices for part in name.split("/"))):
            raise ValueError("Source manifest paths must be safe relative POSIX paths")
        path = PurePosixPath(name)
        lower = name.lower()
        if (path.name.lower().startswith(".env") and path.name.lower() != ".env.example"
                or "deployments.local" in lower
                or any(word in lower for word in ("credentials", "secrets", "id_rsa", "id_ed25519"))):
            raise ValueError("Source manifest cannot include private configuration or credentials")
        if lower in seen:
            raise ValueError("Source paths must remain unique on case-insensitive filesystems")
        seen.add(lower)
        if not isinstance(checksum, str) or not _SHA.fullmatch(checksum):
            raise ValueError("Source manifest entries require lowercase SHA256 values")


def _controls(value):
    _shape(value, _CONTROLS, "Planning controls")
    _integer(value["search_limit"], "search limit", 1, 100000)
    for key in ("health_max_age_seconds", "replan_interval_seconds"):
        _number(value[key], key, maximum=86400)
    _number(value["minimum_improvement"], "minimum improvement", maximum=1)
    _number(value["probe_timeout_seconds"], "probe timeout", maximum=30, positive=True)
    _number(value["max_decision_age_seconds"], "decision age", maximum=86400, positive=True)


def _arm(value, registry):
    _shape(value, _ARM_FIELDS, "Evaluation arm")
    _identifier(value["arm_id"], "arm ID")
    if value["kind"] not in {"fixed", "planned"}:
        raise ValueError("Evaluation arm must be fixed or planned")
    _text(value["selection_source"], "selection source", maximum=4096)
    if type(value["allow_summary_deferral"]) is not bool:
        raise ValueError("Summary deferral must be boolean")
    pairs = value["allowed_pairs"]
    if not isinstance(pairs, dict) or set(pairs) != set(STEP_ORDER):
        raise ValueError("Arm pairs must specify every connected workflow step")
    for allowed in pairs.values():
        if not isinstance(allowed, list) or not 1 <= len(allowed) <= 1024:
            raise ValueError("Allowed model/deployment pairs require a bounded nonempty list")
        seen = set()
        for pair in allowed:
            _shape(pair, {"deployment_id", "model_id"}, "Allowed pair")
            registry.require_model(pair["deployment_id"], pair["model_id"])
            key = pair["deployment_id"], pair["model_id"]
            if key in seen:
                raise ValueError("Allowed model/deployment pairs must be unique")
            seen.add(key)
    offsets = value["start_offsets_seconds"]
    if not isinstance(offsets, list) or not 1 <= len(offsets) <= 128:
        raise ValueError("Arm start offsets require a bounded list")
    for offset in offsets:
        _number(offset, "start offset", maximum=604800)
    if len(set(offsets)) != len(offsets):
        raise ValueError("Arm start offsets must be unique")
    if value["kind"] == "fixed":
        if (value["policy"] != "fixed" or value["custom_weights"] is not None
                or value["allow_summary_deferral"] or offsets != [0]
                or any(len(allowed) != 1 for allowed in pairs.values())):
            raise ValueError("Fixed arms require one immediate explicit pair per step")
    else:
        policy = value["policy"]
        custom = value["custom_weights"]
        if policy not in {"speed", "balanced", "eco", "custom"}:
            raise ValueError("Planned arm requires an optimized policy")
        if policy == "custom":
            if not isinstance(custom, dict) or set(custom) != _METRICS:
                raise ValueError("Custom policy requires exactly the five weights")
            for weight in custom.values():
                _number(weight, "custom weight")
            total = math.fsum(custom.values())
            if not math.isfinite(total) or total <= 0:
                raise ValueError("Custom weights require a finite positive total")
        elif custom is not None:
            raise ValueError("Only custom policies accept custom weights")


def _calibrated_arms(arms, profiles, registry, preview, quality_floor):
    """Recognized calibration claims must reproduce the frozen selector."""
    for arm in arms:
        source = arm["selection_source"]
        if not source.startswith("calibration-median-"):
            continue
        matched = re.fullmatch(r"calibration-median-(latency|cost|energy)-v1:([0-9a-f]{64}):quality-floor=(.+)", source)
        if matched is None or profiles is None:
            raise ValueError("Calibrated baseline attestation requires its supported selector and frozen profiles")
        from workflow.planning_constraints import calibrated_fixed_arm
        expected = calibrated_fixed_arm(profiles, registry, preview, arm_id=arm["arm_id"],
            quality_floor=quality_floor, metric=matched.group(1))
        if arm != expected:
            raise ValueError("Calibrated baseline differs from its frozen selector, profiles or quality floor")


@_malformed
def validate_protocol(value):
    """Validate complete frozen inputs without assuming metric eligibility."""
    _shape(value, _FIELDS | ({"output_quality"} if isinstance(value, dict) and "output_quality" in value else set()), "Evaluation protocol")
    if "output_quality" in value:
        from workflow.workflow_quality import validate_review_config
        validate_review_config(value["output_quality"])
    fingerprint(value)  # Strict finite JSON, before downstream type operations.
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["record_type"] != "EvaluationProtocol"):
        raise ValueError("Unsupported evaluation protocol version")
    _identifier(value["protocol_id"], "protocol ID")
    if value["experiment_mode"] not in {"live", "simulation"}:
        raise ValueError("Evaluation experiment mode must be explicit")
    created = _time(value["created_at"], "protocol creation")
    dataset = value["dataset"]
    _shape(dataset, {"dataset_id", "split", "provenance", "cases"}, "Evaluation dataset")
    _identifier(dataset["dataset_id"], "dataset ID")
    if dataset["split"] not in {"pilot", "final"}:
        raise ValueError("Evaluation dataset split must be pilot or final")
    _text(dataset["provenance"], "dataset provenance", maximum=4096)
    cases = dataset["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise ValueError("Evaluation cases require a bounded nonempty list")
    ids, tickets = set(), set()
    for case in cases:
        _shape(case, {"case_id", "ticket"}, "Evaluation case")
        _identifier(case["case_id"], "case ID")
        _text(case["ticket"], "ticket")
        if case["case_id"] in ids or case["ticket"] in tickets:
            raise ValueError("Evaluation case IDs and tickets must be unique")
        ids.add(case["case_id"])
        tickets.add(case["ticket"])
    _integer(value["response_seconds"], "response budget", 1, 86400)
    _integer(value["completion_seconds"], "completion budget", value["response_seconds"], 86400)
    _number(value["timeout_seconds"], "provider timeout", positive=True, maximum=300)
    _number(value["quality_floor"], "quality floor", maximum=1)
    _integer(value["repeats"], "repeats", 1, 100)
    _integer(value["seed"], "randomization seed", 0, 2 ** 32 - 1)
    _number(value["poll_seconds"], "poll duration", minimum=.01, maximum=60)
    _controls(value["planning_controls"])
    _source_manifest(value["source_manifest"])
    registry = DeploymentRegistry.from_dict(value["registry"])
    if not 1 <= len(registry) <= 128:
        raise ValueError("Evaluation registry requires one to 128 destinations")
    kb = decode_knowledge_base(value["knowledge_base"])
    profiles = value["profiles"]
    if profiles is not None:
        evaluate_profiles(profiles, [])
        if len(profiles["profiles"]) > 1024:
            raise ValueError("Evaluation profiles exceed the supported bound")
    carbon = decode_carbon(value["carbon"])
    tariffs = TariffBook.from_dict(value["tariffs"]) if value["tariffs"] is not None else None
    arms = value["arms"]
    if not isinstance(arms, list) or not 2 <= len(arms) <= 32:
        raise ValueError("Matched evaluation requires two to 32 arms")
    arm_ids = set()
    for arm in arms:
        _arm(arm, registry)
        if arm["arm_id"] in arm_ids:
            raise ValueError("Evaluation arm IDs must be unique")
        arm_ids.add(arm["arm_id"])
        if arm["kind"] == "planned" and (profiles is None or "quality_policy" not in profiles):
            raise ValueError("Planned evaluation requires profiles with a frozen quality policy")
    selection_preview = build_workflow_request(cases[0]["ticket"], workflow_id="calibration-selection-preview",
        arrived_at=created, kb=kb, response_budget_seconds=value["response_seconds"],
        completion_budget_seconds=value["completion_seconds"], enable_thinking=False)
    _calibrated_arms(arms, profiles, registry, selection_preview, value["quality_floor"])
    if value["baseline_arm_id"] not in arm_ids:
        raise ValueError("Baseline must identify one frozen evaluation arm")
    if len(cases) * len(arms) * value["repeats"] > 10000:
        raise ValueError("Evaluation schedule exceeds 10000 trials")
    forecasts = value["forecasts"]
    if not isinstance(forecasts, dict) or set(forecasts) != ids:
        raise ValueError("Forecasts must explicitly cover each case, including empty lists")
    for case in cases:
        preview = build_workflow_request(case["ticket"], workflow_id="protocol-preview", arrived_at=created, kb=kb,
            response_budget_seconds=value["response_seconds"], completion_budget_seconds=value["completion_seconds"])
        make_accounting_context(registry, preview, tariff_book=tariffs, carbon_provider=carbon,
            energy_mode=value["energy_mode"], energy_country=value["energy_country"])
        items = forecasts[case["case_id"]]
        if not isinstance(items, list) or len(items) > len(STEP_ORDER):
            raise ValueError("Case forecasts must be a bounded list")
        seen = set()
        for supplied in items:
            item = WorkloadSizeForecast.from_dict(supplied)
            if (profiles is None or item.step_id not in STEP_ORDER or item.step_id in seen
                    or item.profile_fingerprint != profiles["fingerprint"]
                    or item.workflow_fingerprint != _workload_fingerprint(preview, kb)
                    or item.workload_version != preview.workload_version):
                raise ValueError("Case forecast differs from frozen workload/profile inputs")
            seen.add(item.step_id)
        if profiles is not None:
            for profile in profiles["profiles"]:
                deployment = registry.require_model(profile["deployment_id"], profile["model_id"])
                if (profile["deployment_fingerprint"] != deployment.config_fingerprint()
                        or profile["workload_version"] != preview.workload_version):
                    raise ValueError("Profiles differ from the frozen registry or workload")
    if (not isinstance(value["fingerprint"], str) or not _SHA.fullmatch(value["fingerprint"])
            or value["fingerprint"] != fingerprint({key: item for key, item in value.items() if key != "fingerprint"})):
        raise ValueError("Evaluation protocol fingerprint differs from its frozen content")
    return deepcopy(value)


@_malformed
def seal_protocol(value):
    """Hash and validate a complete caller-built protocol, without filling facts."""
    if not isinstance(value, dict):
        raise ValueError("Evaluation protocol must be an object")
    result = deepcopy(value)
    result.pop("fingerprint", None)
    result["fingerprint"] = fingerprint(result)
    return validate_protocol(result)


def _schedule(protocol):
    rng = random.Random(protocol["seed"])
    cases = sorted(case["case_id"] for case in protocol["dataset"]["cases"])
    arms = sorted(arm["arm_id"] for arm in protocol["arms"])
    blocks = [(case_id, repeat) for repeat in range(protocol["repeats"]) for case_id in cases]
    rng.shuffle(blocks)
    schedule = []
    for case_id, repeat in blocks:
        order = list(arms)
        rng.shuffle(order)
        for arm_id in order:
            identity = {"protocol_fingerprint": protocol["fingerprint"], "case_id": case_id,
                        "repeat": repeat, "arm_id": arm_id}
            trial_id = "trial-" + fingerprint(identity)[:24]
            schedule.append({"trial_id": trial_id, "case_id": case_id, "repeat": repeat,
                             "arm_id": arm_id, "workflow_id": trial_id})
    return schedule


def schedule_trials(protocol):
    """Randomize paired case/repeat blocks, then arm order inside each block."""
    return _schedule(validate_protocol(protocol))


@_malformed
def validate_trial_snapshot(protocol, trial, snapshot):
    """Bind already replay-validated evidence to one exact frozen trial.

    Callers accepting arbitrary snapshots must first validate their queue/replay
    semantics; this boundary adds protocol matching rather than replacing them.
    """
    protocol = validate_protocol(protocol)
    if not isinstance(trial, dict) or _TRIAL_FIELDS - set(trial):
        raise ValueError("Trial identity is missing required fields")
    identity = {key: trial[key] for key in _TRIAL_FIELDS}
    for key in ("trial_id", "case_id", "arm_id", "workflow_id"):
        _identifier(identity[key], key)
    _integer(identity["repeat"], "trial repeat", 0, protocol["repeats"] - 1)
    if identity not in _schedule(protocol):
        raise ValueError("Trial identity differs from the frozen randomized schedule")
    request = snapshot.request
    registry = DeploymentRegistry.from_dict(protocol["registry"])
    kb = decode_knowledge_base(protocol["knowledge_base"])
    case = next(case for case in protocol["dataset"]["cases"] if case["case_id"] == identity["case_id"])
    arm = next(arm for arm in protocol["arms"] if arm["arm_id"] == identity["arm_id"])
    if request.arrived_at < _time(protocol["created_at"], "protocol creation"):
        raise ValueError("Trial cannot precede its frozen protocol")
    expected = build_workflow_request(case["ticket"], workflow_id=identity["workflow_id"], arrived_at=request.arrived_at,
        response_budget_seconds=protocol["response_seconds"], completion_budget_seconds=protocol["completion_seconds"],
        policy=arm["policy"], kb=kb, enable_thinking=False)
    if not arm["allow_summary_deferral"]:
        expected = replace(expected, steps=tuple(replace(step, allow_deferral=False) if step.step_id == "summarize"
                                                else step for step in expected.steps))
    if request != expected:
        raise ValueError("Trial request differs from the frozen ticket, budgets, generation controls or arm")
    config = snapshot.configuration
    if (config.get("registry_snapshot") != protocol["registry"]
            or config.get("registry_fingerprint") != registry.config_fingerprint()
            or config.get("kb_fingerprint") != kb.fingerprint()
            or config.get("workload_fingerprint") != _workload_fingerprint(request, kb)
            or config.get("timeout_seconds") != protocol["timeout_seconds"]
            or config.get("experiment_mode") != protocol["experiment_mode"]
            or config.get("accounting_version") != "execution-accounting.v1"):
        raise ValueError("Trial execution configuration differs from frozen protocol inputs")
    carbon = decode_carbon(protocol["carbon"])
    tariffs = TariffBook.from_dict(protocol["tariffs"]) if protocol["tariffs"] is not None else None
    accounting = make_accounting_context(registry, request, tariff_book=tariffs, carbon_provider=carbon,
        energy_mode=protocol["energy_mode"], energy_country=protocol["energy_country"])
    if snapshot.accounting_context != accounting:
        raise ValueError("Trial accounting inputs differ from the frozen protocol")
    if arm["kind"] == "planned":
        context = {"schema_version": 1, "record_type": "DurablePlanningContext", "profiles": protocol["profiles"],
            "forecasts": protocol["forecasts"][case["case_id"]], "knowledge_base": protocol["knowledge_base"],
            "carbon": protocol["carbon"], "custom_weights": arm["custom_weights"], "quality_floor": protocol["quality_floor"],
            "start_offsets_seconds": arm["start_offsets_seconds"], **protocol["planning_controls"],
            "allowed_pairs": arm["allowed_pairs"]}
        if (snapshot.planning_context != context or not snapshot.decisions
                or config.get("worker_version") != "sqlite-planned-worker-v1"
                or config.get("policy_version") != "joint-calibrated-v1"
                or config.get("calibration_id") != protocol["profiles"]["calibration_id"]):
            raise ValueError("Trial planned context differs from frozen inputs and arm restrictions")
    elif (snapshot.planning_context is not None or snapshot.decisions
            or config.get("worker_version") != "sqlite-worker-v1"
            or config.get("policy_version") != "fixed-explicit-v1"):
        raise ValueError("Fixed trial cannot contain a planned worker or decision history")
    for candidate in (*snapshot.plan_history, *(job.candidate for job in snapshot.jobs),
                      *(attempt.planned_candidate for attempt in snapshot.attempts)):
        allowed = arm["allowed_pairs"].get(candidate.step_id, ())
        if {"deployment_id": candidate.deployment_id, "model_id": candidate.model_id} not in allowed:
            raise ValueError("Trial selected a model/deployment excluded by its arm")
        if arm["kind"] == "fixed" and candidate.planned_start != request.arrived_at:
            raise ValueError("Fixed baseline must retain immediate explicit assignments")
    return deepcopy(identity)
