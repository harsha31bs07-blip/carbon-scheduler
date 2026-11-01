"""data/time_curve.py

Diurnal carbon-intensity modeling simulating real-world hourly grid fluctuations.
Models the midday solar dip ("duck curve"), evening thermal peaker ramps,
and night-time baseload adjustments across geographic regions.

NOTE: these curves are SYNTHETIC (hand-tuned Gaussians on top of annual-average
intensity), not measured hourly grid data. Treat them as a plausible shape, not a forecast.
Pass `base_utc_time` for reproducible results; the default is the current time.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from data.region_carbon import get_base_carbon_intensity, get_region_profile, normalize_location

# Regional diurnal shaping parameters:
# solar_depth: fractional drop below baseline at solar noon (e.g., 0.25 = 25% cleaner during solar peak)
# peak_height: fractional increase above baseline during evening peak demand (e.g., 0.25 = 25% dirtier)
# solar_noon_hour: local time hour when solar generation maximizes (default: 13.0)
# evening_peak_hour: local time hour when evening demand maximizes (default: 19.5)
# night_wind_bonus: fractional reduction at night due to off-peak wind or baseload (e.g., Ireland / Nordic)
DIURNAL_REGIONAL_MODIFIERS: Dict[str, Dict[str, float]] = {
    "us-east": {
        "solar_depth": 0.20,      # PJM solar penetration
        "peak_height": 0.22,      # Evening gas peaker ramp
        "solar_noon_hour": 13.0,
        "evening_peak_hour": 19.5,
        "night_trough_bonus": 0.05,
    },
    "eu-west": {
        "solar_depth": 0.15,      # Moderate solar in Ireland/UK
        "peak_height": 0.20,      # Evening residential load
        "solar_noon_hour": 13.0,
        "evening_peak_hour": 19.0,
        "night_trough_bonus": 0.12,  # Night-time North Sea & Atlantic wind generation
    },
    "in-south": {
        "solar_depth": 0.28,      # High solar irradiance in Southern India
        "peak_height": 0.26,      # Heavy coal & diesel peaking ramp at dusk
        "solar_noon_hour": 12.5,
        "evening_peak_hour": 20.0,
        "night_trough_bonus": 0.02,
    },
    "us-west": {
        "solar_depth": 0.25,
        "peak_height": 0.20,
        "solar_noon_hour": 13.0,
        "evening_peak_hour": 19.0,
        "night_trough_bonus": 0.08,
    },
    "eu-north": {
        "solar_depth": 0.10,
        "peak_height": 0.15,
        "solar_noon_hour": 12.5,
        "evening_peak_hour": 18.5,
        "night_trough_bonus": 0.10,
    },
    "ap-southeast": {
        "solar_depth": 0.18,
        "peak_height": 0.20,
        "solar_noon_hour": 13.0,
        "evening_peak_hour": 19.5,
        "night_trough_bonus": 0.03,
    },
}


def get_local_solar_hour(
    location: str,
    hour_offset: int = 0,
    base_utc_time: Optional[datetime] = None,
) -> float:
    """Calculate the effective local solar clock hour [0.0, 24.0) for a region

    given a future hour offset from a reference UTC timestamp.
    """
    profile = get_region_profile(location)
    ref_time = base_utc_time or datetime.now(timezone.utc)
    utc_fractional_hour = ref_time.hour + (ref_time.minute / 60.0) + (ref_time.second / 3600.0)
    local_hour = (utc_fractional_hour + profile.utc_offset_hours + hour_offset) % 24.0
    if local_hour < 0.0:
        local_hour += 24.0
    return local_hour


def get_diurnal_multiplier(location: str, local_hour: float) -> float:
    """Compute the normalized carbon intensity multiplier [~0.70 to ~1.30]

    for a given location at a specific local solar hour of the day.

    Models:
    1. Midday solar dip: Gaussian trough centered around solar noon (12:30 - 13:30).
    2. Evening demand peak: Gaussian peak centered around 19:00 - 20:00 (peakers on).
    3. Night baseload / wind trough: off-peak reduction during 01:00 - 05:00.
    """
    canonical = normalize_location(location)
    params = DIURNAL_REGIONAL_MODIFIERS.get(
        canonical,
        {
            "solar_depth": 0.20,
            "peak_height": 0.20,
            "solar_noon_hour": 13.0,
            "evening_peak_hour": 19.5,
            "night_trough_bonus": 0.05,
        },
    )

    solar_noon = params["solar_noon_hour"]
    solar_depth = params["solar_depth"]
    evening_peak = params["evening_peak_hour"]
    peak_height = params["peak_height"]
    night_bonus = params["night_trough_bonus"]

    # Midday Solar Dip: Gaussian centered at solar_noon with sigma ~ 2.2 hours
    solar_dist = abs(local_hour - solar_noon)
    solar_component = -solar_depth * math.exp(-((solar_dist / 2.2) ** 2))

    # Evening Peak: Gaussian centered at evening_peak with sigma ~ 2.0 hours
    evening_dist = abs(local_hour - evening_peak)
    evening_component = peak_height * math.exp(-((evening_dist / 2.0) ** 2))

    # Night / early morning trough (centered around 03:00)
    night_dist = min(abs(local_hour - 3.0), abs(local_hour - 27.0), abs(local_hour + 21.0))
    night_component = -night_bonus * math.exp(-((night_dist / 2.5) ** 2))

    multiplier = 1.0 + solar_component + evening_component + night_component

    # Clamp multiplier to physically plausible bounds
    return max(0.50, min(1.60, multiplier))


def get_carbon_intensity_at_hour(
    location: str,
    hour_offset: int,
    base_utc_time: Optional[datetime] = None,
) -> float:
    """Calculate the estimated grid carbon intensity (gCO2eq / kWh) for a location

    at `hour_offset` hours into the future from the reference time.

    Args:
        location: Regional identifier (e.g., 'us-east', 'eu-west', 'in-south').
        hour_offset: Integer hour offset (0 = now, 2 = 2 hours later).
        base_utc_time: Optional reference datetime; defaults to datetime.now(timezone.utc).

    Returns:
        Projected carbon intensity in gCO2eq/kWh.
    """
    base_intensity = get_base_carbon_intensity(location)
    local_hour = get_local_solar_hour(location, hour_offset, base_utc_time=base_utc_time)
    multiplier = get_diurnal_multiplier(location, local_hour)
    return round(base_intensity * multiplier, 2)


def get_24h_carbon_forecast(
    location: str,
    base_utc_time: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Generate a 24-hour hourly carbon intensity forecast starting from offset 0 to 23."""
    forecast = []
    for h in range(24):
        local_hr = get_local_solar_hour(location, h, base_utc_time=base_utc_time)
        intensity = get_carbon_intensity_at_hour(location, h, base_utc_time=base_utc_time)
        forecast.append(
            {
                "hour_offset": h,
                "local_solar_hour": round(local_hr, 2),
                "carbon_intensity_g_per_kwh": intensity,
            }
        )
    return forecast
