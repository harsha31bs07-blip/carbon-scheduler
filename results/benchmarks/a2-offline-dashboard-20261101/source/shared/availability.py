"""Which cloud providers are usable right now.

Read from the environment on every call (not frozen at import), so tests and the
dashboard can switch between offline and hybrid mode. Lives in shared/ so the data
layer can ask without importing workflow/ (which would invert the layering).

No keys -> offline mode (local models only). Keys present -> hybrid mode.
"""

from __future__ import annotations

import importlib.util
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def gemini_status() -> tuple[bool, str]:
    if not os.environ.get("GOOGLE_API_KEY"):
        return False, "GOOGLE_API_KEY not set"
    try:
        sdk_installed = importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:  # parent package "google" missing
        sdk_installed = False
    if not sdk_installed:
        return False, "google-genai package not installed"
    return True, "API key present and SDK available"


def nvidia_status() -> tuple[bool, str]:
    # No free reachability check exists for NIM, so key + config presence is the signal.
    for var in ("NVIDIA_API_KEY", "NVIDIA_NIM_URL", "NVIDIA_NIM_MODEL"):
        if not os.environ.get(var):
            return False, f"{var} not set"
    return True, "API key and config present"


def gemini_available() -> bool:
    return gemini_status()[0]


def nvidia_available() -> bool:
    return nvidia_status()[0]


def mode_label() -> str:
    """Human-readable mode, e.g. 'Offline', 'Hybrid: Gemini', 'Hybrid: Gemini + NVIDIA'."""
    providers = [name for name, ok in (("Gemini", gemini_available()), ("NVIDIA", nvidia_available())) if ok]
    return f"Hybrid: {' + '.join(providers)}" if providers else "Offline"
