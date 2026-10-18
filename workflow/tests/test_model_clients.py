"""Provider failures use mocked transports; no test calls a model or sensor."""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import requests

from workflow import model_clients as clients


@pytest.fixture(autouse=True)
def isolated_clients(monkeypatch):
    monkeypatch.setattr(clients, "CODECARBON_AVAILABLE", False)
    monkeypatch.setattr(clients.time, "sleep", Mock())
    monkeypatch.setattr(clients.random, "uniform", lambda low, high: 0)
    monkeypatch.setattr(clients.requests, "post", Mock(side_effect=AssertionError("Unexpected HTTP call")))
    monkeypatch.setattr(clients, "genai", SimpleNamespace(
        Client=Mock(side_effect=AssertionError("Unexpected SDK call")),
    ))


@pytest.fixture
def nvidia_config(monkeypatch, hybrid):
    hybrid(nvidia=True)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    monkeypatch.setenv("NVIDIA_NIM_URL", "https://provider.invalid/v1/chat/completions")
    monkeypatch.setenv("NVIDIA_NIM_MODEL", "test/model")


@pytest.fixture
def gemini_client(monkeypatch, hybrid):
    hybrid(gemini=True)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock()))
    monkeypatch.setattr(clients.genai, "Client", Mock(return_value=client))
    return client


def response(status=200, data=None):
    return SimpleNamespace(status_code=status, json=Mock(return_value=data), text="provider detail")


def sdk_error(code):
    # Use the installed SDK's actual exception type and status metadata.
    return clients.genai_errors.APIError(code, {"error": {"message": "provider failure"}})


@pytest.mark.parametrize("error", [
    ValueError("invalid timeout 500 configuration"),
    ImportError("missing package"),
    RuntimeError("JSON parse failed: 429 is not an object"),
    clients.AuthError("invalid key"),
])
def test_permanent_errors_preserve_type_and_do_not_retry(error):
    operation = Mock(side_effect=error)

    @clients.with_retry
    def invoke():
        return operation()

    with pytest.raises(type(error)) as raised:
        invoke()
    assert raised.value is error
    assert operation.call_count == 1
    clients.time.sleep.assert_not_called()


def test_permanent_error_after_transient_failure_stops_immediately():
    error = ValueError("invalid config")
    operation = Mock(side_effect=[clients.TransientError("try again"), error])

    @clients.with_retry
    def invoke():
        return operation()

    with pytest.raises(ValueError) as raised:
        invoke()
    assert raised.value is error
    assert operation.call_count == 2
    clients.time.sleep.assert_called_once_with(1.0)


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_wins_over_misleading_body(status):
    http_response = requests.Response()
    http_response.status_code = status
    error = requests.HTTPError("503 retry failed", response=http_response)

    @clients.with_retry
    def invoke():
        raise error

    with pytest.raises(clients.AuthError) as raised:
        invoke()
    assert raised.value.__cause__ is error
    clients.time.sleep.assert_not_called()


def test_transient_retries_are_bounded_and_do_not_print_credentials(capsys):
    secret = "key-that-must-not-be-printed"
    error = clients.TransientError(secret)
    operation = Mock(side_effect=error)

    @clients.with_retry
    def invoke():
        return operation()

    with pytest.raises(RuntimeError, match="failed after 3 retries") as raised:
        invoke()
    assert raised.value.__cause__ is error
    assert operation.call_count == 4
    assert clients.time.sleep.call_args_list == [call(1.0), call(2.0), call(4.0)]
    assert secret not in capsys.readouterr().out
    assert secret not in str(raised.value)


def test_retry_delay_including_jitter_is_capped(monkeypatch):
    monkeypatch.setattr(clients, "MAX_DELAY_SECONDS", 0.5)
    monkeypatch.setattr(clients.random, "uniform", lambda low, high: high)
    operation = Mock(side_effect=[TimeoutError(), "ok"])

    @clients.with_retry
    def invoke():
        return operation()

    assert invoke() == "ok"
    clients.time.sleep.assert_called_once_with(0.5)


@pytest.mark.parametrize("status", [401, 403, 404, 400])
def test_nvidia_permanent_http_errors_do_not_retry(nvidia_config, monkeypatch, status):
    post = Mock(return_value=response(status, {"detail": "test-nvidia-key"}))
    monkeypatch.setattr(clients.requests, "post", post)
    error_type = clients.AuthError if status in (401, 403) else RuntimeError
    with pytest.raises(error_type) as raised:
        clients.call_nemotron_ultra_3("test")
    assert "test-nvidia-key" not in str(raised.value)
    assert post.call_count == 1
    clients.time.sleep.assert_not_called()


