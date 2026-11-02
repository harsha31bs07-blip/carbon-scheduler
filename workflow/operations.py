"""Offline queue inventory, consistent SQLite backup and isolated restore."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import time

from data.quality_reviews import load_json
from workflow.queue_store import QueueStore, QUEUE_VERSION, _dump
from shared.scheduling import SCHEMA_VERSION


@contextmanager
def readonly_store(database):
    if Path(database).is_symlink():
        raise ValueError("Queue cannot follow a symlink")
    path = Path(database).resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("An existing regular queue file is required")
    store = QueueStore.__new__(QueueStore)
    store.path = path
    store.connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    store.connection.row_factory = sqlite3.Row
    store.connection.execute("PRAGMA query_only=ON")
    try:
        yield store
    finally:
        store.close()


def presence(database, *, now=None):
    if not Path(database).is_file():
        return []
    with readonly_store(database) as store:
        return store.presence_report(now=now or datetime.now(timezone.utc))


def inventory(database):
    """Dry-run inventory: no deletes, lease recovery, migrations or inference."""
    started = time.perf_counter()
    with readonly_store(database) as store:
        counts = dict(store.connection.execute("SELECT status,count(*) FROM jobs GROUP BY status"))
        workflows = store.connection.execute("SELECT count(*) FROM workflows").fetchone()[0]
        retries = store.connection.execute("SELECT coalesce(sum(retry_index),0) FROM jobs").fetchone()[0]
        sample = store.workflow_summaries(limit=20)
        sample_seconds = time.perf_counter() - started
        validation_started = time.perf_counter()
        for row in sample:
            store.read(row["workflow_id"])
        validation_seconds = time.perf_counter() - validation_started
        oldest = store.connection.execute("SELECT min(planned_start) FROM jobs WHERE status='scheduled'").fetchone()[0]
        accounting = store._has_accounting_tables()
        gaps = store.connection.execute("SELECT count(*) FROM attempts a LEFT JOIN attempt_accounting b ON a.attempt_id=b.attempt_id WHERE b.attempt_id IS NULL").fetchone()[0] if accounting else store.connection.execute("SELECT count(*) FROM attempts").fetchone()[0]
        workers = store.presence_report(now=datetime.now(timezone.utc))
        planning = store.connection.execute("SELECT count(*),sum(json_extract(report,'$.preparation_seconds')),sum(json_extract(report,'$.result.planner_seconds')) FROM planning_decisions").fetchone() if store._has_planning_tables() else (0, None, None)
    sidecars = {}
    for suffix in ("orphan-attempts", "orphan-accounting"):
        directory = Path(str(database) + "." + suffix)
        files = list(directory.glob("*.json")) if directory.is_dir() else []
        sidecars[suffix] = {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}
    return {"record_type": "QueueOperationsInventory", "workflow_count": workflows,
        "job_status_counts": counts, "explicit_retry_indices_total": retries,
        "oldest_scheduled_start": oldest, "attempts_without_accounting": gaps,
        "workers": workers, "sidecars": sidecars, "database_bytes": Path(database).stat().st_size,
        "planning": {"retained_decisions": planning[0], "preparation_seconds_sum": planning[1],
                     "search_seconds_sum": planning[2]},
        "profiling": {"summary_sample_count": len(sample), "summary_seconds": sample_seconds,
                      "snapshot_validation_seconds": validation_seconds},
        "retention_action": "inventory-only; no files deleted", "inference_performed": False}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _validate_database(path):
    with readonly_store(path) as store:
        versions = list(store.connection.execute("SELECT version,schema_version FROM queue_meta"))
        if len(versions) != 1 or tuple(versions[0]) != (QUEUE_VERSION, SCHEMA_VERSION):
            raise ValueError("Unsupported queue schema version")
        if store.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Queue integrity check failed")
        if store.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Queue foreign keys are inconsistent")
        for row in store.connection.execute("SELECT id FROM workflows"):
            store.read(row[0])


def backup_queue(database, output):
    """SQLite's backup API includes committed WAL contents; no raw DB file copy."""
    if Path(database).is_symlink() or Path(output).is_symlink():
        raise ValueError("Backup paths cannot follow symlinks")
    database, output = Path(database).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Backup output must be a new directory")
    if database.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("Database exceeds the local backup bound")
    if output == database.parent or output in database.parents:
        raise ValueError("Backup output cannot contain the live database")
    with readonly_store(database) as source:
        output.mkdir(parents=True, exist_ok=False)
        target = sqlite3.connect(output / "queue.sqlite3")
        try:
            source.connection.backup(target)
        finally:
            target.close()
    _validate_database(output / "queue.sqlite3")
    files = {"queue.sqlite3": _digest(output / "queue.sqlite3")}
    total = (output / "queue.sqlite3").stat().st_size
    for suffix in ("orphan-attempts", "orphan-accounting"):
        directory = Path(str(database) + "." + suffix)
        if directory.is_symlink():
            raise ValueError("Sidecars cannot follow symlinks")
        for path in sorted(directory.glob("*")) if directory.is_dir() else []:
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ValueError("Unsupported sidecar file")
            content = path.read_bytes()
            total += len(content)
            if total > 256 * 1024 * 1024 or len(files) > 4096:
                raise ValueError("Backup exceeds the local operator bound")
            relative = suffix + "/" + path.name
            target = output / relative
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(content)
            files[relative] = hashlib.sha256(content).hexdigest()
    manifest = {"schema_version": 1, "record_type": "QueueBackup", "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files, "database_consistency": "sqlite-backup-api",
        "sidecar_consistency": "individual captured files; stop workers before backup for an application-wide snapshot",
        "restore_executes_work": False}
    (output / "manifest.json").write_text(_dump(manifest), encoding="utf-8")
    return manifest


