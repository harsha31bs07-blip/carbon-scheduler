"""Carbon source coverage, freshness and provenance without external APIs."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from data.carbon_sources import CarbonAvailability, HistoricalCarbonTrace, StaticCarbonEstimate
from shared.scheduling import CarbonSample


NOW = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)


def row(**changes):
    return replace(CarbonSample("explicit-grid", NOW - timedelta(minutes=10), NOW,
                               NOW + timedelta(hours=1), 250, "caller-trace", "measured"), **changes)


def trace(*rows, max_age=timedelta(hours=2)):
    return HistoricalCarbonTrace("trace-v1", tuple(rows) or (row(),), max_age)


def test_timestamped_measured_trace_roundtrip_and_load(tmp_path):
    # This inline fixture claims measured provenance; no real grid is selected.
    record = trace()
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(record.to_dict()), encoding="utf-8")
    restored = HistoricalCarbonTrace.load(path)
    assert restored.sample("explicit-grid", NOW, as_of=NOW) == row()
    assert restored.fingerprint() == record.fingerprint()
    assert restored.sample("explicit-grid", NOW, as_of=NOW).provenance == "measured"


@pytest.mark.parametrize("provenance", ["measured", "estimated", "mixed", "simulated"])
def test_provenance_is_explicit_and_preserved(provenance):
    provider = trace(row(provenance=provenance))
    assert provider.sample("explicit-grid", NOW, as_of=NOW).provenance == provenance


@pytest.mark.parametrize("region, instant, as_of, expected", [
    (None, NOW, NOW, "unknown-region"),
    ("unknown-grid", NOW, NOW, "missing-region"),
    ("explicit-grid", NOW - timedelta(microseconds=1), NOW, "missing-coverage"),
    ("explicit-grid", NOW + timedelta(hours=1), NOW, "missing-coverage"),
    ("explicit-grid", NOW, NOW - timedelta(minutes=11), "not-yet-issued"),
    ("explicit-grid", NOW, NOW + timedelta(hours=2), "stale"),
])
def test_missing_or_unusable_data_remains_unknown(region, instant, as_of, expected):
    provider = trace()
    result = provider.availability(region, instant, as_of=as_of)
    assert result.status == expected
    assert result.sample is None
    assert provider.sample(region, instant, as_of=as_of) is None


def test_freshness_boundary_is_inclusive_but_validity_is_half_open():
    provider = trace(max_age=timedelta(minutes=10))
    assert provider.sample("explicit-grid", NOW, as_of=NOW) is not None
    assert provider.availability("explicit-grid", NOW, as_of=NOW + timedelta(microseconds=1)).status == "stale"
    assert provider.sample("explicit-grid", NOW + timedelta(hours=1), as_of=NOW) is None


def test_forecast_is_available_only_after_it_was_issued():
    sample = row(issued_at=NOW, valid_from=NOW + timedelta(hours=1), valid_until=NOW + timedelta(hours=2))
    provider = trace(sample)
    assert provider.sample("explicit-grid", NOW + timedelta(hours=1), as_of=NOW) == sample
    assert provider.availability("explicit-grid", NOW + timedelta(hours=1), as_of=NOW - timedelta(seconds=1)).status == "not-yet-issued"


def test_unknown_physical_grid_never_uses_other_region_or_coordinator_region():
    provider = trace(row(region="some-other-grid"))
    assert provider.sample(None, NOW, as_of=NOW) is None
    assert provider.sample("IN-KA", NOW, as_of=NOW) is None


def test_ambiguous_overlap_rejected_even_if_a_revision_was_issued_later():
    with pytest.raises(ValueError, match="overlapping"):
        trace(row(), row(issued_at=NOW, valid_from=NOW + timedelta(minutes=30)))


def test_adjacent_windows_and_different_regions_are_unambiguous():
    second = row(valid_from=NOW + timedelta(hours=1), valid_until=NOW + timedelta(hours=2), value=100)
    provider = trace(second, row(region="another-grid"), row())
    assert provider.sample("explicit-grid", NOW + timedelta(hours=1), as_of=NOW).value == 100
    assert provider.sample("another-grid", NOW, as_of=NOW).value == 250


def test_trace_fingerprint_ignores_input_order_but_changes_with_freshness_or_values():
    second = row(region="another-grid")
    assert trace(row(), second).fingerprint() == trace(second, row()).fingerprint()
    assert trace().fingerprint() != trace(max_age=timedelta(hours=1)).fingerprint()
    assert trace().fingerprint() != trace(row(value=251)).fingerprint()


def test_timezone_conversion_without_timezone_guessing():
    offset = timezone(timedelta(hours=5, minutes=30))
    provider = trace()
    assert provider.sample("explicit-grid", NOW.astimezone(offset), as_of=NOW.astimezone(offset)) == row()
    for instant, as_of in [(NOW.replace(tzinfo=None), NOW), (NOW, NOW.replace(tzinfo=None))]:
        with pytest.raises(ValueError, match="timezone-aware"):
            provider.sample("explicit-grid", instant, as_of=as_of)


def test_static_estimate_requires_explicit_metadata_and_never_claims_measured():
    estimate = StaticCarbonEstimate("known-grid", 300, "operator-estimate", NOW,
                                    NOW, NOW + timedelta(hours=1), timedelta(hours=1))
    sample = estimate.sample("known-grid", NOW, as_of=NOW)
    assert sample.value == 300 and sample.provenance == "estimated"
    assert estimate.sample(None, NOW, as_of=NOW) is None
    assert estimate.sample("different-grid", NOW, as_of=NOW) is None
    restored = StaticCarbonEstimate.from_dict(estimate.to_dict())
    assert restored.fingerprint() == estimate.fingerprint()
    assert restored.sample("known-grid", NOW, as_of=NOW) == sample
    forged = estimate.to_dict()
    forged["sample"]["provenance"] = "measured"
    with pytest.raises(ValueError, match="estimated"):
        StaticCarbonEstimate.from_dict(forged)


def test_carbon_dioxide_only_static_factor_preserves_its_distinct_scope():
    estimate = StaticCarbonEstimate("IN", 675, "CEA-v22-fixture-CO2-only", NOW,
                                    NOW, NOW + timedelta(days=1), timedelta(days=1), unit="gCO2/kWh")
    restored = StaticCarbonEstimate.from_dict(estimate.to_dict())
    sample = restored.sample("IN", NOW, as_of=NOW)
    assert sample.unit == "gCO2/kWh" and sample.value == 675
    assert restored.fingerprint() == estimate.fingerprint()
    equivalent = StaticCarbonEstimate("IN", 675, "CEA-v22-fixture-CO2-only", NOW,
                                      NOW, NOW + timedelta(days=1), timedelta(days=1), unit="gCO2e/kWh")
    assert restored.fingerprint() != equivalent.fingerprint()


def test_trace_cannot_silently_switch_between_co2_and_co2e():
    nonoverlapping = row(unit="gCO2/kWh", valid_from=NOW + timedelta(hours=1), valid_until=NOW + timedelta(hours=2))
    with pytest.raises(ValueError, match="greenhouse-gas units"):
        trace(row(), nonoverlapping)
    provider = trace(row(), row(region="other-grid", unit="gCO2/kWh"))
    assert provider.sample("explicit-grid", NOW, as_of=NOW).unit == "gCO2e/kWh"
    assert provider.sample("other-grid", NOW, as_of=NOW).unit == "gCO2/kWh"


@pytest.mark.parametrize("change", [
    {"schema_version": 99}, {"schema_version": True}, {"max_age_seconds": -1},
    {"max_age_seconds": 0}, {"max_age_seconds": float("nan")},
    {"max_age_seconds": float("inf")}, {"max_age_seconds": True},
    {"extra": "field"}, {"record_type": "OtherTrace"}, {"samples": {}},
])
def test_malformed_versioned_trace_rejected(change):
    with pytest.raises(ValueError):
        HistoricalCarbonTrace.from_dict({**trace().to_dict(), **change})


def test_duplicate_json_fields_rejected(tmp_path):
    path = tmp_path / "trace.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        HistoricalCarbonTrace.load(path)


def test_availability_does_not_allow_value_with_missing_status():
    with pytest.raises(ValueError):
        CarbonAvailability("stale", row())
    with pytest.raises(ValueError):
        CarbonAvailability("available")
    with pytest.raises(ValueError):
        CarbonAvailability("stale", "not-a-sample")
