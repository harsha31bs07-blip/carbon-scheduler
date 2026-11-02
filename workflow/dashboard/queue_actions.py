"""Dashboard submission boundary: save planning inputs without provider probes."""

from data.quality_reviews import load_json
from shared.deployments import DeploymentHealth
from shared.scheduling import SystemClock, WorkloadSizeForecast
from workflow.planning_records import decode_carbon


class PlanningFieldError(ValueError):
    """A safe, field-specific correction for operator-supplied settings."""


def parse_planning_fields(offset_text, weight_text, policy):
    from workflow.planning import _weights
    from workflow.planning_records import _offsets
    from workflow.queue_store import _load
    try:
        offsets = tuple(float(value.strip()) for value in offset_text.split(","))
        _offsets(list(offsets))
    except ValueError:
        raise PlanningFieldError("Offered delays: supply 1–128 unique finite delays between 0 and 604800 seconds.") from None
    try:
        weights = _load(weight_text) if weight_text is not None else None
        _weights(policy, weights)
    except ValueError:
        raise PlanningFieldError("Custom weights: specify latency, accuracy, cost, energy and carbon with finite nonnegative values and a positive total.") from None
    return offsets, weights


def _read_input(path, field):
    try:
        return load_json(path)
    except (ValueError, OSError):
        raise PlanningFieldError(field + ": select a complete, valid JSON evidence file.") from None


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
    forecasts = _read_input(forecasts_path, "Workload forecasts") if forecasts_path else []
    if not isinstance(forecasts, list):
        raise PlanningFieldError("Workload forecasts: supply a JSON array of typed forecasts.")
    profiles = _read_input(profiles_path, "Reviewed profiles")
    carbon = _read_input(carbon_path, "Carbon snapshot") if carbon_path else None
    submission = _submission(token, registry, kb, ticket, settings, strategy="optimized",
                             profiles=profiles, forecasts=forecasts, carbon=carbon)
    existing = store.submission_result(submission)
    if existing:
        return existing
    from data.profiles import _validate_profiles
    try:
        _validate_profiles(profiles)
    except (ValueError, TypeError, KeyError):
        raise PlanningFieldError("Reviewed profiles: select a complete valid CalibrationProfiles snapshot.") from None
    try:
        typed_forecasts = [WorkloadSizeForecast.from_dict(item) for item in forecasts]
    except (ValueError, TypeError, KeyError):
        raise PlanningFieldError("Workload forecasts: one or more entries have invalid fields or schema.") from None
    try:
        provider = decode_carbon(carbon)
    except (ValueError, TypeError, KeyError):
        raise PlanningFieldError("Carbon snapshot: select a supported versioned static estimate or timestamped trace.") from None
    # None denotes unobserved health, never an invented outage or availability.
    def unobserved(deployment, **kwargs):
        return DeploymentHealth(deployment.deployment_id, active_clock.now(), None, (),
                                "awaiting-worker-verification")
    return submit_planned(store, registry, kb, ticket, profiles,
        forecasts=typed_forecasts, carbon_provider=provider, submission=submission,
        health_probe=unobserved, clock=active_clock, **settings)
