from shared.schema import Candidate
from scheduler.candidate_gen import generate_candidates
from scheduler.scorer import score_candidates
from scheduler.policies import get_policy_weights


def schedule_step(step_id: str, policy: str, custom_weights: dict = None, *,
                  base_utc_time=None, execution_only: bool = False) -> Candidate:
    """
    policy: one of "eco", "balanced", "speed", or "custom"
    Returns the single winning Candidate for this step.
    custom_weights keys: w_latency, w_cost, w_energy, w_carbon, w_accuracy
    """
    candidates = generate_candidates(step_id, base_utc_time, execution_only=execution_only)
    weights = get_policy_weights(policy, custom_weights)
    scored = score_candidates(candidates, weights, step_id)
    return scored[0][0]
