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


def project_command(root: str | Path, arguments: list[str | Path], *, executable: str | None = None,
                    powershell: bool | None = None) -> tuple[str, str]:
    """Quote literal arguments for the displayed shell; never execute commands."""
    executable = executable or sys.executable
    powershell = os.name == "nt" if powershell is None else powershell
    root = Path(root).resolve()
    if powershell:
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        prefix = "Set-Location -LiteralPath " + quote(root) + "\n& "
        join = lambda args: " ".join(map(quote, args))
    else:
        prefix = "cd -- " + shlex.quote(str(root)) + "\n"
        join = lambda args: shlex.join([str(arg) for arg in args])
    return ("powershell" if powershell else "bash", prefix + join([executable, *arguments]))


def worker_action_command(root: str | Path, database: str | Path, *arguments: str) -> tuple[str, str]:
    return project_command(root, ["-m", "workflow.worker", "--db", Path(database).resolve(), *arguments])


def startup_commands(root: str | Path, database: str | Path, *, executable: str | None = None,
                     powershell: bool | None = None) -> dict[str, str]:
    root, database = Path(root).resolve(), Path(database).resolve()
    shell, worker = project_command(root, ["-m", "workflow.worker", "--db", database,
        "run", "--idle-exit-seconds", "90"], executable=executable, powershell=powershell)
    _, dashboard = project_command(root, ["-m", "streamlit", "run", root / "workflow/dashboard/app.py",
        "--server.address", "127.0.0.1"], executable=executable, powershell=powershell)
    return {"shell": shell, "worker": worker, "dashboard": dashboard}
