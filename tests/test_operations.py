"""WAL-safe backups, isolated restores and non-authoritative worker observations."""

from datetime import timedelta
import json

import pytest

from workflow.operations import backup_queue, inventory, presence, restore_queue
from workflow.queue_store import QueueStore
from test_dashboard_submission import submit, NOW


def test_wal_backup_and_restore_preserve_workflows_and_sidecars(tmp_path):
    database = tmp_path / "live.sqlite3"
    store = QueueStore(database)
    try:
        identity = submit(store)
        original = store.read(identity)
        sidecar = tmp_path / "live.sqlite3.orphan-accounting"
        sidecar.mkdir()
        content = b'{"retained":"unreconciled fixture"}'
        (sidecar / "fixture.json").write_bytes(content)
        backup = tmp_path / "backup"
        manifest = backup_queue(database, backup)
        assert manifest["database_consistency"] == "sqlite-backup-api"
        result = restore_queue(backup, tmp_path / "restored")
        assert not result["worker_started"] and not result["inference_performed"]
        restored = QueueStore(result["database"])
        try:
            assert restored.read(identity) == original
        finally:
            restored.close()
        assert (tmp_path / "restored/queue.sqlite3.orphan-accounting/fixture.json").read_bytes() == content
        assert store.read(identity) == original
    finally:
        store.close()


def test_restore_rejects_changed_content_before_creating_output(tmp_path):
    store = QueueStore(tmp_path / "live.sqlite3")
    submit(store)
    backup = tmp_path / "backup"
    backup_queue(store.path, backup)
    store.close()
    path = backup / "queue.sqlite3"
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="checksum"):
        restore_queue(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_presence_reports_idle_running_stopped_and_stale_without_changing_leases(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    try:
        submit(store)
        claim = store.claim("one", now=NOW, lease_seconds=1)
        before = tuple(store.connection.execute("SELECT lease_token,lease_owner,lease_until FROM jobs WHERE step_id='classify'").fetchone())
        for state in ("idle", "running", "stopped"):
            store.worker_seen("one", state=state, now=NOW)
            assert store.presence_report(now=NOW)[0]["state"] == state
        assert presence(store.path, now=NOW + timedelta(seconds=31))[0]["state"] == "stale"
        after = tuple(store.connection.execute("SELECT lease_token,lease_owner,lease_until FROM jobs WHERE step_id='classify'").fetchone())
        assert before == after and before[0] == claim.lease_token
        report = inventory(store.path)
        assert report["workflow_count"] == 1 and not report["inference_performed"]
        assert report["retention_action"].startswith("inventory-only")
    finally:
        store.close()


def test_legacy_presence_is_unknown_without_migrating_readonly_database(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = QueueStore(path)
    store.connection.execute("DROP TABLE worker_presence")
    store.close()
    assert presence(path) == []
    import sqlite3
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='worker_presence'").fetchone()


def test_existing_backup_or_restore_directory_is_never_overwritten(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    submit(store)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    try:
        with pytest.raises(ValueError):
            backup_queue(store.path, target)
        with pytest.raises(ValueError):
            restore_queue(tmp_path / "missing", target)
        assert (target / "keep.txt").read_text() == "keep"
    finally:
        store.close()


def test_readonly_inventory_and_backup_support_fixed_queues_without_planning_extension(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = QueueStore(path)
    identity = submit(store)
    for table in ("planning_decisions", "planning_contexts", "planning_meta"):
        store.connection.execute("DROP TABLE " + table)
    store.close()
    assert inventory(path)["planning"]["retained_decisions"] == 0
    manifest = backup_queue(path, tmp_path / "backup")
    assert "queue.sqlite3" in manifest["files"]
