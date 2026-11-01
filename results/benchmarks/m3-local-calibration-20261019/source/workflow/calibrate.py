"""Retain local calibration trials using production prompts and adapters.

Each task uses explicitly frozen, validated dependency context so upstream model
errors cannot silently remove difficult downstream trials. These are per-task
profiles, not connected-workflow quality or end-to-end savings measurements.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import time
from uuid import uuid4

import requests

from data.carbon_sources import HistoricalCarbonTrace, StaticCarbonEstimate
from data.cost_accounting import TariffBook, local_api_tariff
from data.profiles import build_profiles, evaluate_profiles
from shared.deployments import load_deployments
from shared.energy import EnergySession, is_loopback_endpoint
from shared.run_records import code_revision, write_json
from workflow.deployment_clients import DeploymentCallError, call_deployment
from workflow.workload import (
    DEFAULT_TICKETS_PATH, NO_EVIDENCE, STEP_ORDER, KnowledgeBase,
    build_workflow_request, render_step, validate_step_output,
)


def _now():
    return datetime.now(timezone.utc)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_cases(path: str | Path = DEFAULT_TICKETS_PATH, *, split: str = "calibration") -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("tickets"), list):
        raise ValueError("Invalid versioned calibration cases")
    cases = data["tickets"]
    if any(not isinstance(case, dict) or not isinstance(case.get("id"), str) for case in cases):
        raise ValueError("Calibration cases require IDs")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Calibration case IDs must be unique")
    selected = [case for case in cases if case.get("split") == split]
    if not selected or split not in {"calibration", "held_out"}:
        raise ValueError("No cases in the requested split")
    return selected


def frozen_context(case: dict, kb: KnowledgeBase) -> dict[str, str]:
    """Fixture context is never misrepresented as this model's earlier output."""
    category = case["expected_category"]
    articles = kb.lookup(category, case["ticket"])
    retrieval = "\n".join(f"{article.body} [{article.source_id}]" for article in articles) or NO_EVIDENCE
    draft = (f"Support advised reviewing the supplied guidance [{articles[0].source_id}]."
             if articles else "Support advised that verified guidance is unavailable and offered human escalation.")
    outputs = {"classify": category, "retrieve": retrieval, "draft": draft}
    validate_step_output("classify", category)
    validate_step_output("retrieve", retrieval, source_ids=tuple(article.source_id for article in articles))
    validate_step_output("draft", draft, source_ids=tuple(article.source_id for article in articles))
    return outputs


def _resident(deployment, provider_model):
    if deployment.provider != "ollama":
        return None
    response = None
    try:
        response = requests.get(deployment.endpoint.rstrip("/") + "/api/ps", timeout=(2, 3),
                                allow_redirects=False)
        if response.status_code != 200:
            return None
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("models"), list):
            return None
        return any(isinstance(item, dict) and provider_model in {item.get("name"), item.get("model")}
                   for item in body["models"])
    except (requests.RequestException, ValueError, TypeError):
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def hardware_snapshot() -> dict:
    result = {"platform": platform.platform(), "python": platform.python_version(),
              "processor": platform.processor(), "gpu_query": None}
    try:
        queried = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,power.draw,power.limit,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        result["gpu_query"] = queried.stdout.strip()
        result["gpu_query_scope"] = "point-in-time capability, not trial energy"
    except (OSError, subprocess.SubprocessError):
        result["gpu_query_status"] = "unavailable"
    return result


def _tracker_factory(enabled, country_iso_code=None):
    if not enabled:
        return None
    def factory(**kwargs):
        # Optional telemetry initialization belongs inside EnergySession so its
        # failure cannot prevent or repeat inference.
        if country_iso_code is not None:
            from codecarbon import OfflineEmissionsTracker
            return OfflineEmissionsTracker(country_iso_code=country_iso_code, **kwargs)
        from codecarbon import EmissionsTracker
        return EmissionsTracker(**kwargs)
    return factory


