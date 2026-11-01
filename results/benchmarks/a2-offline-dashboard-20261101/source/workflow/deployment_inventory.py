"""Read-only deployment inventory; ``python -m workflow.deployment_inventory``.

Ollama health uses its tags endpoint without inference. Cloud health is explicitly
unknown because configuration or key presence is not availability evidence.
Reachability records whether an HTTP response was received; model capability
requires a valid tags response. Credential preflight failures remain unprobed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys

import requests

from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry, load_deployments
from workflow.deployment_clients import _credential, _validate_timeout, DeploymentCallError


def probe_deployment(deployment: Deployment, *, timeout_seconds: float = 3.0) -> DeploymentHealth:
    timeout = _validate_timeout(timeout_seconds)
    checked_at = datetime.now(timezone.utc)
    if deployment.provider != "ollama":
        return DeploymentHealth(
            deployment_id=deployment.deployment_id, checked_at=checked_at,
            reachable=None, available_models=(), source="not-probed",
            error_code="verification-not-performed",
        )

    def health(*, reachable=False, models=(), error_code=None, source="ollama-tags"):
        return DeploymentHealth(
            deployment_id=deployment.deployment_id, checked_at=checked_at,
            reachable=reachable, available_models=models, source=source,
            error_code=error_code,
        )

    try:
        key = _credential(deployment)
    except DeploymentCallError as exc:
        return health(reachable=None, error_code=exc.error_code, source="credential-preflight")
    response = None
    try:
        response = requests.get(
            deployment.endpoint.rstrip("/") + "/api/tags",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=(min(timeout, 5.0), timeout), allow_redirects=False,
        )
        status = response.status_code
        if status in (401, 403):
            return health(reachable=True, error_code="authentication-rejected")
        if not 200 <= status < 300:
            return health(reachable=True, error_code="http-request-rejected")
        try:
            body = response.json()
        except ValueError:
            return health(reachable=True, error_code="invalid-response")
        if not isinstance(body, dict) or not isinstance(body.get("models"), list):
            return health(reachable=True, error_code="invalid-response")
        observed = set()
        for model in body["models"]:
            if not isinstance(model, dict):
                return health(reachable=True, error_code="invalid-response")
            # Modern Ollama reports both fields; either is an observation.
            for field in ("name", "model"):
                value = model.get(field)
                if value is not None and not isinstance(value, str):
                    return health(reachable=True, error_code="invalid-response")
                if value:
                    observed.add(value)
        canonical = tuple(sorted(name for name, actual in deployment.models.items()
                                 if actual in observed))
        return health(reachable=True, models=canonical)
    except requests.exceptions.Timeout:
        return health(error_code="request-timeout")
    except requests.exceptions.ConnectionError:
        return health(error_code="connection-failed")
    except requests.exceptions.RequestException:
        return health(reachable=response is not None, error_code="request-failed")
    except (ValueError, TypeError):
        return health(reachable=response is not None, error_code="invalid-response")
    finally:
        if response is not None:
            response.close()


def probe_registry(registry: DeploymentRegistry, *, timeout_seconds: float = 3.0) -> dict:
    _validate_timeout(timeout_seconds)
    return {"schema_version": 1, "configuration_fingerprint": registry.config_fingerprint(),
            "deployments": [
                {"configuration": deployment.to_dict(),
                 "health": probe_deployment(deployment, timeout_seconds=timeout_seconds).to_dict()}
                for deployment in registry
            ]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to the explicit deployment registry JSON")
    parser.add_argument("--timeout-seconds", type=float, default=3.0,
                        help="Per-request connection/read inactivity timeout (default: 3)")
    args = parser.parse_args(argv)
    try:
        result = probe_registry(load_deployments(args.config), timeout_seconds=args.timeout_seconds)
    except (ValueError, OSError):
        print(json.dumps({"error_code": "invalid-inventory-configuration"}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
