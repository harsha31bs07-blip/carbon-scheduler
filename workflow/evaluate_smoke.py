"""Run the legacy 16-item rubric smoke through explicit deployment adapters.

Scores are keyword/format proxies, even with live outputs. They never populate
the calibrated semantic-quality field or overwrite the legacy result file.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from data.eval.run_eval import load_eval_set, score_response
from shared.deployments import load_deployments
from shared.scheduling import MAX_OUTPUT_LIMIT_TOKENS
from shared.run_records import write_json
from workflow.deployment_clients import DeploymentCallError, _validate_timeout, call_deployment


def evaluate_smoke(registry, deployment_id, models, *, output: str | Path,
                   timeout_seconds=120, output_limit_tokens=512, dispatcher=None, progress=True):
    timeout_seconds = _validate_timeout(timeout_seconds)
    if type(output_limit_tokens) is not int or not 1 <= output_limit_tokens <= MAX_OUTPUT_LIMIT_TOKENS:
        raise ValueError("Invalid output limit")
    if type(progress) is not bool:
        raise ValueError("progress must be boolean")
    deployment = registry.get(deployment_id)
    if deployment.provider != "ollama":
        raise ValueError("This local smoke requires an explicit Ollama deployment")
    if not isinstance(models, list) or not models or any(not isinstance(model, str) for model in models):
        raise ValueError("models must be a nonempty list")
    if len(set(models)) != len(models):
        raise ValueError("models must be unique")
    for model in models:
        deployment.provider_model(model)
    output = Path(output)
    if output.exists():
        raise FileExistsError("Smoke output already exists")
    items = load_eval_set()
    dataset = json.dumps(items, sort_keys=True, ensure_ascii=False).encode()
    evidence = {"schema_version": 1, "record_type": "LegacyEvalSmoke", "status": "running",
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(), "source": "live-explicit-adapter",
        "score_source": "keyword-format-proxy-v1", "score_provenance": "estimated",
        "is_measured": False, "semantic_quality_status": "unreviewed",
        "deployment": deployment.to_dict(), "models": models, "records": [],
        "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(output, evidence)
    call = dispatcher or call_deployment
    try:
        for model in models:
            for item in items:
                record = {"model_id": model, "item": item, "status": "running", "score": None,
                          "started_at": datetime.now(timezone.utc).isoformat(), "error_code": None}
                evidence["records"].append(record)
                write_json(output, evidence)
                try:
                    result = call(deployment, model, item["prompt"], timeout_seconds=timeout_seconds,
                        output_limit_tokens=output_limit_tokens, enable_thinking=False, max_attempts=1)
                    record.update(status="completed", result=result)
                    if result.get("reported_model_id") != deployment.provider_model(model):
                        record["error_code"] = "model-identity-unverified"
                    elif result.get("completion_reason") != "stop":
                        record["error_code"] = "generation-not-natural-stop"
                    else:
                        record["score"] = score_response(result["text"], item)
                except DeploymentCallError as exc:
                    record.update(status="failed", error_code=exc.error_code,
                                  request_attempted=exc.request_attempted)
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                write_json(output, evidence)
                if progress:
                    print(json.dumps({"model": model, "item": item["id"], "status": record["status"],
                                      "proxy_score": record["score"], "error": record["error_code"]}), flush=True)
        evidence.update(status="completed", finished_at=datetime.now(timezone.utc).isoformat())
        write_json(output, evidence)
        return evidence
    except BaseException:
        evidence.update(status="interrupted", finished_at=datetime.now(timezone.utc).isoformat())
        write_json(output, evidence)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--deployment", default="local-ollama")
    parser.add_argument("--models", nargs="+", default=["local-llama3.2", "local-qwen3.5"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        result = evaluate_smoke(load_deployments(args.config), args.deployment, args.models,
                                output=args.output, timeout_seconds=args.timeout_seconds)
        print(json.dumps({"status": result["status"], "records": len(result["records"]),
                          "semantic_quality_status": result["semantic_quality_status"]}))
        return 0
    except (ValueError, OSError):
        print(json.dumps({"error_code": "invalid-evaluation-configuration"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
