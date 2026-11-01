"""Explicit routing, identity, failure provenance, and safe provider boundaries."""

from types import SimpleNamespace
import traceback

import pytest
import requests

from shared.deployments import Deployment
from workflow import deployment_clients as clients


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        self.closed = False

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request")
    monkeypatch.setattr(clients.requests, "post", forbidden)
    monkeypatch.setattr(clients.requests, "get", forbidden)
    monkeypatch.setattr(clients.time, "sleep", lambda *_: None)


def ollama(deployment_id="edge-a", endpoint="http://edge-a:11434"):
    return Deployment(deployment_id, "ollama", endpoint, {"small": "qwen:exact-tag"})


def test_same_model_routes_to_selected_endpoint_only(monkeypatch):
    calls = []
    responses = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        response = Response({"model": "qwen:exact-tag", "response": "answer", "done": True,
                             "prompt_eval_count": 12, "eval_count": 7})
        responses.append(response)
        return response

    monkeypatch.setattr(clients.requests, "post", post)
    first = clients.call_deployment(ollama(), "small", "hello")
    second = clients.call_deployment(ollama("edge-b", "http://edge-b:11434/"), "small", "hello")
    assert [url for url, _ in calls] == ["http://edge-a:11434/api/generate",
                                        "http://edge-b:11434/api/generate"]
    assert all(call[1]["json"]["model"] == "qwen:exact-tag" for call in calls)
    assert all(call[1]["allow_redirects"] is False for call in calls)
    assert all(response.closed for response in responses)
    assert first["deployment_id"] == "edge-a"
    assert second["deployment_id"] == "edge-b"
    assert first["model_id"] == "small"
    assert first["provider_model_id"] == "qwen:exact-tag"
    assert first["reported_model_id"] == "qwen:exact-tag"
    assert first["input_tokens"] == 12 and first["output_tokens"] == 7
    assert first["token_source"] == "provider_reported"
    assert first["region"] is None and first["grid"] is None
    assert first["energy_wh"] is None and first["carbon_g"] is None
    assert first["energy_measured"] is False


def test_unsupported_pair_is_rejected_before_http():
    with pytest.raises(ValueError, match="not configured"):
        clients.call_deployment(ollama(), "large", "hello")


def test_custom_credentials_resolved_each_call_without_global_mutation(monkeypatch):
    deployment = Deployment("nim", "nvidia", "https://nim.example/v1/chat/completions",
                            {"reasoner": "vendor/exact-model"}, credential_env="OTHER_NIM_KEY")
    seen = []

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return Response({"choices": [{"message": {"content": "ok"}}],
                         "model": "vendor/reported-version"})

    monkeypatch.setattr(clients.requests, "post", post)
    monkeypatch.setenv("NVIDIA_API_KEY", "default-unused")
    monkeypatch.setenv("OTHER_NIM_KEY", "first-key")
    first = clients.call_deployment(deployment, "reasoner", "hello", timeout_seconds=8)
    monkeypatch.setenv("OTHER_NIM_KEY", "second-key")
    clients.call_deployment(deployment, "reasoner", "hello")
    assert seen[0][0] == deployment.endpoint
    assert seen[0][1]["headers"] == {"Authorization": "Bearer first-key"}
    assert seen[1][1]["headers"] == {"Authorization": "Bearer second-key"}
    assert seen[0][1]["json"]["model"] == "vendor/exact-model"
    assert seen[0][1]["timeout"] == (5.0, 8.0)
    assert first["provider_model_id"] == "vendor/exact-model"
    assert first["reported_model_id"] == "vendor/reported-version"
    assert first["input_tokens"] is None and first["output_tokens"] is None
    assert "first-key" not in str(first) and "default-unused" not in str(first)
    assert clients.os.environ["NVIDIA_API_KEY"] == "default-unused"


def test_missing_key_is_preflight_failure_not_request_attempt(monkeypatch):
    deployment = Deployment("nim", "nvidia", "https://nim.example/v1/chat/completions",
                            {"small": "vendor/model"}, credential_env="MISSING_NIM_KEY")
    monkeypatch.delenv("MISSING_NIM_KEY", raising=False)
    with pytest.raises(clients.DeploymentAuthError) as raised:
        clients.call_deployment(deployment, "small", "hello", max_attempts=3)
    assert raised.value.error_code == "missing-credential"
    assert raised.value.attempt_count == 0
    assert raised.value.request_attempted is False


