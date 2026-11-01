"""Conservative host-energy telemetry without changing inference outcomes.

CodeCarbon can combine sensor readings and power models. Its total is therefore
not proof of wholly measured energy. These reports retain that composition and
the machine scope, which includes the Ollama service, idle load and other work.
Remote inference energy cannot be measured at this coordinator.
"""

from __future__ import annotations

import ipaddress
import math
import threading
from typing import Callable
from urllib.parse import urlsplit


_TRACKER_LOCK = threading.Lock()
_COMPONENTS = {"cpu": "_total_cpu_energy", "gpu": "_total_gpu_energy",
               "ram": "_total_ram_energy"}
_CPU_SENSOR_MODES = frozenset({"intel_rapl", "intel_power_gadget", "windows_emi"})


def is_loopback_endpoint(endpoint: str) -> bool:
    """Recognize literal loopback hosts only; do not infer locality via DNS."""
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return False
        host = parsed.hostname
        if host == "localhost":
            return True
        return host is not None and ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _wh(value: object) -> float | None:
    """Read CodeCarbon's private energy counter defensively, in Wh."""
    number = getattr(value, "kWh", None)
    if isinstance(number, bool) or not isinstance(number, (float, int)):
        return None
    result = number * 1000.0
    return result if math.isfinite(result) and result >= 0 else None


def _unknown_components() -> dict:
    return {name: {"value_wh": None, "provenance": "unknown", "source": None}
            for name in _COMPONENTS}


def _tracker_report(tracker: object) -> dict | None:
    total = _wh(getattr(tracker, "_total_energy", None))
    if total is None:
        return None
    hardware = getattr(tracker, "_hardware", ())
    cpu_modes = {getattr(item, "_mode", None) for item in hardware
                 if type(item).__name__ == "CPU"}
    cpu_direct = bool(cpu_modes) and cpu_modes <= _CPU_SENSOR_MODES
    components = _unknown_components()
    for name, counter in _COMPONENTS.items():
        value = _wh(getattr(tracker, counter, None))
        if value is None:
            continue
        # A backend class name or the presence of PyNVML is insufficient to
        # prove successful GPU sensor readings for every included GPU.
        direct = name == "cpu" and cpu_direct
        source = ("codecarbon:cpu:" + ",".join(sorted(cpu_modes)) if direct else
                  "codecarbon:ram-power-model" if name == "ram" else
                  f"codecarbon:{name}:sensor-or-power-model-unverified")
        components[name] = {"value_wh": value,
                            "provenance": "measured" if direct else "estimated",
                            "source": source}
    # Even a tracker exposing only a total has unverified composition. Never
    # promote that counter to measured because telemetry is installed.
    provenance = ("mixed" if any(item["provenance"] == "measured"
                                 for item in components.values()) else "estimated")
    return {"energy_wh": total, "energy_measured": False,
            "energy_method": "CodeCarbon sensor/model composition (RAM modeled)",
            "energy_provenance": provenance, "energy_components": components,
            "energy_scope": "host-machine-including-idle-and-other-processes"}


class EnergySession:
    """A best-effort tracker whose failures never repeat or replace inference.

    Pass a CodeCarbon tracker constructor to enable tracking. ``None`` disables
    it. A configured fallback is an explicit power assumption and is only used
    for loopback endpoints. Concurrent or nested tracker sessions are declined
    to avoid attributing the same host energy more than once.
    """

    def __init__(self, endpoint: str, *, tracker_factory: Callable | None = None,
                 fallback_power_watts: float | None = None) -> None:
        if fallback_power_watts is not None and (
                isinstance(fallback_power_watts, bool)
                or not isinstance(fallback_power_watts, (int, float))
                or not math.isfinite(fallback_power_watts) or fallback_power_watts <= 0):
            raise ValueError("fallback_power_watts must be finite and positive")
        self.local = is_loopback_endpoint(endpoint)
        self.factory = tracker_factory
        self.fallback_power_watts = fallback_power_watts
        self.tracker = None
        self.telemetry_error = None
        self._owns_lock = False
        self._finished = False

    def __enter__(self) -> "EnergySession":
        if not self.local or self.factory is None:
            return self
        self._owns_lock = _TRACKER_LOCK.acquire(blocking=False)
        if not self._owns_lock:
            self.telemetry_error = "another-host-tracker-active"
            return self
        try:
            self.tracker = self.factory(
                tracking_mode="machine", measure_power_secs=1,
                save_to_file=False, save_to_api=False, log_level="error", pue=1.0,
            )
            self.tracker.start()
        except Exception:
            # Keep partially initialized trackers for cleanup on __exit__.
            self.telemetry_error = "tracker-start-failed"
        except BaseException:
            # __exit__ is not invoked automatically when __enter__ is
            # interrupted. Release a partial tracker and lock before reraising.
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if self.tracker is not None:
                try:
                    self.tracker.stop()
                except Exception:
                    self.telemetry_error = "tracker-stop-failed"
        finally:
            self._finished = True
            if self._owns_lock:
                _TRACKER_LOCK.release()
                self._owns_lock = False
        return False

    def report(self, latency_ms: float) -> dict:
        """Return JSON-safe telemetry, after cleanup, using explicit units."""
        if not self._finished:
            raise RuntimeError("EnergySession report requires completed context")
        if (isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float))
                or not math.isfinite(latency_ms) or latency_ms < 0):
            raise ValueError("latency_ms must be finite and nonnegative")
        report = None
        if self.local and self.tracker is not None and self.telemetry_error is None:
            try:
                report = _tracker_report(self.tracker)
            except Exception:
                self.telemetry_error = "tracker-reading-failed"
            if report is None and self.telemetry_error is None:
                self.telemetry_error = "tracker-reading-unavailable"
        if report is None:
            report = {"energy_wh": None, "energy_measured": False,
                      "energy_method": "unknown", "energy_provenance": "unknown",
                      "energy_components": _unknown_components(), "energy_scope": "unknown"}
            if self.local and self.fallback_power_watts is not None:
                watts = self.fallback_power_watts
                value = watts * (latency_ms / 3_600_000)
                if math.isfinite(value):
                    report.update(energy_wh=value,
                                  energy_method=f"TDP estimate ({watts:g}W × duration; uncalibrated assumption)",
                                  energy_provenance="estimated", energy_scope="assumed-inference-power")
                else:
                    self.telemetry_error = "fallback-overflow"
        return {**report, "telemetry_error": self.telemetry_error}


def aggregate_energy_measured(records: list[dict]) -> bool:
    """An aggregate is measured only when every included energy is measured."""
    def measured(item: dict) -> bool:
        value = item.get("energy_wh")
        if (item.get("energy_measured") is not True or isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
                or item.get("energy_provenance", "measured") != "measured"):
            return False
        components = item.get("energy_components", {})
        return all(component.get("provenance") == "measured"
                   for component in components.values())

    return bool(records) and all(measured(item) for item in records)
