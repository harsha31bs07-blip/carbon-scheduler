POLICIES = {
    "speed": {"w_latency": 0.6, "w_cost": 0.1, "w_energy": 0.1, "w_carbon": 0.1, "w_accuracy": 0.1},
    "eco": {"w_latency": 0.1, "w_cost": 0.1, "w_energy": 0.3, "w_carbon": 0.4, "w_accuracy": 0.1},
    "balanced": {"w_latency": 0.2, "w_cost": 0.2, "w_energy": 0.2, "w_carbon": 0.2, "w_accuracy": 0.2},
}

REQUIRED_KEYS = ["w_latency", "w_cost", "w_energy", "w_carbon", "w_accuracy"]


def get_policy_weights(policy: str, custom_weights: dict = None) -> dict:
    if policy == "custom":
        if not custom_weights:
            raise ValueError("custom_weights required for 'custom' policy")
        weights = {k: custom_weights.get(k, 0.0) for k in REQUIRED_KEYS}
        if any(v < 0 for v in weights.values()):
            raise ValueError("Weights cannot be negative")
        total = sum(weights.values())
        if total == 0:
            raise ValueError("Weights must not all be zero")
        return {k: v / total for k, v in weights.items()}
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy: {policy}. Valid: {list(POLICIES.keys())} or 'custom'")
    return POLICIES[policy]