@pytest.mark.parametrize("key", [
    "abc\u2603def", "abc\u00e9def", "abc\x01def", "abc\x1fdef", "abc\x7fdef",
    "abc\tdef", "abc\ndef", "abc\rdef", " abcdef", "abcdef ", "abc def", "",
])
def test_invalid_bearer_credentials_are_rejected_before_http(monkeypatch, key):
    deployment = Deployment("nim", "nvidia", "https://nim.example/v1/chat/completions",
                            {"small": "vendor/model"}, credential_env="REVIEW_KEY")
    monkeypatch.setenv("REVIEW_KEY", key)
    with pytest.raises(clients.DeploymentAuthError) as raised:
        clients.call_deployment(deployment, "small", "hello", max_attempts=3)
    assert raised.value.error_code == "invalid-credential"
    assert raised.value.attempt_count == 0
    assert raised.value.request_attempted is False
    formatted = "".join(traceback.format_exception(raised.value))
    assert "REVIEW_KEY" not in formatted
    if key:
        assert key not in formatted


def test_malformed_nvidia_usage_is_not_silently_treated_as_unknown(monkeypatch):
    deployment = Deployment("nim", "nvidia", "https://nim.example/v1/chat/completions",
                            {"small": "vendor/model"}, credential_env="CUSTOM_NIM_KEY")
    monkeypatch.setenv("CUSTOM_NIM_KEY", "test-key")
    monkeypatch.setattr(clients.requests, "post", lambda *_a, **_k: Response({
        "choices": [{"message": {"content": "ok"}}], "usage": [],
    }))
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(deployment, "small", "hello")
    assert raised.value.error_code == "invalid-token-usage"
    assert raised.value.attempt_count == 1


def test_missing_sdk_is_not_a_network_attempt(monkeypatch):
    deployment = Deployment("gemini", "gemini", "https://gateway.example",
                            {"large": "gemini-version"}, credential_env="CUSTOM_GEMINI_KEY")
    monkeypatch.setenv("CUSTOM_GEMINI_KEY", "test-key")

    def unavailable():
        raise clients.DeploymentCallError("gemini-sdk-unavailable")

    monkeypatch.setattr(clients, "_load_genai", unavailable)
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(deployment, "large", "hello")
    assert raised.value.error_code == "gemini-sdk-unavailable"
    assert raised.value.attempt_count == 0
    assert raised.value.request_attempted is False


@pytest.mark.parametrize("status, expected, retryable", [
    (401, clients.DeploymentAuthError, False),
    (403, clients.DeploymentAuthError, False),
    (429, clients.DeploymentTransientError, True),
    (500, clients.DeploymentTransientError, True),
    (302, clients.DeploymentCallError, False),
    (404, clients.DeploymentCallError, False),
])
def test_status_classification_sanitized_no_fallback(monkeypatch, status, expected, retryable):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return Response({"error": "provider echoed secret-key"}, status=status)

    monkeypatch.setattr(clients.requests, "post", post)
    with pytest.raises(expected) as raised:
        clients.call_deployment(ollama(), "small", "hello", max_attempts=2)
    assert raised.value.retryable is retryable
    assert raised.value.attempt_count == (2 if retryable else 1)
    assert raised.value.request_attempted is True
    assert calls == ["http://edge-a:11434/api/generate"] * raised.value.attempt_count
    assert "secret-key" not in "".join(traceback.format_exception(raised.value))


def test_retry_keeps_pair_and_counts_provider_wall_time(monkeypatch):
    calls = []
    times = iter([100.0, 101.7])
    monkeypatch.setattr(clients.time, "perf_counter", lambda: next(times))

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]["model"], kwargs["json"]["options"]["num_predict"],
                      kwargs["json"]["think"]))
        if len(calls) == 1:
            raise requests.exceptions.Timeout("request headers contain secret-key")
        return Response({"response": "ok", "done": True})

    monkeypatch.setattr(clients.requests, "post", post)
    result = clients.call_deployment(ollama(), "small", "hello", max_attempts=2,
                                     output_limit_tokens=21, enable_thinking=False)
    assert calls == [("http://edge-a:11434/api/generate", "qwen:exact-tag", 21, False)] * 2
    assert result["attempt_count"] == 2
    assert result["latency_ms"] == pytest.approx(1700)


