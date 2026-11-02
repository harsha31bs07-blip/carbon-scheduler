"""Local configuration and commands shared by dashboard and diagnostics."""

import os
from pathlib import Path
import shlex
import sys


DEMO = "results/benchmarks/a2-offline-dashboard-20261101"
PROFILES = "results/benchmarks/m3-local-gated-20261026/profiles.json"


def local_paths(root: str | Path, *, registry_path: str | Path | None = None,
                database_path: str | Path | None = None,
                profiles_path: str | Path | None = None) -> dict[str, Path]:
    root = Path(root).resolve()
    def resolve(value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else root / path
    local = root / "config/deployments.local.json"
    registry = registry_path or os.getenv("CARBON_DEPLOYMENTS_FILE") or (
        local if local.exists() else "config/deployments.example.json")
    return {"root": root, "registry": resolve(registry),
            "database": resolve(database_path or os.getenv("CARBON_QUEUE_DATABASE") or ".runtime/queue.sqlite3"),
            "profiles": resolve(profiles_path or PROFILES), "demo": root / DEMO}


def startup_commands(root: str | Path, database: str | Path, *, executable: str | None = None,
                     powershell: bool | None = None) -> dict[str, str]:
    """Quote literal arguments for the displayed shell; never execute commands."""
    executable = executable or sys.executable
    powershell = os.name == "nt" if powershell is None else powershell
    root, database = Path(root).resolve(), Path(database).resolve()
    if powershell:
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        prefix = "Set-Location -LiteralPath " + quote(root) + "\n& "
        join = lambda args: " ".join(map(quote, args))
    else:
        prefix = "cd -- " + shlex.quote(str(root)) + "\n"
        join = lambda args: shlex.join([str(arg) for arg in args])
    return {"shell": "powershell" if powershell else "bash",
            "worker": prefix + join([executable, "-m", "workflow.worker", "--db", database,
                                      "run", "--idle-exit-seconds", "90"]),
            "dashboard": prefix + join([executable, "-m", "streamlit", "run",
                                         root / "workflow/dashboard/app.py",
                                         "--server.address", "127.0.0.1"])}
