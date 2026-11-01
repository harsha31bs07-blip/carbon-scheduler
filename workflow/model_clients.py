"""
workflow/model_clients.py
─────────────────────────────────────────────────────────────────────────────
Real API client wrappers for the models used in the workflow pipeline.

HYBRID MODE (2026-09-22): Offline default, online when keys present
  LOCAL (always available, zero setup):
    - qwen3.5:4b    (Ollama local inference) — $0 cost, on-device
    - llama3.2:3b   (Ollama local inference) — $0 cost, on-device
  
  CLOUD (available when API keys detected):
    - gemini-3-flash   (Google GenAI SDK)        — requires GOOGLE_API_KEY
    - gemini-3-pro     (Google GenAI SDK)        — requires GOOGLE_API_KEY
    - nemotron-ultra-3 (NVIDIA NIM API endpoint) — requires NVIDIA_API_KEY + config

Cloud models are added ALONGSIDE local models when keys are present, not as replacements.
This restores genuine location diversity (on-device vs. cloud region) per the A2 problem statement.

Each wrapper captures real wall-clock timing (time.perf_counter) and actual
token usage reported by the respective API.

Environment variables:
  - OLLAMA_BASE_URL (optional, defaults to http://localhost:11434)
  - GOOGLE_API_KEY (optional, enables Gemini models)
  - NVIDIA_API_KEY, NVIDIA_NIM_URL, NVIDIA_NIM_MODEL (optional, enables NVIDIA NIM)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import time
import random
from typing import Any, Callable, Dict, Optional
from functools import wraps

# Attempt to load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from shared import availability
from shared.energy import EnergySession

try:
    import httpx
    _SDK_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.NetworkError)
except ImportError:
    _SDK_TRANSPORT_ERRORS = ()

try:
    from google import genai
    from google.genai import errors as genai_errors
except ImportError:
    genai = None
    genai_errors = None  # Handled gracefully in call_gemini functions

# Power measurement for local models
try:
    from codecarbon import EmissionsTracker
    CODECARBON_AVAILABLE = True
except ImportError:
    CODECARBON_AVAILABLE = False
    print("[WARNING] codecarbon not installed. Energy measurements will use TDP estimates. Install with: pip install codecarbon")


# ── Retry Configuration ────────────────────────────────────────────────────────
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 8.0
REQUEST_TIMEOUT_SECONDS = 20.0


class TransientError(Exception):
    """Raised for errors that should trigger a retry (timeout, 429, 5xx)."""
    pass


class AuthError(Exception):
    """Raised for authentication/authorization errors (401, 403, missing key)."""
    pass


def _http_status(exc: Exception) -> Optional[int]:
    """Read provider status metadata without interpreting arbitrary error text."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code
    if genai_errors is not None and isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        return code if isinstance(code, int) else None
    return None