@pytest.mark.parametrize("body", [
    {"response": "ok", "done": True, "eval_count": True},
    {"response": "ok", "done": True, "prompt_eval_count": -1},
    {"response": None, "done": True},
    {"response": "ok", "done": True, "model": "bad model id"},
    {"error": "secret-provider-error"},
    ValueError("secret-json-body"),
])
def test_invalid_response_retains_attempt_provenance_and_safe_errors(monkeypatch, body):
    monkeypatch.setattr(clients.requests, "post", lambda *_args, **_kwargs: Response(body))
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(ollama(), "small", "hello")
    assert raised.value.attempt_count == 1
    assert raised.value.request_attempted is True
    assert "secret" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("body,error_code", [
    ({"response": "partial", "done": False}, "incomplete-response"),
    ({"response": "partial"}, "invalid-response"),
    ({"response": "partial", "done": None}, "invalid-response"),
    ({"response": "partial", "done": 1}, "invalid-response"),
    ({"response": "partial", "done": "true"}, "invalid-response"),
])
def test_ollama_requires_confirmed_completed_response(monkeypatch, body, error_code):
    response = Response(body)
    monkeypatch.setattr(clients.requests, "post", lambda *_a, **_k: response)
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(ollama(), "small", "hello", max_attempts=3)
    assert raised.value.error_code == error_code
    assert raised.value.retryable is False
    assert raised.value.attempt_count == 1
    assert raised.value.request_attempted is True
    assert response.closed is True


def test_gemini_exact_base_url_model_credentials_and_resource_close(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            self.models = self

        def generate_content(self, **kwargs):
            seen["request"] = kwargs
            return SimpleNamespace(text="ok", model_version="gemini-reported-version",
                                   usage_metadata=SimpleNamespace(prompt_token_count=4,
                                                                  candidates_token_count=0))

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(clients, "_load_genai", lambda: SimpleNamespace(Client=Client))
    monkeypatch.setenv("CUSTOM_GEMINI_KEY", "custom-secret")
    deployment = Deployment("gemini-gateway", "gemini", "https://gateway.example",
                            {"large": "gemini-exact-version"}, credential_env="CUSTOM_GEMINI_KEY")
    result = clients.call_deployment(deployment, "large", "hello", timeout_seconds=2.5)
    assert seen["client"] == {
        "vertexai": False, "api_key": "custom-secret",
        "http_options": {"base_url": "https://gateway.example", "timeout": 2500,
                         "retry_options": {"attempts": 1},
                         "client_args": {"follow_redirects": False}},
    }
    assert seen["request"]["model"] == "gemini-exact-version"
    assert seen["closed"] is True
    assert result["provider_model_id"] == "gemini-exact-version"
    assert result["reported_model_id"] == "gemini-reported-version"
    assert result["output_tokens"] == 0 and result["token_source"] == "provider_reported"
    assert "custom-secret" not in str(result)


def test_gemini_auth_rejection_never_echoes_provider_message(monkeypatch):
    class ProviderError(Exception):
        code = 401

    class Client:
        def __init__(self, **kwargs):
            self.models = self

        def generate_content(self, **kwargs):
            raise ProviderError("provider secret-key and raw request")

        def close(self):
            pass

    monkeypatch.setattr(clients, "_load_genai", lambda: SimpleNamespace(Client=Client))
    monkeypatch.setenv("CUSTOM_GEMINI_KEY", "custom-secret")
    deployment = Deployment("gemini", "gemini", "https://gateway.example",
                            {"large": "gemini-exact-version"}, credential_env="CUSTOM_GEMINI_KEY")
    with pytest.raises(clients.DeploymentAuthError) as raised:
        clients.call_deployment(deployment, "large", "hello", max_attempts=3)
    assert raised.value.attempt_count == 1
    assert "secret" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301, True])
