"""Local review drafts are separate from immutable approved judgments."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from data.quality_reviews import fingerprint
from workflow.queue_store import _dump, _load


class ReviewDraftStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS review_drafts(
            kind TEXT NOT NULL,binding TEXT NOT NULL,reviewer TEXT NOT NULL,
            fields TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER NOT NULL CHECK(version=1),
            PRIMARY KEY(kind,binding,reviewer))""")
        if [row[1] for row in self.connection.execute("PRAGMA table_info(review_drafts)")] != [
                "kind", "binding", "reviewer", "fields", "updated_at", "version"]:
            raise ValueError("Unsupported review draft schema")

    @staticmethod
    def identity(kind, template, reviewer):
        if kind not in {"workflow", "calibration"} or not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("Drafts require an explicit reviewer and kind")
        return kind, fingerprint(template), reviewer.strip()

    def save(self, kind, template, reviewer, fields):
        identity = self.identity(kind, template, reviewer)
        if not isinstance(fields, dict) or len(_dump(fields).encode()) > 1024 * 1024:
            raise ValueError("Invalid or oversized draft")
        # Drafts contain editable widget values only, never review status or approval metadata.
        if any(not isinstance(key, str) or type(value) not in {str, int, float, type(None)}
               for key, value in fields.items()):
            raise ValueError("Drafts must contain simple editable fields")
        with self.connection:
            self.connection.execute("INSERT INTO review_drafts VALUES (?,?,?,?,?,1) ON CONFLICT(kind,binding,reviewer) DO UPDATE SET fields=excluded.fields,updated_at=excluded.updated_at",
                (*identity, _dump(fields), datetime.now(timezone.utc).isoformat()))

    def load(self, kind, template, reviewer):
        row = self.connection.execute("SELECT fields,version FROM review_drafts WHERE kind=? AND binding=? AND reviewer=?",
                                      self.identity(kind, template, reviewer)).fetchone()
        if row is None:
            return None
        if row[1] != 1:
            raise ValueError("Unsupported draft version")
        return _load(row[0])

    def close(self):
        self.connection.close()
