"""Additive local human judgments; raw calibration journals remain unchanged."""

from pathlib import Path
import sqlite3

from data.quality_reviews import apply_reviews, fingerprint
from workflow.queue_store import _dump, _load


def _connection(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS calibration_approvals(binding TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL CHECK(version=1))")
    return connection


def _binding(review):
    return fingerprint({key: review[key] for key in ("trial_id", "trial_sha256", "rubric_sha256")})


def save_approval(path, trials, rubric, review):
    apply_reviews(trials, [review], rubric=rubric)
    connection = _connection(path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT payload,version FROM calibration_approvals WHERE binding=?", (_binding(review),)).fetchone()
            if existing:
                if existing[1] != 1 or _load(existing[0]) != review:
                    raise ValueError("An existing approval cannot be overwritten")
                return
            connection.execute("INSERT INTO calibration_approvals VALUES (?,?,1)", (_binding(review), _dump(review)))
    finally:
        connection.close()


def load_approvals(path, trials, rubric):
    if not Path(path).exists():
        return []
    expected = {trial["trial_id"]: fingerprint(trial) for trial in trials}
    connection = _connection(path)
    try:
        result = []
        for payload, version in connection.execute("SELECT payload,version FROM calibration_approvals ORDER BY binding"):
            if version != 1:
                raise ValueError("Unsupported approval version")
            review = _load(payload)
            if (expected.get(review["trial_id"]) == review["trial_sha256"]
                    and review["rubric_sha256"] == fingerprint(rubric)):
                result.append(review)
        apply_reviews(trials, result, rubric=rubric)
        return result
    finally:
        connection.close()
