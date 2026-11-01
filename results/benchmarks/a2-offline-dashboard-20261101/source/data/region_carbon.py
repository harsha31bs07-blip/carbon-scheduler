"""data/region_carbon.py

Regional electrical grid carbon-intensity specifications (gCO2eq / kWh)
derived from published national and regional grid reporting agencies
(EPA eGRID, Electricity Maps, Central Electricity Authority of India, EEA).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class RegionCarbonProfile:
    """Carbon intensity and electrical grid metadata for a geographic region."""

    canonical_id: str
    display_name: str
    grid_name: str
    carbon_intensity_g_per_kwh: float
    utc_offset_hours: float
    primary_energy_sources: List[str]
    data_source: str


# Published carbon intensities (annual lifecycle gCO2eq/kWh)
# Sources:
# - us-east (PJM Interconnection / Virginia): EPA eGRID & Electricity Maps ~385 gCO2/kWh
# - eu-west (Ireland / Western Europe blend): SEAI & Electricity Maps ~260 gCO2/kWh
# - in-south (Southern Regional Grid India): CEA CO2 Baseline Database v19 ~710 gCO2/kWh
REGION_PROFILES: Dict[str, RegionCarbonProfile] = {
    "us-east": RegionCarbonProfile(
        canonical_id="us-east",
        display_name="US East (N. Virginia)",
        grid_name="PJM Interconnection (RFC East)",
        carbon_intensity_g_per_kwh=385.0,
        utc_offset_hours=-5.0,  # EST standard
        primary_energy_sources=["Natural Gas", "Nuclear", "Coal"],
        data_source="EPA eGRID 2023 / Electricity Maps annual average",
    ),
    "eu-west": RegionCarbonProfile(
        canonical_id="eu-west",
        display_name="Europe West (Ireland)",
        grid_name="EirGrid / Western European Interconnection",
        carbon_intensity_g_per_kwh=260.0,
        utc_offset_hours=0.0,  # WET / GMT standard
        primary_energy_sources=["Natural Gas", "Wind", "Interconnects"],
        data_source="SEAI / Electricity Maps 2023 annual average",
    ),
    "in-south": RegionCarbonProfile(
        canonical_id="in-south",
        display_name="India South (Hyderabad / Chennai)",
        grid_name="Southern Regional Grid (SRG) - NEW Grid",
        carbon_intensity_g_per_kwh=710.0,
        utc_offset_hours=5.5,  # IST (UTC+5:30)
        primary_energy_sources=["Thermal Coal", "Solar", "Hydro"],
        data_source="Central Electricity Authority (CEA) CO2 Baseline Database v19",
    ),
    "us-west": RegionCarbonProfile(
        canonical_id="us-west",
        display_name="US West (Oregon)",
        grid_name="WECC Northwest / BPA",
        carbon_intensity_g_per_kwh=120.0,
        utc_offset_hours=-8.0,  # PST standard
        primary_energy_sources=["Hydro", "Natural Gas", "Wind"],
        data_source="EPA eGRID 2023 Northwest Subregion",
    ),
    "eu-north": RegionCarbonProfile(
        canonical_id="eu-north",
        display_name="Europe North (Sweden)",
        grid_name="Nordic Synchronous Area",
        carbon_intensity_g_per_kwh=25.0,
        utc_offset_hours=1.0,  # CET standard
        primary_energy_sources=["Hydro", "Nuclear", "Wind"],
        data_source="Electricity Maps / Svenska kraftnät",
    ),
    "ap-southeast": RegionCarbonProfile(
        canonical_id="ap-southeast",
        display_name="Asia Pacific (Singapore)",
        grid_name="Singapore National Grid",
        carbon_intensity_g_per_kwh=410.0,
        utc_offset_hours=8.0,  # SGT (UTC+8)
        primary_energy_sources=["Imported Natural Gas", "Solar"],
        data_source="Energy Market Authority (EMA) Singapore",
    ),
    # LOCAL DEPLOYMENT (RTX 3050 Laptop GPU)
    "local-gpu": RegionCarbonProfile(
        canonical_id="local-gpu",
        display_name="Local GPU (RTX 3050)",
        grid_name="US Electrical Grid (Average)",
        carbon_intensity_g_per_kwh=400.0,  # US average grid intensity
        utc_offset_hours=-5.0,  # EST (adjust for actual location)
        primary_energy_sources=["Natural Gas", "Coal", "Renewables"],
        data_source="EPA eGRID 2024 US Average (ESTIMATED for local deployment)",
    ),
}

# Alias mapping for cloud provider region names
REGION_ALIASES: Dict[str, str] = {
    # AWS / GCP / Azure equivalents
    "us-east-1": "us-east",
    "us-east-2": "us-east",
    "useast": "us-east",
    "virginia": "us-east",
    "eu-west-1": "eu-west",
    "euwest": "eu-west",
    "ireland": "eu-west",
    "dublin": "eu-west",
    # NOTE: ap-south-1 is Mumbai (Western grid); mapped here as the nearest modeled Indian
    # region (national-average-like intensity). ap-south-2 is Hyderabad (Southern grid).
    "ap-south-1": "in-south",
    "ap-south-2": "in-south",
    "in-south-1": "in-south",
    "insouth": "in-south",
    "india-south": "in-south",
    "hyderabad": "in-south",
    "chennai": "in-south",
    "us-west-2": "us-west",
    "uswest": "us-west",
    "oregon": "us-west",
    "eu-north-1": "eu-north",
    "eunorth": "eu-north",
    "stockholm": "eu-north",
    "ap-southeast-1": "ap-southeast",
    "singapore": "ap-southeast",
}


def normalize_location(location: str) -> str:
    """Normalize regional identifier to canonical region key."""
    cleaned = location.strip().lower()
    if cleaned in REGION_PROFILES:
        return cleaned
    if cleaned in REGION_ALIASES:
        return REGION_ALIASES[cleaned]
    # Check partial match
    for key in REGION_PROFILES:
        if key in cleaned:
            return key
    raise ValueError(
        f"Unknown location '{location}'. Supported regions: {list(REGION_PROFILES.keys())}."
    )


def get_region_profile(location: str) -> RegionCarbonProfile:
    """Get complete carbon and grid profile for a region."""
    canonical = normalize_location(location)
    return REGION_PROFILES[canonical]


def get_base_carbon_intensity(location: str) -> float:
    """Get published annual baseline grid carbon-intensity in gCO2eq/kWh for a location."""
    profile = get_region_profile(location)
    return profile.carbon_intensity_g_per_kwh
