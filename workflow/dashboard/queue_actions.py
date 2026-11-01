"""Dashboard submission boundary: save planning inputs without provider probes."""

from data.quality_reviews import load_json
from shared.deployments import DeploymentHealth
from shared.scheduling import SystemClock, WorkloadSizeForecast
from workflow.planning_records import decode_carbon


def _submission(token, registry, kb, ticket, settings, **evidence):
    if token is None:
        return None
    return {"token": token, "payload": {"ticket": ticket.strip(),
        "registry": registry.to_dict(), "kb_fingerprint": kb.fingerprint(),
        "settings": settings, **evidence}}


def submit_fixed_ticket(store, registry, kb, ticket, *, token, clock=None, **settings):
    from workflow.worker import submit_fixed
    submission = _submission(token, registry, kb, ticket, settings, strategy="fixed")
    existing = store.submission_result(submission)
    return existing or submit_fixed(store, registry, kb, ticket, clock=clock,
                                    submission=submission, **settings)


def validate_fields(ticket, response, completion, delay):
    issues = {}
    if not ticket.strip():
        issues["Support ticket"] = "Describe the customer issue before queueing."
    if completion < response:
        issues["Final deadline"] = "The final deadline must be at least the response deadline."
    if delay >= completion:
        issues["Background summary"] = "Choose a delay shorter than the final deadline."
    return issues


def submit_unverified(store, registry, kb, ticket, *, profiles_path, forecasts_path=None,
                      carbon_path=None, clock=None, token=None, **settings):
    from workflow.planned_worker import submit_planned
    active_clock = clock or SystemClock()
    forecasts = load_json(forecasts_path) if forecasts_path else []
    if not isinstance(forecasts, list):
        raise ValueError("Forecasts must contain a JSON array")
    profiles = load_json(profiles_path)
    carbon = load_json(carbon_path) if carbon_path else None
    submission = _submission(token, registry, kb, ticket, settings, strategy="optimized",
                             profiles=profiles, forecasts=forecasts, carbon=carbon)
    existing = store.submission_result(submission)
    if existing:
        return existing
    # None denotes unobserved health, never an invented outage or availability.
    def unobserved(deployment, **kwargs):
        return DeploymentHealth(deployment.deployment_id, active_clock.now(), None, (),
                                "awaiting-worker-verification")
    return submit_planned(store, registry, kb, ticket, profiles,
        forecasts=[WorkloadSizeForecast.from_dict(item) for item in forecasts],
        carbon_provider=decode_carbon(carbon), submission=submission,
        health_probe=unobserved, clock=active_clock, **settings)