@pytest.mark.parametrize("failure", [
    response(429), response(503), requests.Timeout("timeout"), requests.ConnectionError("offline"),
])
def test_nvidia_retries_transient_failures_then_returns_usage(nvidia_config, monkeypatch, failure):
    result = response(data={
        "choices": [{"message": {"content": "answer"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    })
    post = Mock(side_effect=[failure, result])
    monkeypatch.setattr(clients.requests, "post", post)
    actual = clients.call_nemotron_ultra_3("test")
    assert actual["text"] == "answer"
    assert (actual["input_tokens"], actual["output_tokens"]) == (7, 3)
    assert actual["token_source"] == "provider_reported"
    assert actual["model"] == "nemotron-ultra-3"
    assert post.call_count == 2


@pytest.mark.parametrize("missing, error_type", [
    ("NVIDIA_API_KEY", clients.AuthError),
    ("NVIDIA_NIM_URL", ValueError),
    ("NVIDIA_NIM_MODEL", ValueError),
])
def test_nvidia_missing_config_fails_before_transport(nvidia_config, monkeypatch, missing, error_type):
    monkeypatch.delenv(missing)
    with pytest.raises(error_type, match=missing):
        clients.call_nemotron_ultra_3("test")
    clients.requests.post.assert_not_called()
    clients.time.sleep.assert_not_called()


@pytest.mark.parametrize("data", [{"choices": []}, ["not an object"]])
def test_nvidia_parse_error_is_not_auth_and_not_retried(nvidia_config, monkeypatch, data):
    post = Mock(return_value=response(data=data))
    monkeypatch.setattr(clients.requests, "post", post)
    with pytest.raises(RuntimeError, match="Failed to parse"):
        clients.call_nemotron_ultra_3("test")
    assert post.call_count == 1
    clients.time.sleep.assert_not_called()


def test_nvidia_reads_config_added_after_module_import(nvidia_config, monkeypatch):
    monkeypatch.setattr(clients, "NVIDIA_NIM_URL", None)
    monkeypatch.setattr(clients, "NVIDIA_NIM_MODEL", None)
    post = Mock(return_value=response(data={"choices": [{"message": {"content": "ok"}}]}))
    monkeypatch.setattr(clients.requests, "post", post)
    clients.call_nemotron_ultra_3("test")
    assert post.call_args.args[0] == "https://provider.invalid/v1/chat/completions"
    assert post.call_args.kwargs["json"]["model"] == "test/model"


@pytest.mark.parametrize("provider", ["nvidia", "local"])
def test_invalid_endpoint_is_configuration_error(nvidia_config, monkeypatch, provider):
    post = Mock(side_effect=requests.exceptions.InvalidURL("invalid url"))
    monkeypatch.setattr(clients.requests, "post", post)
    invoke = clients.call_nemotron_ultra_3 if provider == "nvidia" else clients.call_local_llm
    with pytest.raises(ValueError, match="configuration"):
        invoke("test")
    assert post.call_count == 1
    clients.time.sleep.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 404, 400])
def test_gemini_permanent_errors_do_not_retry_or_substitute_model(gemini_client, status):
    gemini_client.models.generate_content.side_effect = sdk_error(status)
    error_type = clients.AuthError if status in (401, 403) else RuntimeError
    with pytest.raises(error_type):
        clients.call_gemini_3_pro("test")
    gemini_client.models.generate_content.assert_called_once_with(
        model=clients.GEMINI_3_PRO_MODEL, contents="test",
    )
    clients.time.sleep.assert_not_called()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_gemini_retries_sdk_status_and_preserves_result(gemini_client, status):
    gemini_client.models.generate_content.side_effect = [sdk_error(status), SimpleNamespace(
        text="answer", usage_metadata=SimpleNamespace(prompt_token_count=9, candidates_token_count=2),
    )]
    actual = clients.call_gemini_3_flash("test")
    assert actual["text"] == "answer"
    assert (actual["input_tokens"], actual["output_tokens"]) == (9, 2)
    assert actual["token_source"] == "provider_reported"
    assert actual["model"] == "gemini-3-flash"
    assert gemini_client.models.generate_content.call_count == 2


def test_gemini_retries_httpx_timeout(gemini_client):
    import httpx

    gemini_client.models.generate_content.side_effect = [
        httpx.ReadTimeout("timeout"), SimpleNamespace(text="ok", usage_metadata=None),
    ]
    assert clients.call_gemini_3_flash("test")["text"] == "ok"
    assert gemini_client.models.generate_content.call_count == 2


def test_gemini_missing_sdk_is_not_reported_as_auth(gemini_client, monkeypatch):
    monkeypatch.setattr(clients, "genai", None)
    with pytest.raises(ImportError, match="google-genai"):
        clients.call_gemini_3_flash("test")
    clients.time.sleep.assert_not_called()


def test_gemini_response_parse_error_fails_without_retry(gemini_client):
    gemini_client.models.generate_content.return_value = SimpleNamespace()
    with pytest.raises(RuntimeError, match="Failed to parse"):
        clients.call_gemini_3_flash("test")
    assert gemini_client.models.generate_content.call_count == 1
    clients.time.sleep.assert_not_called()


@pytest.mark.parametrize("failure, expected", [
    (requests.ConnectionError("offline"), ConnectionError),
    (requests.ReadTimeout("slow"), TimeoutError),
    (requests.ConnectTimeout("slow connect"), TimeoutError),
])
def test_local_transport_errors_keep_executor_contract(monkeypatch, failure, expected):
    post = Mock(side_effect=failure)
    monkeypatch.setattr(clients.requests, "post", post)
    with pytest.raises(expected) as raised:
        clients.call_local_llm("test")
    assert raised.value.__cause__ is failure
    assert post.call_count == 1


def test_local_success_preserves_energy_provenance(monkeypatch):
    monkeypatch.setattr(clients.requests, "post", Mock(return_value=response(data={
        "response": "answer", "prompt_eval_count": 5, "eval_count": 2,
    })))
    monkeypatch.setattr(clients.time, "perf_counter", Mock(side_effect=[10.0, 11.0]))
    actual = clients.call_local_llm("test")
    assert actual["latency_ms"] == 1000
    assert actual["energy_wh"] == pytest.approx(48 / 3600)
    assert actual["energy_measured"] is False
    assert "TDP estimate" in actual["energy_method"]
    assert (actual["input_tokens"], actual["output_tokens"]) == (5, 2)
    assert actual["token_source"] == "provider_reported"


def test_local_authenticated_endpoint_fails_as_auth(monkeypatch):
    monkeypatch.setattr(clients.requests, "post", Mock(return_value=response(401)))
    with pytest.raises(clients.AuthError):
        clients.call_local_llm("test")


@pytest.mark.parametrize("provider", ["nvidia", "gemini", "local"])
@pytest.mark.parametrize("counts, source, expected", [
    ((None, None), "unknown", (0, 0)),
    ((0, 0), "provider_reported", (0, 0)),
    ((7, None), "partially_provider_reported", (7, 0)),
    ((None, 3), "partially_provider_reported", (0, 3)),
])
def test_token_provenance_distinguishes_missing_usage_from_reported_zero(
        nvidia_config, gemini_client, monkeypatch, provider, counts, source, expected):
    if provider == "gemini":
        gemini_client.models.generate_content.return_value = SimpleNamespace(
            text="answer", usage_metadata=SimpleNamespace(
                prompt_token_count=counts[0], candidates_token_count=counts[1]),
        )
        result = clients.call_gemini_3_flash("test")
    else:
        if provider == "nvidia":
            data = {"choices": [{"message": {"content": "answer"}}],
                    "usage": {"prompt_tokens": counts[0], "completion_tokens": counts[1]}}
            invoke = clients.call_nemotron_ultra_3
            monkeypatch.setattr(clients.availability, "nvidia_status", lambda: (True, "test"))
        else:
            data = {"response": "answer", "prompt_eval_count": counts[0], "eval_count": counts[1]}
            invoke = clients.call_local_llm
        monkeypatch.setattr(clients.requests, "post", Mock(return_value=response(data=data)))
        result = invoke("test")
    assert (result["input_tokens"], result["output_tokens"]) == expected
    assert result["token_source"] == source


@pytest.mark.parametrize("invalid", [-1, 1.5, True, "17"])
def test_invalid_provider_usage_fails_parse_without_retry(nvidia_config, monkeypatch, invalid):
    post = Mock(return_value=response(data={
        "choices": [{"message": {"content": "answer"}}],
        "usage": {"prompt_tokens": invalid, "completion_tokens": 3},
    }))
    monkeypatch.setattr(clients.requests, "post", post)
    with pytest.raises(RuntimeError, match="Failed to parse"):
        clients.call_nemotron_ultra_3("test")
    assert post.call_count == 1
    clients.time.sleep.assert_not_called()
