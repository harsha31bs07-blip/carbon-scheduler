"""Explicit candidate-pair restrictions for reproducible planning comparisons.

Restrictions narrow configured capabilities; they never establish availability,
quality or location and never replace the common deployment/profile snapshot.
"""

from collections.abc import Mapping
from copy import deepcopy
import math
import re

from shared.deployments import DeploymentRegistry
from shared.scheduling import WorkflowRequest
from workflow.workload import STEP_ORDER


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_METRICS = ("latency", "accuracy", "cost", "energy", "carbon")


def validate_allowed_pairs(value, registry=None):
    """Validate all per-step choices, optionally checking exact capabilities.

    ``None`` preserves unrestricted legacy planning. A missing registry permits
    structural validation while constructing a context; durable installation
    checks the choices again against its saved registry.
    """
    if value is None:
        return None
    if registry is not None and not isinstance(registry, DeploymentRegistry):
        raise ValueError("Pair restrictions require a deployment registry")
    if not isinstance(value, Mapping) or set(value) != set(STEP_ORDER):
        raise ValueError("Pair restrictions must cover every workflow step")
    result = {}
    for step_id in STEP_ORDER:
        pairs = value[step_id]
        if not isinstance(pairs, (list, tuple)) or not 1 <= len(pairs) <= 1024:
            raise ValueError("Each step requires a bounded nonempty list of pairs")
        seen, accepted = set(), []
        for pair in pairs:
            if not isinstance(pair, Mapping) or set(pair) != {"deployment_id", "model_id"}:
                raise ValueError("A permitted pair requires exact deployment and model IDs")
            identity = pair["deployment_id"], pair["model_id"]
            if any(not isinstance(item, str) or _ID.fullmatch(item) is None for item in identity):
                raise ValueError("Permitted pair IDs must use configured identifier syntax")
            if identity in seen:
                raise ValueError("Permitted pairs must be unique per step")
            if registry is not None:
                registry.require_model(*identity)
            seen.add(identity)
            accepted.append({"deployment_id": identity[0], "model_id": identity[1]})
        result[step_id] = accepted
    return deepcopy(result)


def pair_permitted(allowed_pairs, step_id, deployment_id, model_id):
    """Test a candidate against an already validated restriction snapshot."""
    return allowed_pairs is None or any(
        pair["deployment_id"] == deployment_id and pair["model_id"] == model_id
        for pair in allowed_pairs[step_id])


def candidate_exclusion(candidate, allowed_pairs, *, live):
    """Use one deterministic exclusion precedence in preparation and replay."""
    if live and any(getattr(candidate.estimates, name).provenance == "simulated" for name in _METRICS):
        return "simulation-only-metrics"
    if not pair_permitted(allowed_pairs, candidate.step_id, candidate.deployment_id, candidate.model_id):
        return "pair-not-permitted"
    return None


def calibrated_fixed_arm(profiles, registry, request, *, arm_id="calibrated-fixed", quality_floor=.8,
                         metric="latency"):
    """Choose a frozen per-step baseline using calibration medians only.

    This selector uses no ticket answers or evaluation results. It requires
    reviewed quality coverage, exact production settings and known nonsimulated
    training medians. The choice is fixed for the subsequent experiment; actual
    prompts may still fall outside the calibration range or fail quality review.
    """
    from data.profiles import evaluate_profiles

    if not isinstance(registry, DeploymentRegistry) or not isinstance(request, WorkflowRequest):
        raise ValueError("A calibrated baseline requires a registry and preview request")
    if not isinstance(arm_id, str) or _ID.fullmatch(arm_id) is None:
        raise ValueError("Baseline arm ID must use identifier syntax")
    if not isinstance(metric, str) or metric not in {"latency", "cost", "energy"}:
        raise ValueError("Calibration selection supports latency, cost, or energy medians")
    try:
        valid_floor = type(quality_floor) in {int, float} and math.isfinite(quality_floor) and 0 <= quality_floor <= 1
    except OverflowError:
        valid_floor = False
    if not valid_floor:
        raise ValueError("Baseline quality floor must be finite and between zero and one")
    if tuple(step.step_id for step in request.steps) != STEP_ORDER:
        raise ValueError("Calibration selection requires the connected support workload")
    evaluate_profiles(profiles, [])
    if "quality_policy" not in profiles:
        raise ValueError("Calibration selection requires explicit reviewed coverage policy")
    for profile in profiles["profiles"]:
        deployment = registry.require_model(profile["deployment_id"], profile["model_id"])
        if deployment.config_fingerprint() != profile["deployment_fingerprint"]:
            raise ValueError("Calibration baseline profile differs from its deployment")
    selected = {}
    for step in request.steps:
        eligible = []
        matching = [profile for profile in profiles["profiles"]
            if profile["step_id"] == step.step_id and profile["task_type"] == step.task_type
            and profile["workload_version"] == request.workload_version
            and profile["settings"] == {"output_limit_tokens": step.output_limit_tokens,
                                        "enable_thinking": step.enable_thinking, "temperature": .2}]
        counts = {}
        for profile in matching:
            key = profile["deployment_fingerprint"], profile["model_id"]
            counts[key] = counts.get(key, 0) + 1
        for profile in matching:
            if counts[profile["deployment_fingerprint"], profile["model_id"]] != 1:
                # As in estimate_step, conflicting profile scopes cannot be
                # cherry-picked into a quality-eligible configuration.
                continue
            quality, target = profile["metrics"]["accuracy"], profile["metrics"][metric]
            if (quality["median"] is None or not quality["comparable"] or not quality["coverage"]["eligible"]
                    or quality["median"] < quality_floor or target["median"] is None
                    or target["provenance"] in {"unknown", "simulated"}):
                continue
            eligible.append((target["median"], profile["deployment_id"], profile["model_id"], profile["fingerprint"]))
        if not eligible:
            raise ValueError("No quality-eligible calibration baseline for every workflow step")
        _, deployment_id, model_id, _ = min(eligible)
        selected[step.step_id] = [{"deployment_id": deployment_id, "model_id": model_id}]
    return {"arm_id": arm_id, "kind": "fixed", "policy": "fixed", "custom_weights": None,
        "allowed_pairs": selected, "allow_summary_deferral": False, "start_offsets_seconds": [0],
        "selection_source": f"calibration-median-{metric}-v1:{profiles['fingerprint']}:quality-floor={quality_floor:g}"}
