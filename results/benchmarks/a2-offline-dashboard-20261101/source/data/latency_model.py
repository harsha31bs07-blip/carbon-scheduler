"""data/latency_model.py

Regional network round-trip latency modeling across global cloud regions.
Encodes empirical fiber-optic RTT, submarine cable transit, and intra-region baseline
telemetry for Candidate latency estimation.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from data.model_table import get_model_latency_profile
from data.region_carbon import normalize_location

# Default primary coordinator/client origin region
DEFAULT_ORIGIN: str = "us-east"

# On-device deployment location (see data/region_carbon.py)
LOCAL_LOCATION: str = "local-gpu"

# Pairwise round-trip network transit times (ms) derived from
# global cloud backbone telemetry (AWS/GCP network performance benchmarks & WonderNetwork RTT data)
PAIRWISE_LATENCY_MS: Dict[Tuple[str, str], float] = {
    # Intra-region baseline (datacenter LAN, TLS termination, local gateway overhead)
    ("us-east", "us-east"): 18.0,
    ("eu-west", "eu-west"): 18.0,
    ("in-south", "in-south"): 20.0,
    ("us-west", "us-west"): 18.0,
    ("eu-north", "eu-north"): 20.0,
    ("ap-southeast", "ap-southeast"): 18.0,
    
    # LOCAL DEPLOYMENT - no network latency, just model inference time
    ("local-gpu", "local-gpu"): 0.0,

    # us-east connections (Transatlantic, Transcontinental, Transpacific)
    ("us-east", "eu-west"): 85.0,        # Transatlantic submarine cable (NYC/VA to Ireland/UK)
    ("us-east", "in-south"): 210.0,      # Long-haul: mostly via Atlantic/Europe/Red Sea submarine cables to S. India
    ("us-east", "us-west"): 70.0,        # Transcontinental US fiber (Ashburn to Oregon)
    ("us-east", "eu-north"): 105.0,      # US East to Stockholm (Nordic transit)
    ("us-east", "ap-southeast"): 220.0,  # US East to Singapore

    # eu-west connections
    ("eu-west", "us-east"): 85.0,
    ("eu-west", "eu-north"): 35.0,       # Intra-European backbone (Dublin to Stockholm)
    ("eu-west", "in-south"): 125.0,      # Mediterranean / Red Sea / Arabian Sea submarine transit
    ("eu-west", "us-west"): 135.0,
    ("eu-west", "ap-southeast"): 160.0,

    # in-south connections
    ("in-south", "us-east"): 210.0,
    ("in-south", "eu-west"): 125.0,
    ("in-south", "us-west"): 235.0,
    ("in-south", "eu-north"): 140.0,
    ("in-south", "ap-southeast"): 55.0,  # Bay of Bengal / Malacca Strait (Chennai/Hyd to Singapore)

    # us-west connections
    ("us-west", "us-east"): 70.0,
    ("us-west", "eu-west"): 135.0,
    ("us-west", "in-south"): 235.0,
    ("us-west", "eu-north"): 145.0,
    ("us-west", "ap-southeast"): 165.0,  # Transpacific West Coast to Singapore

    # eu-north connections
    ("eu-north", "us-east"): 105.0,
    ("eu-north", "eu-west"): 35.0,
    ("eu-north", "in-south"): 140.0,
    ("eu-north", "us-west"): 145.0,
    ("eu-north", "ap-southeast"): 175.0,

    # ap-southeast connections
    ("ap-southeast", "us-east"): 220.0,
    ("ap-southeast", "eu-west"): 160.0,
    ("ap-southeast", "in-south"): 55.0,
    ("ap-southeast", "us-west"): 165.0,
    ("ap-southeast", "eu-north"): 175.0,
}

# Conservative fallback for unspecified cross-continent paths
DEFAULT_CROSS_CONTINENT_MS: float = 195.0


def get_inter_region_latency_ms(origin: str, destination: str) -> float:
    """Compute estimated round-trip network latency (ms) between any two cloud regions.

    Handles case normalization, aliases, and symmetry.
    """
    orig = normalize_location(origin)
    dest = normalize_location(destination)

    # Local deployment runs on the caller's own machine: no network hop.
    if dest == LOCAL_LOCATION:
        return 0.0

    if orig == dest:
        return PAIRWISE_LATENCY_MS.get((orig, dest), 18.0)

    # Check direct mapping
    if (orig, dest) in PAIRWISE_LATENCY_MS:
        return PAIRWISE_LATENCY_MS[(orig, dest)]

    # Check symmetric reverse
    if (dest, orig) in PAIRWISE_LATENCY_MS:
        return PAIRWISE_LATENCY_MS[(dest, orig)]

    return DEFAULT_CROSS_CONTINENT_MS


def get_network_latency_ms(location: str, origin: Optional[str] = None) -> float:
    """Get the estimated round-trip network latency in milliseconds for a destination location

    relative to the orchestrator origin (defaults to 'us-east').

    Args:
        location: Target execution region (e.g., 'us-east', 'eu-west', 'in-south').
        origin: Optional source/coordinator region. Defaults to 'us-east'.

    Returns:
        Estimated network round-trip time in milliseconds.
    """
    source_origin = origin or DEFAULT_ORIGIN
    return get_inter_region_latency_ms(source_origin, location)


def get_inference_latency_ms(model: str, output_tokens: int) -> float:
    """Model-side latency: time to first token + decode time for `output_tokens`.

    Uses the ASSUMED per-model ttft / tokens-per-second in data/model_table.py.
    Replace those with values measured by `run_eval.py` (live mode) when available.
    """
    ttft_ms, tps = get_model_latency_profile(model)
    return ttft_ms + (max(0, output_tokens) / tps) * 1000.0


def get_total_latency_ms(
    model: str,
    location: str,
    output_tokens: int,
    origin: Optional[str] = None,
) -> float:
    """Network RTT + model inference latency.

    Does NOT include any deliberate delay from `time_offset_hr`; the scheduler
    adds `time_offset_hr * 3_600_000` itself if it wants "wait then run" latency.
    """
    return get_network_latency_ms(location, origin) + get_inference_latency_ms(model, output_tokens)