def run_calibration(
    registry, *, directory: str | Path, deployment_id: str, models: list[str],
    trials: int = 10, split: str = "calibration", kb: KnowledgeBase | None = None,
    cases_path: str | Path = DEFAULT_TICKETS_PATH, timeout_seconds: float = 120,
    energy: bool = False, carbon_provider=None, dispatcher=None,
    residency_probe=None, tracker_factory=None, progress=True, energy_country_iso_code=None,
) -> dict:
    if type(trials) is not int or not 1 <= trials <= 1000:
        raise ValueError("trials must be an integer from 1 to 1000")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
        raise ValueError("timeout_seconds must be between 0 and 300")
    if (not isinstance(models, list) or not models
            or any(not isinstance(model, str) or not model.strip() for model in models)
            or len(set(models)) != len(models)):
        raise ValueError("models must be a nonempty unique list")
    deployment = registry.get(deployment_id)
    for model in models:
        deployment.provider_model(model)
    if len({deployment.provider_model(model) for model in models}) != len(models):
        raise ValueError("Calibration model IDs must resolve to distinct provider models")
    if type(energy) is not bool or type(progress) is not bool:
        raise ValueError("energy and progress must be booleans")
    knowledge = KnowledgeBase.load() if kb is None else kb
    if not isinstance(knowledge, KnowledgeBase):
        raise ValueError("kb must be a KnowledgeBase")
    cases = load_cases(cases_path, split=split)
    prepared = {case["id"]: frozen_context(case, knowledge) for case in cases}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    started = _now()
    manifest = {"schema_version": 1, "record_type": "CalibrationRun", "status": "running",
                "run_id": uuid4().hex, "started_at": started.isoformat(), "split": split,
                "context_source": "frozen-calibration-context-v1", "requested_trials_per_profile": trials,
                "registry": registry.to_dict(), "registry_fingerprint": registry.config_fingerprint(),
                "kb_fingerprint": knowledge.fingerprint(), "cases_fingerprint": _fingerprint(cases),
                "hardware": hardware_snapshot(), "code_revision": code_revision(),
                "energy_requested": energy, "carbon_configured": carbon_provider is not None}
    write_json(directory / "manifest.json", manifest)
    call = dispatcher or call_deployment
    probe = residency_probe or _resident
    factory = tracker_factory if tracker_factory is not None else _tracker_factory(energy, energy_country_iso_code)
    tariff = (TariffBook(tuple(local_api_tariff(
        deployment.provider_model(model), verified_at=started, valid_from=started,
        valid_until=started + timedelta(days=1), deployment_fingerprint=deployment.config_fingerprint(),
    ) for model in dict.fromkeys(models))) if deployment.provider == "ollama" and is_loopback_endpoint(deployment.endpoint)
              else TariffBook(()))
    retained = []
    try:
        for model in models:
            for step_id in STEP_ORDER:
                for index in range(-1, trials):
                    case = cases[max(index, 0) % len(cases)]
                    request = build_workflow_request(case["ticket"], workflow_id=uuid4().hex,
                        arrived_at=_now(), kb=knowledge, response_budget_seconds=3600,
                        completion_budget_seconds=7200,
                        enable_thinking=False if deployment.provider == "ollama" else None)
                    rendered = render_step(request, step_id, prepared[case["id"]], kb=knowledge)
                    trial_id = uuid4().hex
                    path = directory / (trial_id + ".json")
                    resident = probe(deployment, deployment.provider_model(model))
                    begin = _now()
                    trial = {"schema_version": 1, "record_type": "CalibrationTrial", "trial_id": trial_id,
                        "case_id": case["id"], "split": split, "context_source": "frozen-calibration-context-v1",
                        "model_id": model, "deployment_id": deployment.deployment_id,
                        "deployment_fingerprint": deployment.config_fingerprint(), "step_id": step_id,
                        "task_type": rendered.step.task_type, "workload_version": request.workload_version,
                        "input_size_estimate": rendered.step.input_tokens, "input_size_source": rendered.step.input_size_source,
                        "output_limit_tokens": rendered.step.output_limit_tokens, "enable_thinking": rendered.step.enable_thinking,
                        "warmup": index == -1, "cold_start": None if resident is None else not resident,
                        "temperature": 0.2,
                        "status": "running", "output_valid": False, "completion_reason": None,
                        "latency_seconds": None, "load_seconds": None, "input_tokens": None, "output_tokens": None,
                        "quality_score": None, "quality_source": "unknown", "energy_kwh": None,
                        "energy_provenance": "unknown", "energy_source": None,
                        "carbon_g": None, "carbon_provenance": "unknown", "carbon_source": None,
                        "carbon_unit": "gCO2e",
                        "cost_usd": None, "cost_provenance": "unknown", "cost_source": None,
                        "prompt": rendered.step.prompt, "source_ids": list(rendered.source_ids),
                        "input_fingerprint": rendered.input_fingerprint, "lookup_seconds": rendered.lookup_seconds,
                        "started_at": begin.isoformat(), "error_code": None}
                    write_json(path, trial)  # A crash leaves the uncompleted trial visible.
                    telemetry = EnergySession(deployment.endpoint, tracker_factory=factory)
                    clock_start = time.perf_counter()
                    call_start = None
                    try:
                        with telemetry:
                            call_start = time.perf_counter()
                            result = call(deployment, model, rendered.step.prompt,
                                timeout_seconds=timeout_seconds, max_attempts=1,
                                output_limit_tokens=rendered.step.output_limit_tokens,
                                enable_thinking=rendered.step.enable_thinking)
                        trial.update(status="completed", result=result,
                            completion_reason=result.get("completion_reason"),
                            latency_seconds=result["latency_ms"] / 1000,
                            load_seconds=result.get("provider_timings_seconds", {}).get("load"),
                            input_tokens=result["input_tokens"], output_tokens=result["output_tokens"])
                        reported = result.get("reported_model_id")
                        trial["identity_verification"] = ("matched" if reported == deployment.provider_model(model)
                            else "missing" if reported is None else "mismatched")
                        if deployment.provider == "ollama" and trial["identity_verification"] != "matched":
                            trial["error_code"] = "model-identity-unverified"
                        elif trial["completion_reason"] != "stop":
                            trial["error_code"] = "generation-not-natural-stop"
                        else:
                            try:
                                validate_step_output(step_id, result["text"], source_ids=rendered.source_ids)
                                trial["output_valid"] = True
                            except ValueError:
                                trial["error_code"] = "step-output-invalid"
                    except DeploymentCallError as exc:
                        trial.update(status="failed", error_code=exc.error_code,
                                     request_attempted=exc.request_attempted,
                                     latency_seconds=time.perf_counter() - (call_start or clock_start))
                    end = _now()
                    trial.update(finished_at=end.isoformat(), trial_wall_seconds=time.perf_counter() - clock_start)
                    # Preserve completed output/usage before best-effort accounting.
                    write_json(path, trial)
                    enrichment_errors = []
                    try:
                        report = telemetry.report(trial["trial_wall_seconds"] * 1000)
                    except Exception:
                        report = {"energy_wh": None, "energy_provenance": "unknown", "energy_method": "unknown"}
                        enrichment_errors.append("energy-report-failed")
                    trial["energy_report"] = report
                    if report["energy_wh"] is not None:
                        trial.update(energy_kwh=report["energy_wh"] / 1000,
                                     energy_provenance=report["energy_provenance"], energy_source=report["energy_method"])
                    try:
                        cost = tariff.estimate_usage(deployment.provider, deployment.provider_model(model),
                            trial["input_tokens"], trial["output_tokens"], instant=begin, as_of=end,
                            deployment_fingerprint=deployment.config_fingerprint())
                        trial.update(cost_usd=cost.value, cost_provenance=cost.provenance, cost_source=cost.source)
                    except Exception:
                        enrichment_errors.append("cost-accounting-failed")
                    sample = None
                    try:
                        sample = carbon_provider.sample(deployment.grid, begin, as_of=begin) if carbon_provider else None
                        trial["carbon_status"] = (carbon_provider.availability(deployment.grid, begin, as_of=begin).status
                                                  if carbon_provider else "unconfigured")
                    except Exception:
                        trial["carbon_status"] = "source-failed"
                        enrichment_errors.append("carbon-source-failed")
                    if sample is not None:
                        trial["carbon_sample"] = sample.to_dict()
                        trial["carbon_unit"] = sample.unit.removesuffix("/kWh")
                        product = trial["energy_kwh"] * sample.value if trial["energy_kwh"] is not None else None
                        if product is not None and not math.isfinite(product):
                            trial["carbon_status"] = "carbon-calculation-overflow"
                        elif product is not None and end <= sample.valid_until:
                            trial.update(carbon_g=product,
                                carbon_provenance=("simulated" if "simulated" in
                                    {trial["energy_provenance"], sample.provenance} else "estimated"),
                                carbon_source="energy-times-" + sample.source)
                        elif end > sample.valid_until:
                            trial["carbon_status"] = "execution-outside-sample-coverage"
                    trial["enrichment_errors"] = enrichment_errors
                    write_json(path, trial)
                    retained.append(trial)
                    if progress:
                        print(json.dumps({"model": model, "step": step_id, "trial": index,
                            "status": trial["status"], "output_valid": trial["output_valid"],
                            "seconds": trial["latency_seconds"]}), flush=True)
        profiles = build_profiles(retained, calibration_id=manifest["run_id"])
        write_json(directory / "profiles.json", profiles)
        manifest.update(status="completed", finished_at=_now().isoformat(), retained_trials=len(retained),
                        profiles_fingerprint=profiles["fingerprint"])
        write_json(directory / "manifest.json", manifest)
        return {"directory": str(directory), "manifest": manifest, "profiles": profiles}
    except BaseException:
        manifest.update(status="interrupted", finished_at=_now().isoformat(), retained_trials=len(retained))
        write_json(directory / "manifest.json", manifest)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--deployment", default="local-ollama")
    parser.add_argument("--models", nargs="+", default=["local-llama3.2", "local-qwen3.5"])
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--split", choices=("calibration", "held_out"), default="calibration")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--energy", choices=("none", "codecarbon"), default="none")
    parser.add_argument("--energy-country", help="ISO alpha-3 country for offline telemetry setup (e.g. IND)")
    carbon = parser.add_mutually_exclusive_group()
    carbon.add_argument("--carbon-trace")
    carbon.add_argument("--carbon-estimate", help="Explicit historical/static estimate JSON, not a live forecast")
    args = parser.parse_args(argv)
    try:
        registry = load_deployments(args.config)
        if registry.get(args.deployment).provider != "ollama":
            raise ValueError("This calibration CLI currently supports explicit Ollama deployments")
        provider = HistoricalCarbonTrace.load(args.carbon_trace) if args.carbon_trace else None
        if args.carbon_estimate:
            provider = StaticCarbonEstimate.from_dict(json.loads(Path(args.carbon_estimate).read_text(encoding="utf-8-sig")))
        result = run_calibration(registry, directory=args.output, deployment_id=args.deployment,
            models=args.models, trials=args.trials, split=args.split, timeout_seconds=args.timeout_seconds,
            energy=args.energy == "codecarbon", energy_country_iso_code=args.energy_country, carbon_provider=provider)
        print(json.dumps({"status": result["manifest"]["status"], "directory": result["directory"],
                          "summary": result["profiles"]["summary"]}, indent=2))
        return 0
    except (ValueError, OSError):
        print(json.dumps({"error_code": "invalid-calibration-configuration-or-evidence"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
