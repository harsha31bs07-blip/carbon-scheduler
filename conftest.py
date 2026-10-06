# Presence of this file makes pytest put the repo root on sys.path so `data` and `shared` import from anywhere.

import pytest

from shared import availability


@pytest.fixture(autouse=True)
def offline_by_default(monkeypatch):
    """Run every test in offline mode so results don't depend on the developer's .env."""
    monkeypatch.setattr(availability, "gemini_status", lambda: (False, "disabled in tests"))
    monkeypatch.setattr(availability, "nvidia_status", lambda: (False, "disabled in tests"))


@pytest.fixture
def hybrid(monkeypatch):
    """Call hybrid(gemini=..., nvidia=...) to switch cloud providers on for one test."""

    def enable(gemini: bool = True, nvidia: bool = False) -> None:
        monkeypatch.setattr(availability, "gemini_status", lambda: (gemini, "enabled in test"))
        monkeypatch.setattr(availability, "nvidia_status", lambda: (nvidia, "enabled in test"))

    return enable
