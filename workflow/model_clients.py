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
from typing import Any, Callable, Dict, Optional, Type
from functools import wraps

# Attempt to load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from shared import availability

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


def _is_transient_error(exc: Exception) -> bool:
    """Determine if an exception represents a transient error worth retrying."""
    err_msg = str(exc).lower()
    
    # Check for specific HTTP status codes in error message
    transient_indicators = [
        "timeout",
        "timed out",
        "429",
        "rate limit",
        "rate_limit",
        "too many requests",
        "500",
        "502",
        "503",
        "504",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "connection reset",
        "connection refused",
        "temporary failure",
    ]
    
    # Check for auth errors (should NOT retry)
    auth_indicators = [
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "invalid_api_key",
        "missing api key",
        "missing_api_key",
        "authentication failed",
        "permission denied",
        "api key not valid",
    ]
    
    # Auth errors take precedence - fail fast
    for indicator in auth_indicators:
        if indicator in err_msg:
            return False
    
    # Check for transient errors
    for indicator in transient_indicators:
        if indicator in err_msg:
            return True
    
    # Also check for specific exception types
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    
    # Check for requests library specific exceptions
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        if hasattr(exc, 'response') and exc.response is not None:
            status = exc.response.status_code
            if status == 429 or 500 <= status < 600:
                return True
            if status in (401, 403):
                return False
    
    # Check for google-genai specific errors
    if genai_errors is not None:
        if isinstance(exc, genai_errors.APIError):
            if hasattr(exc, 'code'):
                if exc.code == 429 or 500 <= exc.code < 600:
                    return True
                if exc.code in (401, 403):
                    return False
    
    return False


