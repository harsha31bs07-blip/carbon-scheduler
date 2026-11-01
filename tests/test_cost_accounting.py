"""Explicit tariffs, actual usage, retry unknowns and API-only accounting."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from data.cost_accounting import ApiTariff, TariffBook, cost_from_usage, local_api_tariff


NOW = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
DEPLOYMENT = "a" * 64


def tariff(**changes):
    # Rates are arbitrary fixtures, never defaults or current provider prices.
    return replace(ApiTariff("gemini", "exact-model-revision", 2, 6, "caller-supplied-fixture",
                             NOW, NOW, NOW + timedelta(days=1)), **changes)


def calculate(row=None, input_tokens=1000, output_tokens=250, **changes):
    kwargs = {"provider": "gemini", "model_id": "exact-model-revision", "instant": NOW, "as_of": NOW}
    return cost_from_usage(row or tariff(), input_tokens, output_tokens, **{**kwargs, **changes})


def test_actual_counts_and_explicit_rates_calculate_api_cost():
    metric = calculate()
    assert metric.value == pytest.approx(.0035)
    assert metric.unit == "USD" and metric.provenance == "estimated"
    assert "caller-supplied-fixture" in metric.source
    assert "api-only" in metric.source and "electricity-excluded" in metric.source


@pytest.mark.parametrize("input_tokens,output_tokens", [(None, None), (100, None), (None, 100)])
def test_missing_usage_from_any_attempt_is_not_a_zero_charge(input_tokens, output_tokens):
    metric = calculate(input_tokens=input_tokens, output_tokens=output_tokens)
    assert metric.value is None and metric.provenance == "unknown"
    assert "missing-token-usage" in metric.source


def test_failed_retry_unknown_usage_cannot_be_ignored_in_total():
    # A successful retry's cost does not account for an earlier unmetered call.
    attempts = [calculate(input_tokens=None, output_tokens=None), calculate()]
    assert attempts[0].value is None
    assert attempts[1].value > 0
    complete_total = None if any(row.value is None for row in attempts) else sum(row.value for row in attempts)
    assert complete_total is None


def test_exact_zero_usage_is_known_when_both_counts_reported():
    metric = calculate(input_tokens=0, output_tokens=0)
    assert metric.value == 0 and metric.provenance == "estimated"


def test_explicit_local_zero_api_tariff_allows_missing_usage_without_claiming_free_energy():
    row = local_api_tariff("llama3.2:3b", verified_at=NOW, valid_from=NOW,
                           valid_until=NOW + timedelta(days=1), deployment_fingerprint=DEPLOYMENT)
    metric = cost_from_usage(row, None, None, provider="ollama", model_id="llama3.2:3b",
                             instant=NOW, as_of=NOW, deployment_fingerprint=DEPLOYMENT)
    assert metric.value == 0 and metric.provenance == "estimated"
    assert "local-no-api-subscription" in metric.source
    assert "api-only" in metric.source and "electricity-excluded" in metric.source
    assert cost_from_usage(row, None, None, provider="ollama", model_id="llama3.2:3b",
                           instant=NOW, as_of=NOW).value is None


def test_one_free_direction_does_not_make_missing_usage_known():
    assert calculate(tariff(input_usd_per_million=0), input_tokens=None).value is None
    assert calculate(tariff(output_usd_per_million=0), output_tokens=None).value is None


@pytest.mark.parametrize("changes, reason", [
    ({"model_id": "similar-model"}, "model-mismatch"),
    ({"provider": "nvidia"}, "model-mismatch"),
    ({"instant": NOW - timedelta(microseconds=1)}, "outside-validity"),
    ({"instant": NOW + timedelta(days=1)}, "outside-validity"),
    ({"as_of": NOW - timedelta(microseconds=1)}, "not-yet-verified"),
])
def test_stale_future_or_mismatched_rate_is_unknown(changes, reason):
    metric = calculate(**changes)
    assert metric.value is None and reason in metric.source


def test_deployment_rate_cannot_apply_to_other_endpoint_configuration():
    row = tariff(deployment_fingerprint=DEPLOYMENT)
    assert calculate(row).value is None
    assert calculate(row, deployment_fingerprint="b" * 64).value is None
    assert calculate(row, deployment_fingerprint=DEPLOYMENT).value == pytest.approx(.0035)


def test_book_exact_lookup_and_adjacent_rate_periods(tmp_path):
    second = tariff(input_usd_per_million=4, valid_from=NOW + timedelta(days=1), valid_until=NOW + timedelta(days=2))
    book = TariffBook((second, tariff()))
    path = tmp_path / "tariffs.json"
    path.write_text(json.dumps(book.to_dict()), encoding="utf-8")
    restored = TariffBook.load(path)
    assert restored.fingerprint() == book.fingerprint()
    assert restored.estimate_usage("gemini", "exact-model-revision", 1_000_000, 0,
                                   instant=NOW + timedelta(days=1), as_of=NOW).value == 4
    assert restored.estimate_usage("gemini", "model-alias", 100, 100, instant=NOW, as_of=NOW).value is None
    assert restored.estimate_usage("gemini", "exact-model-revision", 100, 100,
                                   instant=NOW + timedelta(days=2), as_of=NOW).value is None


def test_overlapping_rates_and_global_deployment_conflict_fail():
    with pytest.raises(ValueError, match="ambiguous"):
        TariffBook((tariff(), tariff()))
    with pytest.raises(ValueError, match="ambiguous"):
        TariffBook((tariff(), tariff(deployment_fingerprint=DEPLOYMENT)))
    separate = TariffBook((tariff(deployment_fingerprint=DEPLOYMENT), tariff(deployment_fingerprint="b" * 64)))
    assert len(separate.tariffs) == 2


def test_tariff_roundtrip_timezone_and_fingerprint_consistency():
    offset = timezone(timedelta(hours=5, minutes=30))
    row = tariff(verified_at=NOW.astimezone(offset))
    assert ApiTariff.from_dict(row.to_dict()) == tariff()
    assert TariffBook((row,)).fingerprint() == TariffBook((tariff(),)).fingerprint()


@pytest.mark.parametrize("field,value", [
    ("input_usd_per_million", -1), ("output_usd_per_million", float("nan")),
    ("input_usd_per_million", float("inf")), ("input_usd_per_million", True),
    ("verified_at", NOW.replace(tzinfo=None)), ("valid_until", NOW),
    ("scope", "total-cost-of-ownership"), ("deployment_fingerprint", "wrong"),
    ("source", ""), ("model_id", ""), ("provider", "unknown"),
])
def test_invalid_tariffs_rejected(field, value):
    with pytest.raises(ValueError):
        tariff(**{field: value})


@pytest.mark.parametrize("value", [-1, True, 1.5, float("nan"), "100"])
def test_invalid_usage_rejected_even_for_zero_tariff(value):
    row = tariff(input_usd_per_million=0, output_usd_per_million=0)
    with pytest.raises(ValueError):
        calculate(row, input_tokens=value)


def test_cost_overflow_preserves_unknown_instead_of_nonfinite_json():
    assert calculate(input_tokens=10 ** 1000).value is None
    assert calculate(tariff(input_usd_per_million=1e308), input_tokens=10 ** 20).value is None


@pytest.mark.parametrize("changes", [{"schema_version": 2}, {"schema_version": True}, {"extra": 1},
                                    {"record_type": "Other"}, {"verified_at": 123}])
def test_invalid_versioned_tariffs_rejected(changes):
    with pytest.raises(ValueError):
        ApiTariff.from_dict({**tariff().to_dict(), **changes})


def test_duplicate_json_tariff_keys_rejected(tmp_path):
    path = tmp_path / "tariff.json"
    path.write_text('{"tariffs": [], "tariffs": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        TariffBook.load(path)