def test_invalid_timeouts_do_not_send_requests(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        clients.call_deployment(ollama(), "small", "hello", timeout_seconds=timeout)


@pytest.mark.parametrize("limit", [1, 27, 32768])
@pytest.mark.parametrize("thinking", [None, False, True])
def test_ollama_receives_exact_generation_controls(monkeypatch, limit, thinking):
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return Response({"response": "ok", "done": True})

    monkeypatch.setattr(clients.requests, "post", post)
    result = clients.call_deployment(ollama(), "small", "hello",
                                     output_limit_tokens=limit, enable_thinking=thinking)
    assert sent[0]["model"] == "qwen:exact-tag"
    assert sent[0]["options"]["num_predict"] == limit
    if thinking is None:
        assert "think" not in sent[0]
        assert result["effective_thinking_mode"] == "provider-default"
    else:
        assert sent[0]["think"] is thinking
        assert result["effective_thinking_mode"] == ("enabled" if thinking else "disabled")
    assert result["output_limit_tokens"] == limit
    assert result["enable_thinking"] is thinking


@pytest.mark.parametrize("limit", [0, -1, 32769, True, 1.5, "42", None, float("nan"), float("inf")])
def test_invalid_output_limits_never_reach_provider(limit):
    with pytest.raises(ValueError, match="output_limit_tokens"):
        clients.call_deployment(ollama(), "small", "hello", output_limit_tokens=limit)


@pytest.mark.parametrize("thinking", [0, 1, "false", [], {}])
def test_invalid_thinking_controls_never_reach_provider(thinking):
    with pytest.raises(ValueError, match="enable_thinking"):
        clients.call_deployment(ollama(), "small", "hello", enable_thinking=thinking)


@pytest.mark.parametrize("provider", ["gemini", "nvidia"])
@pytest.mark.parametrize("thinking", [False, True])
def test_unsupported_cloud_thinking_setting_fails_preflight_without_silent_ignore(provider, thinking):
    deployment = Deployment("cloud", provider, "https://gateway.example",
                            {"model": "provider-exact-model"}, credential_env="UNNEEDED_KEY")
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(deployment, "model", "hello", enable_thinking=thinking)
    assert raised.value.error_code == "unsupported-generation-setting"
    assert raised.value.request_attempted is False
    assert raised.value.attempt_count == 0


def test_nvidia_receives_requested_limit_and_retains_provider_default_thinking(monkeypatch):
    sent = []
    deployment = Deployment("nim", "nvidia", "https://nim.example/v1/chat/completions",
                            {"model": "exact/model"}, credential_env="NIM_GENERATION_TEST_KEY")
    monkeypatch.setenv("NIM_GENERATION_TEST_KEY", "test-key")

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return Response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(clients.requests, "post", post)
    result = clients.call_deployment(deployment, "model", "hello", output_limit_tokens=19)
    assert sent[0]["max_tokens"] == 19
    assert result["effective_thinking_mode"] == "provider-default"


def test_gemini_receives_requested_limit_without_claiming_disabled_thinking(monkeypatch):
    sent = []

    class Client:
        def __init__(self, **kwargs):
            self.models = self

        def generate_content(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(text="ok")

        def close(self):
            pass

    monkeypatch.setattr(clients, "_load_genai", lambda: SimpleNamespace(Client=Client))
    monkeypatch.setenv("GEMINI_GENERATION_TEST_KEY", "test-key")
    deployment = Deployment("gemini", "gemini", "https://gateway.example",
                            {"model": "exact-model"}, credential_env="GEMINI_GENERATION_TEST_KEY")
    result = clients.call_deployment(deployment, "model", "hello", output_limit_tokens=37)
    assert sent[0]["config"]["max_output_tokens"] == 37
    assert "thinking_config" not in sent[0]["config"]
    assert result["effective_thinking_mode"] == "provider-default"


def test_ollama_model_rejecting_thinking_is_not_replaced_or_retried(monkeypatch):
    sent = []

    def post(url, **kwargs):
        sent.append((url, kwargs["json"]))
        return Response({"error": "model does not support thinking"}, status=400)

    monkeypatch.setattr(clients.requests, "post", post)
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(ollama(), "small", "hello", enable_thinking=True, max_attempts=3)
    assert raised.value.error_code == "http-request-rejected"
    assert raised.value.attempt_count == 1
    assert len(sent) == 1
    assert sent[0][1]["model"] == "qwen:exact-tag"
    assert sent[0][1]["think"] is True


@pytest.mark.parametrize("provider,reported,expected", [
    ("ollama", "stop", "stop"), ("ollama", "length", "length"),
    ("ollama", "unload", "unload"), ("ollama", None, None),
    ("nvidia", "stop", "stop"), ("nvidia", "length", "length"),
    ("nvidia", "content_filter", "content-filter"), ("nvidia", "tool_calls", "tool-calls"),
    ("gemini", "STOP", "stop"), ("gemini", "MAX_TOKENS", "length"),
    ("gemini", "SAFETY", "content-filter"),
    ("gemini", "MALFORMED_FUNCTION_CALL", "invalid-tool-call"),
    ("gemini", None, None),
])
def test_provider_completion_metadata_is_normalized_without_dropping_partial_output(monkeypatch, provider, reported, expected):
    deployment = Deployment("worker", provider, "https://worker.example",
                            {"model": "exact-model"}, credential_env="COMPLETION_TEST_KEY")
    monkeypatch.setenv("COMPLETION_TEST_KEY", "test-key")
    text = "Partial output remains evidence"
    if provider == "gemini":
        class Client:
            def __init__(self, **kwargs):
                self.models = self

            def generate_content(self, **kwargs):
                return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=reported)],
                                       usage_metadata=SimpleNamespace(candidates_token_count=7))

            def close(self):
                pass
        monkeypatch.setattr(clients, "_load_genai", lambda: SimpleNamespace(Client=Client))
    else:
        body = ({"response": text, "done": True, "done_reason": reported, "eval_count": 7}
                if provider == "ollama" else
                {"choices": [{"message": {"content": text}, "finish_reason": reported}],
                 "usage": {"completion_tokens": 7}})
        monkeypatch.setattr(clients.requests, "post", lambda *_a, **_k: Response(body))
    result = clients.call_deployment(deployment, "model", "hello", output_limit_tokens=7)
    assert result["completion_reason"] == expected
    assert result["text"] == text and result["output_tokens"] == 7