def restore_queue(backup, output):
    if Path(backup).is_symlink() or Path(output).is_symlink():
        raise ValueError("Restore paths cannot follow symlinks")
    backup, output = Path(backup).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Restore output must be a new isolated directory")
    manifest = load_json(backup / "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("record_type") != "QueueBackup" or "queue.sqlite3" not in manifest.get("files", {}):
        raise ValueError("Unsupported backup manifest")
    files, total = {}, 0
    for name, checksum in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in {"..", "."} or ":" in part or "\\" in part for part in relative.parts):
            raise ValueError("Unsafe backup path")
        if name != "queue.sqlite3" and not (len(relative.parts) == 2 and relative.parts[0] in {"orphan-attempts", "orphan-accounting"} and name.endswith(".json")):
            raise ValueError("Unsupported backup member")
        path = backup / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != backup.parent):
            raise ValueError("Backup cannot follow symlinks")
        size = path.stat().st_size
        total += size
        if total > 256 * 1024 * 1024 or len(files) >= 4096:
            raise ValueError("Backup exceeds the local operator bound")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError("Backup checksum mismatch")
        files[name] = content
    _validate_database(backup / "queue.sqlite3")
    output.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        relative = PurePosixPath(name)
        target = output / (name if name == "queue.sqlite3" else "queue.sqlite3." + relative.parts[0] + "/" + relative.parts[1])
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(content)
    _validate_database(output / "queue.sqlite3")
    return {"database": str(output / "queue.sqlite3"), "validated": True,
            "inference_performed": False, "worker_started": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    item = commands.add_parser("inventory")
    item.add_argument("--db", required=True)
    item = commands.add_parser("backup")
    item.add_argument("--db", required=True)
    item.add_argument("--output", required=True)
    item = commands.add_parser("restore")
    item.add_argument("--backup", required=True)
    item.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = (inventory(args.db) if args.command == "inventory" else backup_queue(args.db, args.output)
                  if args.command == "backup" else restore_queue(args.backup, args.output))
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
        print(json.dumps({"error_code": "invalid-queue-operation"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
