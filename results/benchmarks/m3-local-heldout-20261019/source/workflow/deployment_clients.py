"""Inference adapters for explicitly selected deployments.

The adapter never chooses another endpoint or model. It resolves credentials at
call time and keeps provider error bodies and credentials out of exceptions.
Energy at the inference host is unknown until host-side telemetry is integrated.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import os
import time

import requests

from shared.deployments import Deployment
from shared.scheduling import MAX_OUTPUT_LIMIT_TOKENS


class DeploymentCallError(RuntimeError):
    """Safe transport failure with a machine-readable classification."""

    retryable = False

    def __init__(self, error_code: str, *, attempt_count: int = 0,
                 request_attempted: bool = False) -> None:
        self.error_code = error_code
        self.attempt_count = attempt_count
        self.request_attempted = request_attempted
        super().__init__(f"Deployment call failed ({error_code}).")


class DeploymentAuthError(DeploymentCallError):
    """Missing credentials or a provider authentication/authorization rejection."""


class DeploymentTransientError(DeploymentCallError):
    """A timeout, connection failure, rate limit, or server failure."""

    retryable = True


def _validate_timeout(value: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 300):
        raise ValueError("timeout_seconds must be finite and between 0 and 300")
    return float(value)


def _credential(deployment: Deployment) -> str | None:
    name = deployment.credential_env
    value = os.environ.get(name) if name else None
    # Bearer tokens must be visible ASCII without whitespace. Reject invalid
    # values before HTTP header serialization, which can raise native encoding
    # errors containing the credential instead of requests.RequestException.
    if value is not None and (not value or any(not 33 <= ord(char) <= 126 for char in value)):
        raise DeploymentAuthError("invalid-credential")
    if deployment.provider in {"gemini", "nvidia"} and value is None:
        raise DeploymentAuthError("missing-credential")
    if name is not None and value is None:
        raise DeploymentAuthError("missing-credential")
    return value


def _check_status(status: int) -> None:
    if status in (401, 403):
        raise DeploymentAuthError("authentication-rejected", request_attempted=True)
    if status == 429:
        raise DeploymentTransientError("rate-limited", request_attempted=True)
    if 500 <= status < 600:
        raise DeploymentTransientError("provider-unavailable", request_attempted=True)
    if not 200 <= status < 300:
        # Redirects are rejected too: authorization cannot silently move to a
        # different deployment, and registry identity remains the selected host.
        raise DeploymentCallError("http-request-rejected", request_attempted=True)


def _http_post(endpoint: str, payload: dict, key: str | None, timeout: float) -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = requests.post(
            endpoint, json=payload, headers=headers,
            timeout=(min(timeout, 5.0), timeout), allow_redirects=False,
        )
    except requests.exceptions.Timeout:
        raise DeploymentTransientError("request-timeout", request_attempted=True) from None
    except requests.exceptions.ConnectionError:
        raise DeploymentTransientError("connection-failed", request_attempted=True) from None
    except requests.exceptions.RequestException:
        raise DeploymentCallError("request-failed", request_attempted=True) from None
    try:
        _check_status(response.status_code)
        try:
            body = response.json()
        except (ValueError, TypeError):
            raise DeploymentCallError("invalid-response") from None
        if not isinstance(body, dict):
            raise DeploymentCallError("invalid-response")
        return body
    except DeploymentCallError as exc:
        exc.request_attempted = True
        raise
    finally:
        response.close()


def _tokens(input_count: object, output_count: object) -> dict:
    for value in (input_count, output_count):
        if value is not None and (type(value) is not int or value < 0):
            raise DeploymentCallError("invalid-token-usage")
    present = sum(value is not None for value in (input_count, output_count))
    return {"input_tokens": input_count, "output_tokens": output_count,
            "token_source": ("unknown", "partially_provider_reported", "provider_reported")[present]}


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise DeploymentCallError("invalid-response")
    return value


def _reported_model(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or any(char.isspace() for char in value):
        raise DeploymentCallError("invalid-model-identity")
    return value


def _completion_reason(value: object) -> str | None:
    """Normalize provider metadata without retaining arbitrary provider text.

    Missing metadata is unknown. Unrecognized tokens map to ``other``; neither
    implies natural completion, and token counts alone cannot establish it.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise DeploymentCallError("invalid-completion-reason")
    token = value.lower().replace("_", "-")
    if token in {"length", "max-tokens", "max-output-tokens"}:
        return "length"
    if token in {"safety", "recitation", "language", "blocklist", "prohibited-content", "spii",
                 "image-safety", "image-prohibited-content", "image-recitation", "content-filter"}:
        return "content-filter"
    if token in {"tool-calls", "function-call"}:
        return "tool-calls"
    if token in {"malformed-function-call", "unexpected-tool-call", "too-many-tool-calls", "invalid-tool-call"}:
        return "invalid-tool-call"
    if token in {"stop", "load", "unload", "cancelled", "error"}:
        return token
    return "other"


