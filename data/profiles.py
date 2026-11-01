"""Versioned empirical profiles from retained, split-isolated calibration trials.

These profiles describe observed workloads rather than claiming stable latency
tails or semantic quality from output formatting. Latency uses a nonnegative
input-size trend when supported, otherwise a median of valid natural completions;
sample/residual deviation is descriptive uncertainty. Unknowns never become zeros. Held-out examples and
warm-ups are counted in the audit summary but cannot affect profile statistics
or fingerprints.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
import hashlib
import json
import math
import statistics
from typing import Any

from shared.scheduling import MAX_OUTPUT_LIMIT_TOKENS, MetricEstimate, StepEstimates
from data.quality_policy import quality_coverage, validate_quality_policy


PROFILE_SCHEMA_VERSION = 1
CONTEXT_SOURCE = "frozen-calibration-context-v1"
_PROVENANCE = {"unknown", "estimated", "measured", "mixed", "simulated"}
_FIELDS = {
    "schema_version", "record_type", "trial_id", "split", "context_source",
    "model_id", "deployment_id", "deployment_fingerprint", "step_id", "task_type",
    "workload_version", "input_size_estimate", "output_limit_tokens", "enable_thinking",
    "warmup", "cold_start", "status", "output_valid", "completion_reason",
    "latency_seconds", "load_seconds", "input_tokens", "output_tokens",
    "quality_score", "quality_source", "energy_kwh", "energy_provenance",
    "carbon_g", "cost_usd",
}
_IDENTITY = (
    "deployment_fingerprint", "model_id", "step_id", "task_type", "workload_version",
    "output_limit_tokens", "enable_thinking", "temperature", "carbon_unit",
)
_SETTINGS = ("output_limit_tokens", "enable_thinking", "temperature")
_MEASURES = {"cost": ("cost_usd", "USD"), "energy": ("energy_kwh", "kWh"),
             "carbon": ("carbon_g", "gCO2e")}


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _number(value: Any, name: str, *, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    try:
        valid = math.isfinite(value) and value >= 0 and (maximum is None or value <= maximum)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and within its allowed range")


def _integer(value: Any, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    _number(value, name)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _reviewed(source: str) -> bool:
    """Only an explicit reviewed rubric can establish semantic quality."""
    return source in {"manual-review", "human-review"} or source.startswith(
        ("manual-review:", "human-review:")
    ) and bool(source.split(":", 1)[1].strip())


def _validate_trial(trial: Any) -> dict:
    if not isinstance(trial, Mapping) or _FIELDS - trial.keys():
        raise ValueError("Calibration trial is missing required fields")
    # Additional raw-attempt and hardware metadata belongs to the retained
    # journal. Only this documented subset is copied to profile provenance.
    data = {name: trial[name] for name in _FIELDS}
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported calibration trial schema_version")
    if data["record_type"] != "CalibrationTrial":
        raise ValueError("Expected CalibrationTrial record")
    for name in ("trial_id", "model_id", "deployment_id", "deployment_fingerprint", "step_id",
                 "task_type", "workload_version", "quality_source"):
        _text(data[name], name)
    if data["context_source"] != CONTEXT_SOURCE:
        raise ValueError("Calibration requires frozen production-workload contexts")
    # The production adapter fixes temperature at 0.2. Record other benchmark
    # settings separately so they cannot silently train that production profile.
    data["temperature"] = trial.get("temperature", .2)
    _number(data["temperature"], "temperature", maximum=2)
    data["carbon_unit"] = trial.get("carbon_unit", "gCO2e")
    if data["carbon_unit"] not in ("gCO2", "gCO2e"):
        raise ValueError("carbon_unit must distinguish gCO2 from gCO2e")
    if not isinstance(data["split"], str) or data["split"] not in {"calibration", "held_out"}:
        raise ValueError("Unsupported calibration dataset split")
    for name in ("input_size_estimate", "output_limit_tokens"):
        _integer(data[name], name, minimum=1 if name == "output_limit_tokens" else 0)
    if data["output_limit_tokens"] > MAX_OUTPUT_LIMIT_TOKENS:
        raise ValueError("output_limit_tokens exceeds the supported limit")
    for name in ("warmup", "output_valid"):
        if type(data[name]) is not bool:
            raise ValueError(f"{name} must be a boolean")
    for name in ("enable_thinking", "cold_start"):
        if data[name] is not None and type(data[name]) is not bool:
            raise ValueError(f"{name} must be a boolean or None")
    if not isinstance(data["status"], str) or data["status"] not in {"completed", "failed"}:
        raise ValueError("Unsupported trial status")
    if data["completion_reason"] is not None:
        from shared.scheduling import COMPLETION_REASONS
        if not isinstance(data["completion_reason"], str) or data["completion_reason"] not in COMPLETION_REASONS:
            raise ValueError("Unsupported completion reason")
    if data["status"] == "failed" and data["output_valid"]:
        raise ValueError("Failed trials cannot have valid output")
    for name in ("latency_seconds", "load_seconds", "energy_kwh", "carbon_g", "cost_usd"):
        if data[name] is not None:
            _number(data[name], name)
    for name in ("input_tokens", "output_tokens"):
        if data[name] is not None:
            _integer(data[name], name)
    if data["quality_score"] is not None:
        _number(data["quality_score"], "quality_score", maximum=1)
    # Imported reviews bind the exact raw output and reviewer judgments. Keep
    # their content hash in fitted identity even when two reviews score alike.
    # Absent fields preserve the identity of older, unreviewed profiles.
    if "quality_review_fingerprint" in trial:
        digest = trial["quality_review_fingerprint"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or data["quality_score"] is None or not _reviewed(data["quality_source"])):
            raise ValueError("Quality review fingerprint requires a reviewed score and SHA256")
        data["quality_review_fingerprint"] = digest
        _text(trial.get("case_id"), "reviewed case_id")
        data["quality_case_id"] = trial["case_id"]
    # Formatting-only scores can be retained but never become accuracy. Their
    # source remains visible in quality_source_counts for review.
    for metric, (value_field, _unit) in _MEASURES.items():
        provenance = trial.get(f"{metric}_provenance", "unknown")
        source = trial.get(f"{metric}_source")
        if not isinstance(provenance, str) or provenance not in _PROVENANCE:
            raise ValueError(f"Unsupported {metric} provenance")
        if source is not None:
            _text(source, f"{metric}_source")
        if data[value_field] is None and provenance != "unknown":
            raise ValueError(f"Missing {metric} requires unknown provenance")
        if data[value_field] is not None:
            if provenance == "unknown":
                raise ValueError(f"Known {metric} requires explicit provenance")
            _text(source, f"{metric}_source")
        data[f"{metric}_provenance"] = provenance
        data[f"{metric}_source"] = source
    return data


def _stats(values: list[float | int]) -> dict:
    """Linear-interpolated p90 is descriptive even for a tiny sample."""
    if not values:
        return {name: 0 if name == "count" else None for name in
                ("count", "minimum", "maximum", "mean", "median", "p90", "stddev")}
    ordered = sorted(values)
    position = (len(ordered) - 1) * .9
    lower, upper = math.floor(position), math.ceil(position)
    p90 = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    result = {"count": len(ordered), "minimum": ordered[0], "maximum": ordered[-1],
              "mean": statistics.mean(ordered), "median": statistics.median(ordered),
              "p90": p90, "stddev": statistics.stdev(ordered) if len(ordered) > 1 else None}
    # Individual finite observations can still produce an overflowing aggregate.
    for name, value in result.items():
        if value is not None:
            _number(value, f"statistic {name}")
    return result


def _metric_stats(trials: list[dict], metric: str, *, unit: str | None = None) -> dict:
    field, default_unit = _MEASURES[metric]
    unit = unit or default_unit
    usable = [trial for trial in trials if trial[field] is not None
              and trial[f"{metric}_provenance"] != "simulated"]
    origins = Counter(trial[f"{metric}_provenance"] for trial in usable)
    # Missing observations are part of the coverage claim. A measured subset is
    # never described as a fully measured profile.
    provenance = ("unknown" if not usable else next(iter(origins)) if len(origins) == 1
                  and len(usable) == len(trials) else "mixed")
    return {"unit": unit, **_stats([trial[field] for trial in usable]),
            "provenance": provenance, "observed_count": len(usable),
            "missing_count": len(trials) - len(usable),
            "provenance_counts": dict(sorted(origins.items())),
            "sources": sorted({trial[f"{metric}_source"] for trial in usable})}


def _latency_model(trials: list[dict]) -> dict:
    """Fit a size trend only when at least three different sizes were observed.

    Output limit/thinking/workload/deployment are fixed by the parent profile.
    A negative slope or degenerate fit falls back to the descriptive median;
    small samples cannot justify a general scaling model.
    """
    sizes = [trial["input_size_estimate"] for trial in trials]
    fallback = {"method": "median", "distinct_input_sizes": len(set(sizes)),
                "slope_seconds_per_byte": None, "intercept_seconds": None,
                "residual_stddev_seconds": None}
    if len(set(sizes)) < 3:
        return fallback
    try:
        fit = statistics.linear_regression(sizes, [trial["latency_seconds"] for trial in trials])
        slope, intercept = fit.slope, fit.intercept
        if not math.isfinite(slope) or not math.isfinite(intercept) or slope < 0:
            return fallback
        residuals = [trial["latency_seconds"] - max(0., intercept + slope * trial["input_size_estimate"])
                     for trial in trials]
        residual_stddev = statistics.stdev(residuals)
        if not math.isfinite(residual_stddev):
            return fallback
    except (OverflowError, statistics.StatisticsError):
        return fallback
    return {"method": "nonnegative-input-size-slope", "distinct_input_sizes": len(set(sizes)),
            "slope_seconds_per_byte": slope, "intercept_seconds": intercept,
            "residual_stddev_seconds": residual_stddev}


def _profile(trials: list[dict], calibration_id: str, quality_policy=None) -> dict:
    first = trials[0]
    valid = [trial for trial in trials if trial["status"] == "completed" and trial["output_valid"]
             and trial["completion_reason"] == "stop"]
    latency_trials = [trial for trial in valid if trial["latency_seconds"] is not None]
    quality_trials = [trial for trial in valid if trial["quality_score"] is not None
                      and _reviewed(trial["quality_source"])]
    # A different rubric is a different observation scale. Keep its descriptive
    # scores, but do not pool multiple rubrics into one accuracy estimate.
    quality_sources = sorted({trial["quality_source"] for trial in quality_trials})
    quality = {"unit": "ratio", **_stats([trial["quality_score"] for trial in quality_trials]),
               "sources": quality_sources,
               "comparable": len(quality_sources) == 1,
               "provenance": "estimated" if len(quality_sources) == 1 else "unknown"}
    if quality_policy is not None:
        quality["coverage"] = quality_coverage(quality_policy, reviewed_trials=len(quality_trials),
            valid_trials=len(valid), case_ids=sorted({trial["quality_case_id"] for trial in quality_trials
                                                    if "quality_case_id" in trial}))
    ranges = [trial["input_size_estimate"] for trial in valid]
    states = {}
    for label, value in (("cold", True), ("warm", False), ("unknown", None)):
        selected = [trial for trial in trials if trial["cold_start"] is value]
        state_valid = [trial for trial in latency_trials if trial["cold_start"] is value]
        states[label] = {"attempt_count": len(selected),
                         "valid_latency": _stats([trial["latency_seconds"] for trial in state_valid]),
                         "load_seconds": _stats([trial["load_seconds"] for trial in selected
                                                 if trial["load_seconds"] is not None])}
    result = {
        "deployment_id": first["deployment_id"],
        **{name: first[name] for name in _IDENTITY if name not in _SETTINGS},
        "settings": {name: first[name] for name in _SETTINGS},
        "input_size": {"unit": "utf8-bytes-proxy", "minimum": min(ranges) if ranges else None,
                       "maximum": max(ranges) if ranges else None},
        "sample_counts": {"attempts": len(trials),
                          "completed": sum(t["status"] == "completed" for t in trials),
                          "failed": sum(t["status"] == "failed" for t in trials),
                          "valid_natural_completions": len(valid),
                          "invalid_output": sum(not t["output_valid"] for t in trials),
                          "truncated": sum(t["completion_reason"] == "length" for t in trials),
                          "reviewed_quality": len(quality_trials)},
        "metrics": {
            "latency": {"unit": "s", **_stats([t["latency_seconds"] for t in latency_trials]),
                        "provenance": "estimated" if latency_trials else "unknown"},
            "accuracy": quality,
            **{metric: _metric_stats(valid, metric,
                                    unit=first["carbon_unit"] if metric == "carbon" else None)
               for metric in _MEASURES},
        },
        "all_attempt_latency": _stats([t["latency_seconds"] for t in trials if t["latency_seconds"] is not None]),
        "latency_model": _latency_model(latency_trials),
        "input_tokens": _stats([t["input_tokens"] for t in valid if t["input_tokens"] is not None]),
        "output_tokens": _stats([t["output_tokens"] for t in valid if t["output_tokens"] is not None]),
        "start_states": states,
        "quality_source_counts": dict(sorted(Counter(t["quality_source"] for t in trials).items())),
        "trial_ids": sorted(t["trial_id"] for t in trials),
        "source_fingerprint": _fingerprint(sorted(trials, key=lambda t: t["trial_id"])),
        "calibration_id": calibration_id,
        "uncertainty_method": "sample-standard-deviation; descriptive, not a confidence interval",
        "tail_claim": "p90 is descriptive; these samples do not establish stable tail latency",
    }
    result["fingerprint"] = _fingerprint(result)
    return result


def _envelope_identity(value: dict) -> dict:
    # Audit counts change when a held-out record is added; calibration identity
    # must not. Raw held-out data and warm-ups never enter this payload.
    identity = {key: value[key] for key in
                ("schema_version", "record_type", "calibration_id", "context_source", "profiles")}
    if "quality_policy" in value:
        identity["quality_policy"] = value["quality_policy"]
    return identity


def build_profiles(trials: list[dict], *, calibration_id: str, quality_policy=None) -> dict:
    """Build JSON-safe exact-configuration profiles without held-out leakage.

    Every trial ID must be unique, including excluded records. Failed and
    truncated trials remain in each profile's audit counts. Arbitrary additional
    trial metadata is accepted but is not copied into the published profile.
    """
    _text(calibration_id, "calibration_id")
    if quality_policy is not None:
        quality_policy = validate_quality_policy(quality_policy)
    if not isinstance(trials, list):
        raise ValueError("trials must be a list")
    records = [_validate_trial(trial) for trial in trials]
    ids = [record["trial_id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Calibration trial IDs must be unique")
    eligible = [record for record in records if record["split"] == "calibration" and not record["warmup"]]
    deployment_labels: dict[str, str] = {}
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for record in eligible:
        fingerprint, label = record["deployment_fingerprint"], record["deployment_id"]
        if fingerprint in deployment_labels and deployment_labels[fingerprint] != label:
            raise ValueError("One deployment fingerprint has inconsistent deployment IDs")
        deployment_labels[fingerprint] = label
        grouped[tuple(record[name] for name in _IDENTITY)].append(record)
    profiles = [_profile(group, calibration_id, quality_policy) for group in grouped.values()]
    profiles.sort(key=lambda profile: profile["fingerprint"])
    result = {"schema_version": PROFILE_SCHEMA_VERSION, "record_type": "CalibrationProfiles",
              "calibration_id": calibration_id, "context_source": CONTEXT_SOURCE,
              "profiles": profiles,
              "summary": {"retained_trials": len(records), "profile_trials": len(eligible),
                          "held_out_excluded": sum(t["split"] == "held_out" for t in records),
                          "calibration_warmups_excluded": sum(t["split"] == "calibration" and t["warmup"]
                                                              for t in records),
                          "profile_count": len(profiles)}}
    if quality_policy is not None:
        result["quality_policy"] = quality_policy
    result["fingerprint"] = _fingerprint(_envelope_identity(result))
    return result


def _json_safe(value: Any) -> None:
    if value is None or type(value) in {str, bool}:
        return
    if isinstance(value, (float, int)):
        # A fitted intercept and signed held-out errors may legitimately be
        # negative. Their owning fields perform the range validation.
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("Profile numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _json_safe(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_safe(item)
        return
    raise ValueError("Profiles must contain only JSON-safe values")


def _validate_profiles(profiles: Any) -> None:
    expected = {"schema_version", "record_type", "calibration_id", "context_source", "profiles", "summary", "fingerprint"}
    if not isinstance(profiles, dict) or set(profiles) not in (expected, expected | {"quality_policy"}):
        raise ValueError("Invalid CalibrationProfiles envelope")
    policy = validate_quality_policy(profiles["quality_policy"]) if "quality_policy" in profiles else None
    if type(profiles["schema_version"]) is not int or profiles["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise ValueError("Unsupported profile schema_version")
    if profiles["record_type"] != "CalibrationProfiles" or profiles["context_source"] != CONTEXT_SOURCE:
        raise ValueError("Unsupported calibration profile record")
    _text(profiles["calibration_id"], "calibration_id")
    if not isinstance(profiles["profiles"], list):
        raise ValueError("profiles must be a list")
    _json_safe(profiles)
    identities = set()
    try:
        summary = profiles["summary"]
        if not isinstance(summary, dict) or set(summary) != {"retained_trials", "profile_trials", "held_out_excluded",
                                                           "calibration_warmups_excluded", "profile_count"}:
            raise ValueError("Invalid calibration audit summary")
        for count in summary.values():
            _integer(count, "audit summary count")
        if summary["profile_count"] != len(profiles["profiles"]) or summary["retained_trials"] != (
                summary["profile_trials"] + summary["held_out_excluded"] + summary["calibration_warmups_excluded"]):
            raise ValueError("Inconsistent calibration audit summary")
        for profile in profiles["profiles"]:
            if not isinstance(profile, dict) or profile.get("calibration_id") != profiles["calibration_id"]:
                raise ValueError("Invalid profile calibration identity")
            expected_fingerprint = _fingerprint({key: value for key, value in profile.items() if key != "fingerprint"})
            if profile.get("fingerprint") != expected_fingerprint:
                raise ValueError("Profile fingerprint does not match its content")
            for name in _IDENTITY:
                if name in _SETTINGS:
                    continue
                _text(profile[name], name)
            identity = tuple(profile[name] if name not in _SETTINGS
                             else profile["settings"][name] for name in _IDENTITY)
            if identity in identities:
                raise ValueError("Duplicate profile configuration")
            identities.add(identity)
            _integer(profile["settings"]["output_limit_tokens"], "profile output limit", minimum=1)
            if profile["settings"]["output_limit_tokens"] > MAX_OUTPUT_LIMIT_TOKENS:
                raise ValueError("Profile output limit exceeds supported range")
            thinking = profile["settings"]["enable_thinking"]
            if thinking is not None and type(thinking) is not bool:
                raise ValueError("Profile thinking setting must be boolean or None")
            _number(profile["settings"]["temperature"], "profile temperature", maximum=2)
            if profile["carbon_unit"] not in ("gCO2", "gCO2e"):
                raise ValueError("Unsupported profile carbon scope")
            _text(profile["deployment_id"], "deployment_id")
            for count in profile["sample_counts"].values():
                _integer(count, "profile sample count")
            attempts = profile["sample_counts"]["attempts"]
            valid = profile["sample_counts"]["valid_natural_completions"]
            if valid > attempts or profile["sample_counts"]["completed"] + profile["sample_counts"]["failed"] != attempts:
                raise ValueError("Inconsistent profile sample counts")
            if not isinstance(profile["trial_ids"], list) or len(profile["trial_ids"]) != attempts:
                raise ValueError("Profile trial IDs must match sample count")
            if len(set(profile["trial_ids"])) != attempts:
                raise ValueError("Profile trial IDs must be unique")
            for trial_id in profile["trial_ids"]:
                _text(trial_id, "profile trial_id")
            size = profile["input_size"]
            if size["unit"] != "utf8-bytes-proxy":
                raise ValueError("Unsupported profile input-size unit")
            if valid:
                _integer(size["minimum"], "minimum input size")
                _integer(size["maximum"], "maximum input size")
                if size["maximum"] < size["minimum"]:
                    raise ValueError("Invalid profile input-size range")
            elif size["minimum"] is not None or size["maximum"] is not None:
                raise ValueError("Profiles without valid output cannot establish a size range")
            for metric, unit in (("latency", "s"), ("accuracy", "ratio"), ("cost", "USD"),
                                 ("energy", "kWh"), ("carbon", "gCO2e")):
                stat = profile["metrics"][metric]
                _validate_stats(stat, maximum=1 if metric == "accuracy" else None)
                if metric == "carbon":
                    unit = profile["carbon_unit"]
                if stat["unit"] != unit or stat["count"] > valid:
                    raise ValueError("Invalid profile metric unit or observation count")
                if metric == "accuracy" and type(stat["comparable"]) is not bool:
                    raise ValueError("Profile quality comparability must be boolean")
                if metric == "accuracy":
                    if policy is not None:
                        coverage = stat.get("coverage")
                        if (not isinstance(coverage, dict) or type(coverage.get("eligible")) is not bool
                                or type(coverage.get("reviewed_trials")) is not int
                                or type(coverage.get("valid_trials")) is not int
                                or type(coverage.get("reviewed_fraction")) not in {int, float}
                                or coverage != quality_coverage(policy,
                                reviewed_trials=stat["count"], valid_trials=valid,
                                case_ids=coverage.get("distinct_case_ids"))):
                            raise ValueError("Profile quality coverage differs from its policy")
                    elif "coverage" in stat:
                        raise ValueError("Quality coverage requires its policy")
            model = profile["latency_model"]
            _integer(model["distinct_input_sizes"], "distinct input sizes")
            if model["method"] == "nonnegative-input-size-slope":
                _number(model["slope_seconds_per_byte"], "latency slope")
                if not isinstance(model["intercept_seconds"], (int, float)) or isinstance(model["intercept_seconds"], bool):
                    raise ValueError("Latency intercept must be finite")
                _number(model["residual_stddev_seconds"], "residual deviation")
                if model["distinct_input_sizes"] < 3:
                    raise ValueError("A size trend needs at least three distinct inputs")
            elif model["method"] != "median" or any(model[name] is not None for name in
                    ("slope_seconds_per_byte", "intercept_seconds", "residual_stddev_seconds")):
                raise ValueError("Unsupported latency model")
            for name in ("all_attempt_latency", "input_tokens", "output_tokens"):
                _validate_stats(profile[name])
            for state in profile["start_states"].values():
                _integer(state["attempt_count"], "start-state attempt count")
                _validate_stats(state["valid_latency"])
                _validate_stats(state["load_seconds"])
        if summary["profile_trials"] != sum(profile["sample_counts"]["attempts"] for profile in profiles["profiles"]):
            raise ValueError("Audit summary does not match profile trials")
        if profiles["fingerprint"] != _fingerprint(_envelope_identity(profiles)):
            raise ValueError("Calibration profile fingerprint does not match its content")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Malformed calibration profile") from exc


def _validate_stats(stat: Any, *, maximum: float | None = None) -> None:
    if not isinstance(stat, dict):
        raise ValueError("Profile statistics must be an object")
    _integer(stat["count"], "statistic count")
    names = ("minimum", "maximum", "mean", "median", "p90", "stddev")
    if not stat["count"]:
        if any(stat[name] is not None for name in names):
            raise ValueError("Empty statistics must preserve unknown values")
        return
    for name in names:
        if name == "stddev" and stat["count"] == 1:
            if stat[name] is not None:
                raise ValueError("One observation cannot establish sample deviation")
            continue
        _number(stat[name], f"statistic {name}", maximum=maximum if name != "stddev" else None)
    if not stat["minimum"] <= stat["median"] <= stat["p90"] <= stat["maximum"]:
        raise ValueError("Inconsistent profile quantiles")
    if not stat["minimum"] <= stat["mean"] <= stat["maximum"]:
        raise ValueError("Profile mean lies outside observations")


def _unknown(reason: str) -> StepEstimates:
    source = f"calibration-unknown:{reason}"
    return StepEstimates(*(MetricEstimate(None, unit, "unknown", source) for unit in
                           ("s", "ratio", "USD", "kWh", "gCO2e")))


def estimate_step(
    profiles: dict, *, deployment_fingerprint: str, model_id: str, step_id: str,
    workload_version: str, input_size_estimate: int, output_limit_tokens: int,
    enable_thinking: bool | None, allow_extrapolation: bool = False,
    task_type: str | None = None,
) -> StepEstimates:
    """Estimate only an exact deployment/model/workload/settings combination.

    The UTF-8 input-size proxy must fall within observed successful workloads.
    Three or more distinct observed sizes allow a nonnegative linear latency
    slope; an unsupported/negative trend uses the median. Explicit
    ``allow_extrapolation`` permits reuse of that model outside the range with
    an extrapolation label. Profile sample counts/ranges and cold/warm breakdown
    remain in the profile document. All predictions have estimated provenance
    even when their source observations were measured.

    Carbon mass retains its original scope (gCO2 or gCO2e). Otherwise matching
    profiles with different scopes are ambiguous, even when carbon itself is
    unobserved; the estimator never converts CO2 mass to CO2-equivalent mass.
    """
    _validate_profiles(profiles)
    if task_type is not None:
        _text(task_type, "task_type")
    for name, value in (("deployment_fingerprint", deployment_fingerprint), ("model_id", model_id),
                        ("step_id", step_id), ("workload_version", workload_version)):
        _text(value, name)
    _integer(input_size_estimate, "input_size_estimate")
    _integer(output_limit_tokens, "output_limit_tokens", minimum=1)
    if output_limit_tokens > MAX_OUTPUT_LIMIT_TOKENS:
        raise ValueError("output_limit_tokens exceeds the supported limit")
    if enable_thinking is not None and type(enable_thinking) is not bool:
        raise ValueError("enable_thinking must be a boolean or None")
    if type(allow_extrapolation) is not bool:
        raise ValueError("allow_extrapolation must be a boolean")
    matches = [profile for profile in profiles["profiles"]
               if profile["deployment_fingerprint"] == deployment_fingerprint
               and profile["model_id"] == model_id and profile["step_id"] == step_id
               and (task_type is None or profile["task_type"] == task_type)
               and profile["workload_version"] == workload_version
               and profile["settings"] == {"output_limit_tokens": output_limit_tokens,
                                           "enable_thinking": enable_thinking, "temperature": .2}]
    if len(matches) != 1:
        reason = "missing-profile"
        if matches:
            reason = ("ambiguous-carbon-scope" if len({profile["carbon_unit"] for profile in matches}) > 1
                      else "ambiguous-task-profile")
        return _unknown(reason)
    profile = matches[0]
    observed = profile["input_size"]
    if observed["minimum"] is None:
        return _unknown("no-valid-completions")
    in_range = observed["minimum"] <= input_size_estimate <= observed["maximum"]
    if not in_range and not allow_extrapolation:
        return _unknown("outside-observed-input-range")
    source = f"calibration:{profiles['calibration_id']}:{profile['fingerprint']}"
    if not in_range:
        source += ":extrapolated"
    estimates = {}
    for name, unit in (("latency", "s"), ("accuracy", "ratio"), ("cost", "USD"),
                       ("energy", "kWh"), ("carbon", "gCO2e")):
        stat = profile["metrics"][name]
        if name == "carbon":
            unit = profile["carbon_unit"]
        usable = stat["count"] > 0
        if name == "accuracy":
            usable = usable and stat["comparable"]
            usable = usable and ("quality_policy" not in profiles or stat["coverage"]["eligible"])
        if usable:
            value, uncertainty = stat["median"], stat["stddev"]
            metric_source = source
            if name == "latency" and profile["latency_model"]["method"] == "nonnegative-input-size-slope":
                model = profile["latency_model"]
                value = max(0., model["intercept_seconds"] + model["slope_seconds_per_byte"] * input_size_estimate)
                uncertainty = model["residual_stddev_seconds"]
                metric_source += ":input-size-linear"
            estimates[name] = MetricEstimate(value, unit, "estimated", metric_source, uncertainty)
        else:
            reason = ("quality-coverage-insufficient" if name == "accuracy" and "quality_policy" in profiles
                      and not stat["coverage"]["eligible"] else f"{name}-unobserved")
            estimates[name] = MetricEstimate(None, unit, "unknown", source + ":" + reason)
    return StepEstimates(**estimates)


def evaluate_profiles(profiles: dict, held_out_trials: list[dict]) -> dict:
    """Report held-out latency errors without modifying training profiles.

    Every supplied record must belong to the held-out split. Warm-ups, invalid
    completions, unknown profiles and workloads outside observed size ranges
    remain explicit per-trial exclusions. No implicit extrapolation is used.
    """
    _validate_profiles(profiles)
    if not isinstance(held_out_trials, list):
        raise ValueError("held_out_trials must be a list")
    records = [_validate_trial(trial) for trial in held_out_trials]
    ids = [record["trial_id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Held-out trial IDs must be unique")
    training_ids = {trial_id for profile in profiles["profiles"] for trial_id in profile["trial_ids"]}
    if training_ids.intersection(ids):
        raise ValueError("Held-out trials overlap training trial IDs")
    if any(record["split"] != "held_out" for record in records):
        raise ValueError("Evaluation accepts only held-out trials")
    rows, errors = [], []
    for trial in sorted(records, key=lambda record: record["trial_id"]):
        reason = None
        if trial["warmup"]:
            reason = "warmup"
        elif trial["status"] != "completed" or not trial["output_valid"] or trial["completion_reason"] != "stop":
            reason = "invalid-or-incomplete-output"
        elif trial["latency_seconds"] is None:
            reason = "missing-observed-latency"
        estimate = None
        if reason is None:
            prediction = estimate_step(profiles, **{name: trial[name] for name in
                ("deployment_fingerprint", "model_id", "step_id", "workload_version", "input_size_estimate",
                 "output_limit_tokens", "enable_thinking", "task_type")})
            estimate = prediction.latency.value
            if estimate is None:
                reason = prediction.latency.source.removeprefix("calibration-unknown:")
            elif prediction.carbon.unit != trial["carbon_unit"]:
                reason = "carbon-unit-mismatch"
                estimate = None
        error = estimate - trial["latency_seconds"] if reason is None else None
        if error is not None:
            errors.append(error)
        rows.append({"trial_id": trial["trial_id"], "observed_seconds": trial["latency_seconds"],
                     "estimated_seconds": estimate, "signed_error_seconds": error,
                     "absolute_error_seconds": abs(error) if error is not None else None,
                     "excluded_reason": reason})
    absolute = [abs(error) for error in errors]
    return {"schema_version": PROFILE_SCHEMA_VERSION, "record_type": "ProfileEvaluation",
            "profile_fingerprint": profiles["fingerprint"], "split": "held_out",
            "evaluated_count": len(errors), "excluded_count": len(records) - len(errors),
            "mean_absolute_error_seconds": statistics.mean(absolute) if errors else None,
            "mean_signed_error_seconds": statistics.mean(errors) if errors else None,
            "p90_absolute_error_seconds": _stats(absolute)["p90"], "trials": rows}