def _is_transient_error(exc: Exception) -> bool:
    """Retry only transport failures, explicit transient errors, 429, and 5xx."""
    if isinstance(exc, AuthError):
        return False
    status = _http_status(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600
    return isinstance(exc, (
        TransientError, TimeoutError, ConnectionError,
        requests.exceptions.Timeout, requests.exceptions.ConnectionError,
    ) + _SDK_TRANSPORT_ERRORS)


def with_retry(func: Callable) -> Callable:
    """
    Decorator that adds exponential backoff retry logic for transient errors.
    
    Retries up to MAX_RETRIES times with delays: 1s, 2s, 4s (capped at MAX_DELAY_SECONDS)
    Adds jitter to prevent thundering herd.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES + 1):  # +1 for initial attempt
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, AuthError):
                    raise
                if _http_status(exc) in (401, 403):
                    raise AuthError(
                        f"{func.__name__} authentication failed (HTTP {_http_status(exc)})."
                    ) from exc
                # A permanent error must fail immediately, even after a transient one.
                # Keep configuration and programming errors in their original category.
                if not _is_transient_error(exc):
                    raise
                if attempt >= MAX_RETRIES:
                    raise RuntimeError(
                        f"{func.__name__} failed after {MAX_RETRIES} retries "
                        f"({type(exc).__name__})."
                    ) from exc
                
                # Calculate delay with exponential backoff and jitter
                delay = min(BASE_DELAY_SECONDS * (2 ** attempt), MAX_DELAY_SECONDS)
                jitter = random.uniform(0, delay * 0.1)
                total_delay = min(delay + jitter, MAX_DELAY_SECONDS)
                
                # Provider messages can echo request headers/keys; log only the type.
                print(f"  [RETRY {attempt + 1}/{MAX_RETRIES}] {func.__name__} failed "
                      f"({type(exc).__name__}). "
                      f"Retrying in {total_delay:.1f}s...")
                time.sleep(total_delay)
    
    return wrapper


def _token_usage(input_tokens: Optional[int], output_tokens: Optional[int]) -> dict:
    """Retain unavailable usage as unknown, never call default zeroes measurements."""
    for value in (input_tokens, output_tokens):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Provider token counts must be non-negative integers")
    reported = sum(value is not None for value in (input_tokens, output_tokens))
    source = {0: "unknown", 1: "partially_provider_reported", 2: "provider_reported"}[reported]
    return {"input_tokens": input_tokens if input_tokens is not None else 0,
            "output_tokens": output_tokens if output_tokens is not None else 0,
            "token_source": source}


# ── Model Endpoints Configuration ───────────────────────────────────────────
GEMINI_3_FLASH_MODEL = os.environ.get("GEMINI_3_FLASH_MODEL", "gemini-3-flash-preview")
GEMINI_3_PRO_MODEL = os.environ.get("GEMINI_3_PRO_MODEL", "gemini-3.1-pro-preview")

# NVIDIA NIM configuration — REQUIRED, no defaults (account-specific)
# Set these environment variables to match your build.nvidia.com account:
#   NVIDIA_NIM_URL: Full endpoint URL (e.g., https://integrate.api.nvidia.com/v1/chat/completions)
#   NVIDIA_NIM_MODEL: Model identifier (e.g., nvidia/llama-3.1-nemotron-70b-instruct or meta/llama-3.1-8b-instruct)
# Find the correct values at: https://build.nvidia.com/explore/discover → Select your model → View code → API endpoint
NVIDIA_NIM_URL = os.environ.get("NVIDIA_NIM_URL")
NVIDIA_NIM_MODEL = os.environ.get("NVIDIA_NIM_MODEL")


# ── Cloud Model Availability Detection ──────────────────────────────────────
# Detect which cloud providers are available based on API keys present.
# Cloud models are available ALONGSIDE local models when keys are valid.
# No keys = offline mode (local models only).

# Snapshot at import, kept for display code. Call paths re-check via shared.availability,
# so a key added or removed after import takes effect on the next call.
GEMINI_AVAILABLE, GEMINI_STATUS = availability.gemini_status()
NVIDIA_AVAILABLE, NVIDIA_STATUS = availability.nvidia_status()

# Backward compat: USE_CLOUD_MODELS is now derived from detection
USE_CLOUD_MODELS = GEMINI_AVAILABLE or NVIDIA_AVAILABLE


# ── Gemini Helpers (available when GOOGLE_API_KEY present) ─────────────────────
def _call_gemini_model(
    model_name_label: str,
    target_model_id: str,
    prompt: str,
    step_context: Optional[dict] = None,
) -> dict:
    """
    Helper to execute real Gemini API calls using google-genai SDK.
    
    HYBRID MODE: Available when GOOGLE_API_KEY is present, alongside local models.
    """
    if genai is None:
        raise ImportError(
            "The 'google-genai' package is not installed. "
            "Please install it with: pip install google-genai"
        )

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise AuthError(
            f"Missing GOOGLE_API_KEY environment variable. "
            f"Required to call {model_name_label}."
        )

    gemini_ok, gemini_reason = availability.gemini_status()
    if not gemini_ok:
        raise RuntimeError(
            f"Gemini model '{model_name_label}' is not available. Reason: {gemini_reason}."
        )

    client = genai.Client(api_key=api_key)
    start_time = time.perf_counter()

    try:
        response = client.models.generate_content(
            model=target_model_id,
            contents=prompt,
        )
    except Exception as exc:
        # Let the retry wrapper inspect structured SDK status/transport types.
        # Substituting a different model here would hide its identity from logs.
        if _is_transient_error(exc) or _http_status(exc) in (401, 403):
            raise
        if isinstance(exc, (ValueError, ImportError)):
            raise
        status = _http_status(exc)
        detail = f"HTTP {status}" if status is not None else type(exc).__name__
        raise RuntimeError(
            f"Error calling {model_name_label} (endpoint: {target_model_id}; {detail})."
        ) from exc

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    try:
        text = response.text or ""
        usage = getattr(response, "usage_metadata", None)
        tokens = _token_usage(getattr(usage, "prompt_token_count", None),
                              getattr(usage, "candidates_token_count", None))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Failed to parse {model_name_label} response ({type(exc).__name__})."
        ) from exc

    return {
        "text": text,
        "latency_ms": latency_ms,
        **tokens,
        "model": model_name_label,
    }


# ── Public Model Wrappers ────────────────────────────────────────────────────

@with_retry
def call_gemini_3_flash(prompt: str, step_context: Optional[dict] = None) -> dict:
    """
    Wrapper for gemini-3-flash (used for 'classify', 'draft', & 'summarize').
    Returns {text, latency_ms, input_tokens, output_tokens, model}.
    Includes retry with exponential backoff for transient errors.
    """
    # Check for missing API key before attempting call (fail fast)
    if not os.environ.get("GOOGLE_API_KEY"):
        raise AuthError("Missing GOOGLE_API_KEY environment variable. Required to call gemini-3-flash.")
    
    return _call_gemini_model(
        model_name_label="gemini-3-flash",
        target_model_id=GEMINI_3_FLASH_MODEL,
        prompt=prompt,
        step_context=step_context,
    )


@with_retry
def call_gemini_3_pro(prompt: str, step_context: Optional[dict] = None) -> dict:
    """
    Wrapper for gemini-3-pro (legacy, paid-only, no free tier).
    NOTE: No longer used in pipeline (draft step now uses gemini-3-flash).
    Returns {text, latency_ms, input_tokens, output_tokens, model}.
    Includes retry with exponential backoff for transient errors.
    """
    # Check for missing API key before attempting call (fail fast)
    if not os.environ.get("GOOGLE_API_KEY"):
        raise AuthError("Missing GOOGLE_API_KEY environment variable. Required to call gemini-3-pro.")
    
    return _call_gemini_model(
        model_name_label="gemini-3-pro",
        target_model_id=GEMINI_3_PRO_MODEL,
        prompt=prompt,
        step_context=step_context,
    )


@with_retry
def call_nemotron_ultra_3(prompt: str, step_context: Optional[dict] = None) -> dict:
    """
    Wrapper for nemotron-ultra-3 (used for 'retrieve') via NVIDIA NIM API.
    Returns {text, latency_ms, input_tokens, output_tokens, model}.
    Includes retry with exponential backoff for transient errors.
    
    HYBRID MODE: Available when NVIDIA_API_KEY + config are present, alongside local models.
    
    Required environment variables:
      - NVIDIA_API_KEY: Your NVIDIA API key
      - NVIDIA_NIM_URL: Full endpoint URL (from build.nvidia.com)
      - NVIDIA_NIM_MODEL: Model identifier (from build.nvidia.com)
    """
    # Check for missing configuration before attempting call (fail fast)
    if not os.environ.get("NVIDIA_API_KEY"):
        raise AuthError(
            "Missing NVIDIA_API_KEY environment variable. "
            "Required to call nemotron-ultra-3 via NVIDIA NIM API."
        )
    
    nvidia_nim_url = os.environ.get("NVIDIA_NIM_URL")
    nvidia_nim_model = os.environ.get("NVIDIA_NIM_MODEL")
    if not nvidia_nim_url:
        raise ValueError(
            "Missing NVIDIA_NIM_URL environment variable. "
            "Set this to your NVIDIA NIM endpoint URL from build.nvidia.com. "
            "Example: https://integrate.api.nvidia.com/v1/chat/completions"
        )
    
    if not nvidia_nim_model:
        raise ValueError(
            "Missing NVIDIA_NIM_MODEL environment variable. "
            "Set this to your NVIDIA model identifier from build.nvidia.com. "
            "Example: nvidia/llama-3.1-nemotron-70b-instruct or meta/llama-3.1-8b-instruct"
        )

    nvidia_ok, nvidia_reason = availability.nvidia_status()
    if not nvidia_ok:
        raise RuntimeError(
            f"NVIDIA NIM model 'nemotron-ultra-3' is not available. Reason: {nvidia_reason}."
        )

    headers = {
        "Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": nvidia_nim_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 1024,
    }

    start_time = time.perf_counter()
    try:
        resp = requests.post(
            nvidia_nim_url,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
            requests.exceptions.MissingSchema) as exc:
        raise ValueError("Invalid NVIDIA_NIM_URL configuration.") from exc
    except requests.RequestException as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        # Classify network errors for retry logic
        if isinstance(e, requests.exceptions.Timeout):
            raise TransientError(f"NVIDIA NIM request timed out after {latency_ms}ms.") from e
        elif isinstance(e, requests.exceptions.ConnectionError):
            raise TransientError("NVIDIA NIM connection failed.") from e
        raise RuntimeError(
            f"Network request to NVIDIA NIM failed after {latency_ms}ms ({type(e).__name__})."
        ) from e

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    if resp.status_code != 200:
        # Classify HTTP errors for retry logic
        if resp.status_code == 404:
            # 404 often means wrong model/function ID for this account
            raise RuntimeError(
                f"NVIDIA NIM API returned 404 Not Found ({latency_ms}ms).\n"
                f"This usually means the model/function is not available for your account.\n"
                f"Check NVIDIA_NIM_URL and NVIDIA_NIM_MODEL.\n"
                f"Find the correct values at: https://build.nvidia.com/explore/discover"
            )
        elif resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise TransientError(
                f"NVIDIA NIM API returned HTTP status {resp.status_code} "
                f"({latency_ms}ms)."
            )
        elif resp.status_code in (401, 403):
            raise AuthError(
                f"NVIDIA NIM API authentication failed (HTTP {resp.status_code})."
            )
        raise RuntimeError(
            f"NVIDIA NIM API returned HTTP status {resp.status_code} "
            f"({latency_ms}ms)."
        )

    try:
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("No choices returned from NVIDIA NIM.")
        text = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage") or {}
        tokens = _token_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
    except Exception as parse_err:
        raise RuntimeError(
            f"Failed to parse NVIDIA NIM JSON response ({type(parse_err).__name__})."
        ) from parse_err

    return {
        "text": text,
        "latency_ms": latency_ms,
        **tokens,
        "model": "nemotron-ultra-3",
    }


# ── Local LLM (Ollama) ───────────────────────────────────────────────────────
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_GENERATE_ENDPOINT = f"{OLLAMA_BASE_URL}/api/generate"


def call_local_llm(
    prompt: str,
    step_context: Optional[dict] = None,
    model_name: str = "llama3.2:3b",
    enable_thinking: bool = False,
) -> dict:
    """Call Ollama once and retain conservative host-energy provenance.

    CodeCarbon machine scope includes Ollama, idle energy and other processes;
    RAM and unverified sensor components remain modeled. If unavailable, the
    legacy 48W power assumption is explicitly estimated and uncalibrated. A
    remote endpoint has unknown energy, since coordinator power is not inference
    host power. Telemetry errors never repeat a provider request.
    """
    payload = {
        "model": model_name, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2, "num_predict": 512},
    }
    if "qwen" in model_name.lower() and not enable_thinking:
        payload["think"] = False

    telemetry = EnergySession(
        OLLAMA_GENERATE_ENDPOINT,
        tracker_factory=EmissionsTracker if CODECARBON_AVAILABLE else None,
        fallback_power_watts=48.0,
    )
    with telemetry:
        start_time = time.perf_counter()
        response = None
        try:
            try:
                response = requests.post(
                    OLLAMA_GENERATE_ENDPOINT, json=payload,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                    requests.exceptions.MissingSchema) as exc:
                raise ValueError("Invalid OLLAMA_BASE_URL configuration.") from exc
            except requests.exceptions.Timeout as exc:
                raise TimeoutError("Ollama request timed out.") from exc
            except requests.exceptions.ConnectionError as exc:
                raise ConnectionError(
                    "Cannot connect to the configured Ollama endpoint. Ensure Ollama is running."
                ) from exc
            except requests.RequestException as exc:
                raise RuntimeError("Network request to Ollama failed.") from exc

            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            if response.status_code in (401, 403):
                raise AuthError(
                    f"Ollama endpoint authentication failed (HTTP {response.status_code})."
                )
            if response.status_code != 200:
                # Provider bodies may echo credentials, prompts or other secrets.
                raise RuntimeError(
                    f"Ollama API returned HTTP status {response.status_code} ({latency_ms}ms)."
                )
            try:
                data = response.json()
                text = data.get("response", "")
                tokens = _token_usage(data.get("prompt_eval_count"), data.get("eval_count"))
            except Exception as exc:
                raise RuntimeError("Failed to parse Ollama JSON response.") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    return {
        "text": text, "latency_ms": latency_ms, **tokens,
        "model": f"local-{model_name}", **telemetry.report(latency_ms),
    }


# Helper wrappers for specific local models
def call_qwen_3_5_4b(
    prompt: str, 
    step_context: Optional[dict] = None,
    enable_thinking: bool = False,
) -> dict:
    """
    Wrapper for qwen3.5:4b local model.
    
    Args:
      enable_thinking: Enable chain-of-thought reasoning mode (slow, verbose).
                       Recommended: True for draft, False for classify/retrieve/summarize.
    """
    return call_local_llm(prompt, step_context, model_name="qwen3.5:4b", enable_thinking=enable_thinking)


def call_llama_3_2_3b(prompt: str, step_context: Optional[dict] = None) -> dict:
    """Wrapper for llama3.2:3b local model (no thinking mode available)."""
    return call_local_llm(prompt, step_context, model_name="llama3.2:3b")


# Convenience mapping & dispatcher
MODEL_REGISTRY: Dict[str, Callable[[str, Optional[dict]], dict]] = {
    # ── LOCAL MODELS (ACTIVE) ────────────────────────────────────────────────
    "qwen3.5:4b": call_qwen_3_5_4b,
    "llama3.2:3b": call_llama_3_2_3b,
    "local-qwen3.5": call_qwen_3_5_4b,
    "local-llama3.2": call_llama_3_2_3b,
    # Backward compat aliases
    "local_llama3.2": call_llama_3_2_3b,
    
    # ── CLOUD MODELS (available when API keys detected) ────────────────────────
    "gemini-3-flash": call_gemini_3_flash,
    "gemini-3-pro": call_gemini_3_pro,
    "nemotron-ultra-3": call_nemotron_ultra_3,
    "gemini_3_flash": call_gemini_3_flash,
    "gemini_3_pro": call_gemini_3_pro,
    "nemotron_ultra_3": call_nemotron_ultra_3,
}


def call_model(model_name: str, prompt: str, step_context: Optional[dict] = None) -> dict:
    """Dispatches call to appropriate model wrapper based on model string."""
    normalized_name = model_name.strip().lower()
    fn = MODEL_REGISTRY.get(normalized_name)
    if not fn:
        raise ValueError(
            f"Unknown model '{model_name}'. Available models: {list(MODEL_REGISTRY.keys())}"
        )
    return fn(prompt, step_context)


# ── Smoke Test ───────────────────────────────────────────────────────────────
def smoke_test() -> dict[str, Any]:
    """
    Calls both local models with trivial dummy prompts to verify
    connectivity, wall-clock timing, and token counting.
    
    HYBRID MODE: Tests qwen3.5:4b and llama3.2:3b via Ollama (always available).
    Cloud models (gemini, nemotron) tested if API keys detected.
    """
    dummy_prompt = "Say 'OK' in one word."
    results: dict[str, Any] = {}

    print("=" * 60)
    print("SMOKE TEST: Verifying Model Clients")
    print(f"  Mode: {'HYBRID' if (GEMINI_AVAILABLE or NVIDIA_AVAILABLE) else 'OFFLINE ONLY'}")
    print(f"  Gemini: {'AVAILABLE' if GEMINI_AVAILABLE else 'UNAVAILABLE'} ({GEMINI_STATUS})")
    print(f"  NVIDIA: {'AVAILABLE' if NVIDIA_AVAILABLE else 'UNAVAILABLE'} ({NVIDIA_STATUS})")
    print("=" * 60)

    # 1. Check qwen3.5:4b (LOCAL)
    print("\n[1/2] Testing qwen3.5:4b (Ollama local inference)...")
    try:
        res_qwen = call_qwen_3_5_4b(dummy_prompt)
        results["qwen3.5:4b"] = {"status": "SUCCESS", "data": res_qwen}
        print(f"  [SUCCESS] | Latency: {res_qwen['latency_ms']}ms | "
              f"Tokens: in={res_qwen['input_tokens']}, out={res_qwen['output_tokens']} | "
              f"Response: {res_qwen['text'].strip()[:60]!r}")
    except Exception as e:
        results["qwen3.5:4b"] = {"status": "FAILED", "error": str(e)}
        print(f"  [FAILED]: {e}")

    # 2. Check llama3.2:3b (LOCAL)
    print("\n[2/2] Testing llama3.2:3b (Ollama local inference)...")
    try:
        res_llama = call_llama_3_2_3b(dummy_prompt)
        results["llama3.2:3b"] = {"status": "SUCCESS", "data": res_llama}
        print(f"  [SUCCESS] | Latency: {res_llama['latency_ms']}ms | "
              f"Tokens: in={res_llama['input_tokens']}, out={res_llama['output_tokens']} | "
              f"Response: {res_llama['text'].strip()[:60]!r}")
    except Exception as e:
        results["llama3.2:3b"] = {"status": "FAILED", "error": str(e)}
        print(f"  [FAILED]: {e}")

    # Optional: Test cloud models if available
    if GEMINI_AVAILABLE:
        print("\n[CLOUD] Testing gemini-3-flash...")
        try:
            res_flash = call_gemini_3_flash(dummy_prompt)
            results["gemini-3-flash"] = {"status": "SUCCESS", "data": res_flash}
            print(f"  [SUCCESS] | Latency: {res_flash['latency_ms']}ms")
        except Exception as e:
            results["gemini-3-flash"] = {"status": "FAILED", "error": str(e)}
            print(f"  [FAILED]: {e}")
    else:
        print(f"\n[CLOUD] Gemini skipped: {GEMINI_STATUS}")

    if NVIDIA_AVAILABLE:
        print("\n[CLOUD] Testing nemotron-ultra-3...")
        try:
            res_nemotron = call_nemotron_ultra_3(dummy_prompt)
            results["nemotron-ultra-3"] = {"status": "SUCCESS", "data": res_nemotron}
            print(f"  [SUCCESS] | Latency: {res_nemotron['latency_ms']}ms")
        except Exception as e:
            results["nemotron-ultra-3"] = {"status": "FAILED", "error": str(e)}
            print(f"  [FAILED]: {e}")
    else:
        print(f"\n[CLOUD] NVIDIA NIM skipped: {NVIDIA_STATUS}")

    print("\n" + "=" * 60)
    print("Smoke Test Complete.")
    print("=" * 60)
    return results


if __name__ == "__main__":
    smoke_test()
