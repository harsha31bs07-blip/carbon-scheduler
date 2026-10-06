from shared.schema import Candidate
from scheduler.candidate_gen import get_step
import logging

logger = logging.getLogger(__name__)

MS_PER_HOUR = 3_600_000


def effective_latency_ms(c: Candidate) -> float:
    """Time until the step's result is ready: deliberate deferral + compute latency.

    est_latency_ms excludes the deferral by design (see data/README.md), so it is added
    here for scoring. Without it, deferring looks free and even the speed policy defers.
    """
    return c.time_offset_hr * MS_PER_HOUR + c.est_latency_ms


def _normalize(values: list[float]) -> list[float]:
    min_v = min(values)
    max_v = max(values)
    if max_v == min_v:
        return [0.0 for _ in values]
    return [(v - min_v) / (max_v - min_v) for v in values]


def score_candidates(candidates: list[Candidate], weights: dict, step_id: str) -> list[tuple[Candidate, float]]:
    step = get_step(step_id)
    max_latency = step.get("max_latency_ms")
    min_accuracy = step.get("min_accuracy", 0.7)

    filtered = [
        c for c in candidates
        if c.est_accuracy >= min_accuracy and (max_latency is None or c.est_latency_ms <= max_latency)
    ]

    if not filtered:
        logger.warning(f"All candidates filtered out for {step_id}, using fallback")
        max_latency = step.get("max_latency_ms")
        fallback = min(
            candidates,
            key=lambda c: (
                -(c.est_accuracy),
                max(0, c.est_latency_ms - max_latency) if max_latency else c.est_latency_ms
            )
        )
        filtered = [fallback]

    # SLA filtering above uses compute latency only; scoring charges for the wait as well.
    latencies = [effective_latency_ms(c) for c in filtered]
    costs = [c.est_cost_usd for c in filtered]
    energies = [c.est_energy_kwh for c in filtered]
    carbons = [c.est_carbon_g for c in filtered]
    accuracies = [c.est_accuracy for c in filtered]

    norm_latency = _normalize(latencies)
    norm_cost = _normalize(costs)
    norm_energy = _normalize(energies)
    norm_carbon = _normalize(carbons)
    norm_accuracy = _normalize(accuracies)

    scored = []
    for i, c in enumerate(filtered):
        score = (
            weights["w_latency"] * norm_latency[i]
            + weights["w_cost"] * norm_cost[i]
            + weights["w_energy"] * norm_energy[i]
            + weights["w_carbon"] * norm_carbon[i]
            - weights["w_accuracy"] * norm_accuracy[i]
        )
        scored.append((c, score))

    scored.sort(key=lambda x: (x[1], x[0].est_carbon_g, x[0].est_latency_ms, x[0].model, x[0].location, x[0].time_offset_hr))
    return scored