"""Local diagnostics. Provider inventory requires the explicit --probe option."""

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path

from data.profiles import _validate_profiles
from data.quality_reviews import load_json
from shared.deployments import DeploymentHealth, load_deployments
from shared.scheduling import WorkloadSizeForecast
from workflow.dashboard.services import local_paths, startup_commands
from workflow.workload import KnowledgeBase


def observe_destinations(registry_path, *, timeout_seconds=3):
    from workflow.deployment_inventory import probe_deployment
    registry = load_deployments(registry_path)
    if len(tuple(registry)) > 16:
        raise ValueError("Readiness probes support at most sixteen destinations")
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 3:
        raise ValueError("Readiness probe timeout must be at most three seconds")
    return {"registry_fingerprint": registry.config_fingerprint(),
            "observations": [probe_deployment(item, timeout_seconds=timeout_seconds).to_dict()
                             for item in registry]}


def inspect_readiness(root, *, registry_path=None, database_path=None, profiles_path=None,
                      forecasts_path=None, carbon_path=None, observations=None, now=None):
    """Inspect supplied files only. This is not request-specific dispatch approval."""
    paths = local_paths(root, registry_path=registry_path, database_path=database_path,
                        profiles_path=profiles_path)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Readiness time must be timezone-aware")
    checks, destinations = [], []
    def check(name, status, detail):
        checks.append({"component": name, "status": status, "detail": detail})
    try:
        registry = load_deployments(paths["registry"])
        check("Deployment configuration", "valid", "Exact endpoint/model mappings loaded; availability is checked separately.")
        health = {}
        if observations is not None:
            if observations["registry_fingerprint"] != registry.config_fingerprint():
                raise ValueError("Observations belong to different configuration")
            for raw in observations["observations"]:
                item = DeploymentHealth.from_dict(raw)
                registry.get(item.deployment_id)
                if item.deployment_id in health:
                    raise ValueError("Duplicate health observation")
                health[item.deployment_id] = item
        for deployment in registry:
            item = health.get(deployment.deployment_id)
            status = "unknown"
            if item is not None:
                age = (now - item.checked_at).total_seconds()
                status = ("stale" if age > 60 or age < 0 else
                          "unknown" if item.reachable is None else
                          "reachable" if item.reachable else "unavailable")
            destinations.append({"destination": deployment.deployment_id,
                "models": dict(deployment.models), "region": deployment.region,
                "grid": deployment.grid, "availability": status,
                "observed_at": item.checked_at.isoformat() if item else None,
                "observed_models": list(item.available_models) if item else [],
                "source": item.source if item else "not-observed"})
    except (ValueError, OSError, TypeError, KeyError):
        check("Deployment observations", "invalid", "Configuration or retained observations could not be validated; reload or check the registry.")
    try:
        kb = KnowledgeBase.load(paths["root"] / "data/workloads/support_kb.v1.json")
        check("Knowledge base", "valid", "Versioned KB loaded: " + kb.version)
    except (ValueError, OSError, TypeError, KeyError):
        check("Knowledge base", "invalid", "The versioned knowledge base is missing or invalid.")
    try:
        profiles = load_json(paths["profiles"])
        _validate_profiles(profiles)
        reviewed = sum(item["metrics"]["accuracy"]["count"] > 0
            and item["metrics"]["accuracy"]["comparable"]
            and item["metrics"]["accuracy"].get("coverage", {}).get("eligible", "quality_policy" not in profiles)
            for item in profiles["profiles"])
        check("Reviewed profiles", "available" if reviewed else "blocked",
              f"{reviewed}/{len(profiles['profiles'])} profiles have usable reviewed-quality coverage; model, context and quality-floor compatibility still apply.")
    except (ValueError, OSError, TypeError, KeyError):
        check("Reviewed profiles", "missing-or-invalid", "Select frozen reviewed profiles. Unknown semantic quality blocks optimized dispatch.")
    for name, supplied in (("Workload forecasts", forecasts_path), ("Carbon snapshot", carbon_path)):
        if supplied is None:
            check(name, "not-selected", "No input selected; downstream size/carbon support remains request-specific.")
            continue
        try:
            path = Path(supplied)
            value = load_json(path if path.is_absolute() else paths["root"] / path)
            if name == "Workload forecasts":
                if not isinstance(value, list):
                    raise ValueError("Expected forecast list")
                for item in value:
                    WorkloadSizeForecast.from_dict(item)
                detail = f"{len(value)} typed forecasts loaded; bindings, freshness and context coverage are checked by the planner."
            else:
                from workflow.planning_records import decode_carbon
                decode_carbon(value)
                if value is None:
                    raise ValueError("Missing carbon snapshot")
                detail = "Versioned snapshot loaded; grid, issue time, freshness and execution interval still require planner checks. Annual static proxies do not establish hourly benefits."
            check(name, "valid-format", detail)
        except (ValueError, OSError, TypeError, KeyError):
            check(name, "invalid", "Selected evidence could not be validated.")
    check("Worker", "unknown", "No worker-presence telemetry is retained yet. Job leases do not prove an idle worker is running.")
    check("Queue file", "present" if paths["database"].is_file() else "not-created",
          "Submission creates the queue when needed. This check does not open or migrate the database.")
    for package in ("requests", "python-dotenv", "streamlit", "pandas", "plotly"):
        try:
            check("Dependency: " + package, "installed", metadata.version(package))
        except metadata.PackageNotFoundError:
            check("Dependency: " + package, "missing", "Install requirements.txt in the active Python environment.")
    return {"schema_version": 1, "checked_at": now.isoformat(), "checks": checks,
            "destinations": destinations, "dispatch_approved": False,
            "commands": startup_commands(paths["root"], paths["database"]),
            "scope": "Local configuration inspection; no workflow-specific eligibility or full A2 validation."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config")
    parser.add_argument("--db")
    parser.add_argument("--profiles")
    parser.add_argument("--forecasts")
    parser.add_argument("--carbon")
    parser.add_argument("--probe", action="store_true", help="Explicit bounded read-only provider inventory; no generation")
    args = parser.parse_args(argv)
    try:
        paths = local_paths(args.root, registry_path=args.config)
        observations = observe_destinations(paths["registry"]) if args.probe else None
        report = inspect_readiness(args.root, registry_path=args.config, database_path=args.db,
            profiles_path=args.profiles, forecasts_path=args.forecasts, carbon_path=args.carbon,
            observations=observations)
    except (ValueError, OSError):
        print(json.dumps({"error_code": "invalid-readiness-configuration"}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if any(item["status"] in {"invalid", "missing", "missing-or-invalid"}
                    for item in report["checks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
