import math
from collections.abc import Mapping, Sized

POLICIES = {
    "speed": {"w_latency": 0.6, "w_cost": 0.1, "w_energy": 0.1, "w_carbon": 0.1, "w_accuracy": 0.1},
    "eco": {"w_latency": 0.1, "w_cost": 0.1, "w_energy": 0.3, "w_carbon": 0.4, "w_accuracy": 0.1},
    "balanced": {"w_latency": 0.2, "w_cost": 0.2, "w_energy": 0.2, "w_carbon": 0.2, "w_accuracy": 0.2},
}

REQUIRED_KEYS = ["w_latency", "w_cost", "w_energy", "w_carbon", "w_accuracy"]


def get_policy_weights(policy: str, custom_weights: dict = None) -> dict:
    if policy == "custom":
        if custom_weights is None or (isinstance(custom_weights, Sized) and len(custom_weights) == 0):
            raise ValueError("custom_weights required for 'custom' policy")
        if not isinstance(custom_weights, Mapping):
            raise ValueError(
                f"custom_weights must be a mapping, got {type(custom_weights).__name__}"
            )
        unknown = sorted((k for k in custom_weights if k not in REQUIRED_KEYS), key=str)
        if unknown:
            unknown_str = ", ".join(str(k) for k in unknown)
            raise ValueError(f"Unknown weight keys: {unknown_str}")
        weights = {}
        for k in REQUIRED_KEYS:
            v = custom_weights.get(k, 0.0)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"Weight '{k}' must be a number, got {type(v).__name__}")
            if not math.isfinite(v):
                raise ValueError(f"Weight '{k}' must be finite, got {v}")
            weights[k] = v
        if any(v < 0 for v in weights.values()):
            raise ValueError("Weights cannot be negative")
        total = sum(weights.values())
        if total == 0:
            raise ValueError("Weights must not all be zero")
        return {k: weights[k] / total for k in REQUIRED_KEYS}
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy: {policy}. Valid: {list(POLICIES.keys())} or 'custom'")
    return dict(POLICIES[policy])