def _load_genai():
    try:
        from google import genai
    except ImportError:
        raise DeploymentCallError("gemini-sdk-unavailable") from None
    return genai


def _ollama_timings(body: dict) -> dict[str, float]:
    """Preserve provider-reported nanosecond durations in explicit seconds."""
    result = {}
    for provider_field, name in (("load_duration", "load"), ("total_duration", "total"),
                                  ("prompt_eval_duration", "prefill"), ("eval_duration", "decode")):
        value = body.get(provider_field)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            raise DeploymentCallError("invalid-provider-timing", request_attempted=True)
        try:
            seconds = value / 1_000_000_000
        except OverflowError:
            raise DeploymentCallError("invalid-provider-timing", request_attempted=True) from None
        if not math.isfinite(seconds):
            raise DeploymentCallError("invalid-provider-timing", request_attempted=True)
        result[name] = seconds
    return result


def _call_gemini(deployment: Deployment, model: str, prompt: str,
                 key: str, timeout: float, output_limit_tokens: int) -> dict:
    genai = _load_genai()
    client = None
    request_attempted = False
    try:
        # SDK timeouts are milliseconds. Disable its internal retries so the
        # outer attempt limit records every request and remains authoritative.
        client = genai.Client(
            vertexai=False, api_key=key,
            http_options={"base_url": deployment.endpoint,
                          "timeout": max(1, int(timeout * 1000)),
                          "retry_options": {"attempts": 1},
                          "client_args": {"follow_redirects": False}},
        )
        request_attempted = True
        response = client.models.generate_content(
            model=model, contents=prompt,
            config={"temperature": 0.2, "max_output_tokens": output_limit_tokens},
        )
        usage = getattr(response, "usage_metadata", None)
        candidates = getattr(response, "candidates", None)
        reason = getattr(candidates[0], "finish_reason", None) if candidates else None
        return {"text": _text(response.text),
                "reported_model_id": _reported_model(getattr(response, "model_version", None)),
                "completion_reason": _completion_reason(reason),
                **_tokens(getattr(usage, "prompt_token_count", None),
                          getattr(usage, "candidates_token_count", None))}
    except DeploymentCallError as exc:
        exc.request_attempted = request_attempted or exc.request_attempted
        raise
    except Exception as exc:
        # Read status metadata only. Provider text may contain credentials.
        status = getattr(exc, "code", None)
        if type(status) is int:
            _check_status(status)
        transport_types = (TimeoutError, ConnectionError)
        try:
            import httpx
            transport_types += (httpx.TimeoutException, httpx.NetworkError)
        except ImportError:
            pass
        try:
            import httpx2
            transport_types += (httpx2.TimeoutException, httpx2.NetworkError)
        except ImportError:
            pass
        if isinstance(exc, transport_types):
            raise DeploymentTransientError("transport-failed", request_attempted=request_attempted) from None
        raise DeploymentCallError("gemini-request-failed", request_attempted=request_attempted) from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                # Cleanup must not replace the safe result or safe primary error.
                pass


