"""Create explicitly simulated, fully executed evidence without an inference host."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from shared.deployments import Deployment, DeploymentRegistry
from shared.scheduling import ExecutionAttempt
from workflow.evaluate_policies import DEFAULT_CONTROLS, default_arms, source_manifest, prepare_evaluation, run_evaluation
from workflow.evaluation_protocol import seal_protocol
from workflow.workflow_quality import default_review_config
from workflow.workload import KnowledgeBase, NO_EVIDENCE


class _Clock:
    def __init__(self):
        self.time = datetime.now(timezone.utc)

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += timedelta(seconds=seconds)


def create_demo(output, *, work_directory=None):
    """Exercise the real queue/accounting/export path with scripted outputs only."""
    from workflow.planned_worker import knowledge_snapshot
    from workflow.policy_comparison import export_comparison
    output = Path(output)
    if output.exists():
        raise FileExistsError("Demo output already exists")
    clock, kb = _Clock(), KnowledgeBase.load()
    registry = DeploymentRegistry([Deployment("scripted-fixture", "ollama", "http://simulation.invalid:11434",
        {"small": "fixture:small", "large": "fixture:large"})])
    cases = [{"case_id": "demo-account", "ticket": "My CarbonDesk account is locked after failed login attempts."},
             {"case_id": "demo-invoice", "ticket": "I own the CarbonDesk workspace and need to download last month's invoice."}]
    arms = default_arms(registry, include_planned=False)
    protocol = seal_protocol({"schema_version": 1, "record_type": "EvaluationProtocol",
        "protocol_id": "offline-dashboard-demonstration.v1", "experiment_mode": "simulation",
        "created_at": clock.now().isoformat(), "dataset": {"dataset_id": "scripted-demo.v1", "split": "pilot",
            "provenance": "Scripted synthetic outputs and clock; no inference, human judgments or live savings evidence", "cases": cases},
        "registry": registry.to_dict(), "knowledge_base": knowledge_snapshot(kb), "profiles": None,
        "forecasts": {case["case_id"]: [] for case in cases}, "carbon": None, "tariffs": None,
        "energy_mode": "none", "energy_country": None, "arms": arms, "baseline_arm_id": arms[0]["arm_id"],
        "response_seconds": 300, "completion_seconds": 600, "timeout_seconds": 120., "quality_floor": .8,
        "repeats": 1, "seed": 42, "poll_seconds": .1, "planning_controls": DEFAULT_CONTROLS,
        "source_manifest": source_manifest(), "output_quality": default_review_config()})
    def scripted(candidate, registry, prompt, **settings):
        payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
        started = clock.now()
        seconds = 1 if candidate.model_id == "small" else 2
        clock.sleep(seconds)
        if candidate.step_id == "classify":
            text = "billing" if "invoice" in payload["ticket"] else "account_access"
        elif candidate.step_id == "retrieve":
            text = "\n".join(article["body"] + " [" + article["source_id"] + "]"
                for article in payload["knowledge_base_evidence"]) or NO_EVIDENCE
        elif candidate.step_id == "draft":
            text = "Scripted demonstration reply. " + payload["retrieved_evidence"]
        else:
            text = "Scripted demonstration summary; no actual account action was performed."
        return ExecutionAttempt(uuid4().hex, settings["workflow_id"], candidate, "completed",
            actual_start=started, actual_finish=clock.now(), executed_model_id=registry.get(candidate.deployment_id).provider_model(candidate.model_id),
            executed_deployment_id=candidate.deployment_id, output=text, input_tokens=None, output_tokens=None,
            provider_seconds=seconds, output_limit_tokens=settings["output_limit_tokens"],
            enable_thinking=settings["enable_thinking"], completion_reason="stop")
    directory = prepare_evaluation(protocol, work_directory or Path(".runtime") / ("offline-demo-" + uuid4().hex), clock=clock)
    report = run_evaluation(directory, clock=clock, sleeper=clock.sleep, dispatcher=scripted,
                            heartbeat=False, progress=False)
    bundle = export_comparison(directory, output)
    return {"experiment_mode": "simulation", "inference_performed": False, "human_reviews_generated": 0,
        "evaluation": str(directory), "bundle": str(bundle), "finished_trials": report["finished_trials"],
        "release_eligible": report["release_evidence"]["eligible"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(create_demo(args.output, work_directory=args.work_directory)))
        return 0
    except (ValueError, OSError, KeyError):
        print(json.dumps({"error_code": "offline-demo-could-not-be-created"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