def with_retry(func: Callable) -> Callable:
    """
    Decorator that adds exponential backoff retry logic for transient errors.
    
    Retries up to MAX_RETRIES times with delays: 1s, 2s, 4s (capped at MAX_DELAY_SECONDS)
    Adds jitter to prevent thundering herd.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_exception = None
        
        for attempt in range(MAX_RETRIES + 1):  # +1 for initial attempt
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_exception = exc
                
                # Don't retry on auth errors - fail fast
                if isinstance(exc, AuthError) or (not _is_transient_error(exc) and attempt == 0):
                    if not _is_transient_error(exc):
                        raise AuthError(f"Authentication/authorization error: {exc}") from exc
                    raise
                
                # If this was the last attempt, re-raise
                if attempt >= MAX_RETRIES:
                    break
                
                # Calculate delay with exponential backoff and jitter
                delay = min(BASE_DELAY_SECONDS * (2 ** attempt), MAX_DELAY_SECONDS)
                jitter = random.uniform(0, delay * 0.1)
                total_delay = delay + jitter
                
                print(f"  [RETRY {attempt + 1}/{MAX_RETRIES}] {func.__name__} failed: {exc}. "
                      f"Retrying in {total_delay:.1f}s...")
                time.sleep(total_delay)
        
        # All retries exhausted
        raise RuntimeError(
            f"{func.__name__} failed after {MAX_RETRIES} retries. "
            f"Last error: {last_exception}"
        ) from last_exception
    
    return wrapper


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
    gemini_ok, gemini_reason = availability.gemini_status()
    if not gemini_ok:
        raise RuntimeError(
            f"Gemini model '{model_name_label}' is not available. Reason: {gemini_reason}. "
            f"Set GOOGLE_API_KEY environment variable to enable cloud models. "
            f"Available local models: qwen3.5:4b, llama3.2:3b"
        )
    
    if genai is None:
        raise ImportError(
            "The 'google-genai' package is not installed. "
            "Please install it with: pip install google-genai"
        )

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            f"Missing GOOGLE_API_KEY environment variable. "
            f"Required to call {model_name_label}."
        )

    client = genai.Client(api_key=api_key)
    start_time = time.perf_counter()

    try:
        response = client.models.generate_content(
            model=target_model_id,
            contents=prompt,
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        err_msg = str(exc)
        if "NOT_FOUND" in err_msg or "404" in err_msg:
            # Fallback to gemini-3-flash-preview (free tier available)
            fallback_id = "gemini-3-flash-preview"
            try:
                fallback_start = time.perf_counter()
                response = client.models.generate_content(
                    model=fallback_id,
                    contents=prompt,
                )
                latency_ms = round((time.perf_counter() - fallback_start) * 1000, 2)
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"Failed to call {model_name_label} (endpoint: {target_model_id}) "
                    f"and fallback ({fallback_id}) also failed: {fallback_exc}"
                ) from fallback_exc
        else:
            raise RuntimeError(
                f"Error calling {model_name_label} (endpoint: {target_model_id}) "
                f"after {latency_ms}ms: {exc}"
            ) from exc

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    text = response.text or ""

    input_tokens = 0
    output_tokens = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

    return {
        "text": text,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
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
    nvidia_ok, nvidia_reason = availability.nvidia_status()
    if not nvidia_ok:
        raise RuntimeError(
            f"NVIDIA NIM model 'nemotron-ultra-3' is not available. Reason: {nvidia_reason}. "
            f"Set NVIDIA_API_KEY, NVIDIA_NIM_URL, and NVIDIA_NIM_MODEL environment variables. "
            f"Available local models: qwen3.5:4b, llama3.2:3b"
        )
    
    # Check for missing configuration before attempting call (fail fast)
    if not os.environ.get("NVIDIA_API_KEY"):
        raise AuthError(
            "Missing NVIDIA_API_KEY environment variable. "
            "Required to call nemotron-ultra-3 via NVIDIA NIM API."
        )
    
    if not NVIDIA_NIM_URL:
        raise ValueError(
            "Missing NVIDIA_NIM_URL environment variable. "
            "Set this to your NVIDIA NIM endpoint URL from build.nvidia.com. "
            "Example: https://integrate.api.nvidia.com/v1/chat/completions"
        )
    
    if not NVIDIA_NIM_MODEL:
        raise ValueError(
            "Missing NVIDIA_NIM_MODEL environment variable. "
            "Set this to your NVIDIA model identifier from build.nvidia.com. "
            "Example: nvidia/llama-3.1-nemotron-70b-instruct or meta/llama-3.1-8b-instruct"
        )

    headers = {
        "Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": NVIDIA_NIM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 1024,
    }

    start_time = time.perf_counter()
    try:
        resp = requests.post(
            NVIDIA_NIM_URL,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        # Classify network errors for retry logic
        if isinstance(e, requests.exceptions.Timeout):
            raise TransientError(f"Request timeout after {latency_ms}ms: {e}") from e
        elif isinstance(e, requests.exceptions.ConnectionError):
            raise TransientError(f"Connection error: {e}") from e
        raise RuntimeError(
            f"Network request to NVIDIA NIM failed after {latency_ms}ms: {e}"
        ) from e

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    if resp.status_code != 200:
        # Parse error response for detailed message
        try:
            error_data = resp.json()
            error_detail = error_data.get("detail", resp.text)
        except Exception:
            error_detail = resp.text
        
        # Classify HTTP errors for retry logic
        if resp.status_code == 404:
            # 404 often means wrong model/function ID for this account
            raise RuntimeError(
                f"NVIDIA NIM API returned 404 Not Found ({latency_ms}ms).\n"
                f"Error detail: {error_detail}\n"
                f"This usually means the model/function is not available for your account.\n"
                f"Current settings:\n"
                f"  NVIDIA_NIM_URL={NVIDIA_NIM_URL}\n"
                f"  NVIDIA_NIM_MODEL={NVIDIA_NIM_MODEL}\n"
                f"Find the correct values at: https://build.nvidia.com/explore/discover"
            )
        elif resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise TransientError(
                f"NVIDIA NIM API returned HTTP status {resp.status_code} "
                f"({latency_ms}ms): {error_detail}"
            )
        elif resp.status_code in (401, 403):
            raise AuthError(
                f"NVIDIA NIM API authentication failed (HTTP {resp.status_code}): {error_detail}"
            )
        raise RuntimeError(
            f"NVIDIA NIM API returned HTTP status {resp.status_code} "
            f"({latency_ms}ms): {error_detail}"
        )

    try:
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise ValueError(f"No choices returned from NVIDIA NIM: {data}")
        text = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
    except Exception as parse_err:
        raise RuntimeError(
            f"Failed to parse NVIDIA NIM JSON response: {parse_err}"
        ) from parse_err

    return {
        "text": text,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
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
    """
    Wrapper for local LLM via Ollama (llama3.2:3b or qwen3.5:4b).
    Returns {text, latency_ms, input_tokens, output_tokens, model, energy_wh, energy_measured}.
    
    No API key required - connects to local Ollama instance.
    Cost: $0 (local inference, no API charges)
    
    Available models (both installed):
      - llama3.2:3b  (2.0 GB, Meta Llama architecture)
      - qwen3.5:4b   (3.4 GB, Qwen architecture, supports thinking mode)
    
    Args:
      enable_thinking: For qwen3.5:4b only - enable chain-of-thought reasoning.
                       Increases latency and token count. Recommended only for
                       complex generation tasks (draft), not for classify/summarize.
    
    Environment variables (optional):
      - OLLAMA_BASE_URL: Base URL for Ollama API (default: http://localhost:11434)
    
    Power Measurement:
      - If codecarbon is available: REAL measurement via GPU (pynvml) + CPU (Windows EMI/RAPL)
      - Fallback: TDP-based estimate (48W × duration) if sensors unavailable
    
    Raises:
      - ConnectionError if Ollama is not reachable
      - RuntimeError for other failures
    """
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 512,  # Max output tokens
        },
    }
    
    # Disable thinking mode for qwen3.5 (unless explicitly enabled)
    # This dramatically reduces latency for simple tasks (classify, summarize)
    if "qwen" in model_name.lower() and not enable_thinking:
        payload["think"] = False
    
    # Start power tracking if available
    tracker = None
    energy_wh = None
    energy_measured = False
    energy_method = "TDP estimate (48W)"
    
    if CODECARBON_AVAILABLE:
        try:
            tracker = EmissionsTracker(
                save_to_file=False,
                log_level='error',  # Quiet mode
                gpu_ids=[0] if os.name == 'nt' else None,  # Windows: use GPU 0
            )
            tracker.start()
        except Exception as e:
            # Graceful degradation: continue without tracking
            print(f"[WARNING] codecarbon tracker failed to start: {e}. Using TDP estimate.")
            tracker = None
    
    start_time = time.perf_counter()
    
    try:
        resp = requests.post(
            OLLAMA_GENERATE_ENDPOINT,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.ConnectionError as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        if tracker:
            try:
                tracker.stop()
            except:
                pass
        raise ConnectionError(
            f"Cannot connect to Ollama at {OLLAMA_BASE_URL}. "
            f"Ensure Ollama is running and the model '{model_name}' is installed. "
            f"Install with: ollama pull {model_name}"
        ) from e
    except requests.exceptions.Timeout as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        if tracker:
            try:
                tracker.stop()
            except:
                pass
        raise TimeoutError(
            f"Ollama request timed out after {latency_ms}ms. "
            f"The model may be loading or the prompt is too complex."
        ) from e
    except requests.RequestException as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        if tracker:
            try:
                tracker.stop()
            except:
                pass
        raise RuntimeError(
            f"Network request to Ollama failed after {latency_ms}ms: {e}"
        ) from e
    
    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    
    # Stop power tracking and get energy
    if tracker:
        try:
            emissions_kg = tracker.stop()
            energy_kwh = tracker._total_energy.kWh
            energy_wh = energy_kwh * 1000
            energy_measured = True
            energy_method = "codecarbon: GPU(pynvml)+CPU(Windows EMI)+RAM(est. 10W)"
        except Exception as e:
            print(f"[WARNING] codecarbon tracker failed to stop: {e}. Using TDP estimate.")
            energy_wh = None
    
    # Fallback to TDP estimate if measurement failed
    if energy_wh is None:
        # TDP-based estimate: 48W (GPU 60% of 80W TDP) × duration
        duration_hours = latency_ms / 1000 / 3600
        energy_wh = 48.0 * duration_hours
        energy_measured = False
        energy_method = "TDP estimate (48W × duration)"
    
    if resp.status_code != 200:
        try:
            error_data = resp.json()
            error_detail = error_data.get("error", resp.text)
        except Exception:
            error_detail = resp.text
        
        raise RuntimeError(
            f"Ollama API returned HTTP status {resp.status_code} "
            f"({latency_ms}ms): {error_detail}"
        )
    
    try:
        data = resp.json()
        text = data.get("response", "")
        
        # Ollama returns token counts in metadata
        # prompt_eval_count = input tokens processed
        # eval_count = output tokens generated
        input_tokens = data.get("prompt_eval_count", 0)
        output_tokens = data.get("eval_count", 0)
        
        # Note: Ollama also provides eval_duration (nanoseconds) for throughput calculation
        # but we only need wall-clock latency here (already measured)
        
    except Exception as parse_err:
        raise RuntimeError(
            f"Failed to parse Ollama JSON response: {parse_err}"
        ) from parse_err
    
    return {
        "text": text,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": f"local-{model_name}",  # Prefix to distinguish from cloud models
        "energy_wh": energy_wh,
        "energy_measured": energy_measured,
        "energy_method": energy_method,
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