def _execute_once(deployment: Deployment, model: str, prompt: str,
                  key: str | None, timeout: float, output_limit_tokens: int,
                  enable_thinking: bool | None) -> dict:
    if deployment.provider == "gemini":
        return _call_gemini(deployment, model, prompt, key, timeout, output_limit_tokens)
    if deployment.provider == "ollama":
        payload = {"model": model, "prompt": prompt, "stream": False,
                   "options": {"temperature": 0.2, "num_predict": output_limit_tokens}}
        if enable_thinking is not None:
            payload["think"] = enable_thinking
        body = _http_post(
            deployment.endpoint.rstrip("/") + "/api/generate",
            payload, key, timeout,
        )
        try:
            if "error" in body:
                raise DeploymentCallError("provider-rejected-request")
            if body.get("done") is False:
                raise DeploymentCallError("incomplete-response")
            if body.get("done") is not True:
                raise DeploymentCallError("invalid-response")
            return {"text": _text(body.get("response")),
                    "reported_model_id": _reported_model(body.get("model")),
                    "provider_timings_seconds": _ollama_timings(body),
                    "completion_reason": _completion_reason(body.get("done_reason")),
                    **_tokens(body.get("prompt_eval_count"), body.get("eval_count"))}
        except DeploymentCallError as exc:
            exc.request_attempted = True
            raise
    body = _http_post(
        deployment.endpoint,
        {"model": model, "messages": [{"role": "user", "content": prompt}],
         "temperature": 0.2, "max_tokens": output_limit_tokens}, key, timeout,
    )
    try:
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise DeploymentCallError("invalid-response") from None
        usage = body.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, Mapping):
            raise DeploymentCallError("invalid-token-usage")
        return {"text": _text(text), "reported_model_id": _reported_model(body.get("model")),
                "completion_reason": _completion_reason(choice.get("finish_reason")),
                **_tokens(usage.get("prompt_tokens"), usage.get("completion_tokens"))}
    except DeploymentCallError as exc:
        exc.request_attempted = True
        raise


def call_deployment(
    deployment: Deployment,
    canonical_model: str,
    prompt: str,
    *,
    timeout_seconds: float = 20.0,
    max_attempts: int = 1,
    output_limit_tokens: int = 1024,
    enable_thinking: bool | None = None,
) -> dict:
    """Call exactly the configured endpoint/model, with no implicit fallback.

    Optional retries (at most three attempts) repeat this same pair only. Each
    request has bounded connection and response inactivity timeouts; this is not
    a total workflow deadline. Deadline enforcement belongs to the scheduler.
    No host energy is inferred from coordinator power or elapsed time.

    ``enable_thinking=None`` retains the selected model's provider default.
    Explicit booleans are supported by Ollama's native ``think`` option; an
    unsupported model is rejected by that provider without changing the model.
    Cloud thinking controls fail preflight until their model-specific semantics
    are supported. The output limit is passed to the provider without adjustment.
    """
    if not isinstance(deployment, Deployment):
        raise ValueError("deployment must be a Deployment")
    if not isinstance(canonical_model, str):
        raise ValueError("canonical_model must be text")
    model = deployment.provider_model(canonical_model)
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be nonempty text")
    timeout = _validate_timeout(timeout_seconds)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3")
    if type(output_limit_tokens) is not int or not 1 <= output_limit_tokens <= MAX_OUTPUT_LIMIT_TOKENS:
        raise ValueError(f"output_limit_tokens must be an integer between 1 and {MAX_OUTPUT_LIMIT_TOKENS}")
    if enable_thinking is not None and type(enable_thinking) is not bool:
        raise ValueError("enable_thinking must be a boolean or None for provider default")
    if enable_thinking is not None and deployment.provider != "ollama":
        raise DeploymentCallError("unsupported-generation-setting")
    key = _credential(deployment)
    started = time.perf_counter()
    requests_attempted = 0
    for attempt in range(1, max_attempts + 1):
        try:
            result = _execute_once(deployment, model, prompt, key, timeout,
                                   output_limit_tokens, enable_thinking)
            return {"deployment_id": deployment.deployment_id,
                    "provider": deployment.provider, "endpoint": deployment.endpoint,
                    "model_id": canonical_model, "provider_model_id": model,
                    "region": deployment.region, "grid": deployment.grid,
                    "location_source": deployment.location_source,
                    "output_limit_tokens": output_limit_tokens,
                    "enable_thinking": enable_thinking,
                    "effective_thinking_mode": ("provider-default" if enable_thinking is None
                                                else "enabled" if enable_thinking else "disabled"),
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "energy_wh": None, "energy_measured": False, "energy_method": "unknown",
                    "carbon_g": None, "attempt_count": requests_attempted + 1, **result}
        except DeploymentCallError as exc:
            requests_attempted += int(exc.request_attempted)
            exc.attempt_count = requests_attempted
            exc.request_attempted = requests_attempted > 0
            if not exc.retryable or attempt == max_attempts:
                raise exc from None
            time.sleep(min(0.5 * (2 ** (attempt - 1)), 1.0))
    raise AssertionError("unreachable")