def test_gemini_sdk_finish_reason_enum_is_normalized(monkeypatch):
    from enum import Enum

    class FinishReason(str, Enum):
        MAX_TOKENS = "MAX_TOKENS"

    class Client:
        def __init__(self, **kwargs):
            self.models = self

        def generate_content(self, **kwargs):
            return SimpleNamespace(text="partial", candidates=[SimpleNamespace(finish_reason=FinishReason.MAX_TOKENS)])

        def close(self):
            pass

    monkeypatch.setattr(clients, "_load_genai", lambda: SimpleNamespace(Client=Client))
    monkeypatch.setenv("COMPLETION_TEST_KEY", "test-key")
    deployment = Deployment("worker", "gemini", "https://worker.example",
                            {"model": "exact-model"}, credential_env="COMPLETION_TEST_KEY")
    assert clients.call_deployment(deployment, "model", "hello")["completion_reason"] == "length"


@pytest.mark.parametrize("reported", [[], {}, 1, True, "secret provider message"])
def test_malformed_completion_metadata_has_safe_failure_provenance(monkeypatch, reported):
    monkeypatch.setattr(clients.requests, "post", lambda *_a, **_k: Response({
        "response": "partial", "done": True, "done_reason": reported,
    }))
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(ollama(), "small", "hello")
    assert raised.value.error_code == "invalid-completion-reason"
    assert raised.value.attempt_count == 1 and raised.value.request_attempted is True
    assert "secret" not in "".join(traceback.format_exception(raised.value))


def test_unrecognized_completion_token_is_not_persisted_as_provider_text(monkeypatch):
    monkeypatch.setattr(clients.requests, "post", lambda *_a, **_k: Response({
        "response": "ok", "done": True, "done_reason": "secret-provider-token",
    }))
    result = clients.call_deployment(ollama(), "small", "hello")
    assert result["completion_reason"] == "other"
    assert "secret-provider-token" not in str(result)
