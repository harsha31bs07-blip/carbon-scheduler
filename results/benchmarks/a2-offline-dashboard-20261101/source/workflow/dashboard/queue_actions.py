"""Dashboard submission boundary: save planning inputs without provider probes."""

from data.quality_reviews import load_json
from shared.deployments import DeploymentHealth
from shared.scheduling import SystemClock, WorkloadSizeForecast
from workflow.planning_records import decode_carbon


def submit_unverified(store, registry, kb, ticket, *, profiles_path, forecasts_path=None,
                      carbon_path=None, clock=None, **settings):
    from workflow.planned_worker import submit_planned
    active_clock = clock or SystemClock()
    forecasts = load_json(forecasts_path) if forecasts_path else []
    if not isinstance(forecasts, list):
        raise ValueError("Forecasts must contain a JSON array")
    # None denotes unobserved health, never an invented outage or availability.
    def unobserved(deployment, **kwargs):
        return DeploymentHealth(deployment.deployment_id, active_clock.now(), None, (),
                                "awaiting-worker-verification")
    return submit_planned(store, registry, kb, ticket, load_json(profiles_path),
        forecasts=[WorkloadSizeForecast.from_dict(item) for item in forecasts],
        carbon_provider=decode_carbon(load_json(carbon_path)) if carbon_path else None,
        health_probe=unobserved, clock=active_clock, **settings)
