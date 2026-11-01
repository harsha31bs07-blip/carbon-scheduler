"""Versioned experiment context shared by execution, replay, and presentation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

SCHEMA_VERSION = 2
ROOT = Path(__file__).resolve().parent.parent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def reference_time(value: str | datetime | None = None) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    result = result or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("Reference time must include a timezone")
    return result.astimezone(timezone.utc)


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Run/comparison IDs must contain only letters, digits, underscores or hyphens")
    return value


def workload_id(prompts: dict[str, str]) -> str:
    encoded = json.dumps(prompts, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "support-ticket-v1-" + hashlib.sha256(encoded).hexdigest()[:16]


def calibration_id() -> str:
    paths = ("data/model_table.py", "data/latency_model.py", "data/region_carbon.py",
             "data/time_curve.py", "data/candidate_builder.py", "data/eval/eval_results.json",
             "scheduler/data_adapter.py", "shared/schema.py")
    digest = hashlib.sha256()
    for name in paths:
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def code_revision() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True, timeout=5).stdout.strip()
        changes = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal",
                                  "--", "shared", "data", "scheduler", "workflow"],
                                 cwd=ROOT, capture_output=True, text=True, check=True, timeout=5)
        return head + ("-dirty" if changes.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass(frozen=True)
class RunContext:
    policy: str
    custom_weights: dict | None
    workload_id: str
    comparison_id: str | None
    provider_mode: str
    reference_utc_time: str
    calibration_id: str
    execution_mode: str
    code_revision: str

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


def write_json(path: Path, value: dict) -> None:
    """Replace a manifest atomically; interrupted runs keep their previous status."""
    payload = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)
