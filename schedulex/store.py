import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "jobs.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, created REAL NOT NULL, due REAL NOT NULL,
            status TEXT NOT NULL, spec TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            started REAL, finished REAL, exit_code INTEGER, cloud_url TEXT
        )""")
        if "cloud_url" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN cloud_url TEXT")
        if "thread_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN thread_id TEXT")
        if "turn_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN turn_id TEXT")
        if "revision" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "previous_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN previous_id TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS next_occurrence ON jobs(previous_id)")
        self.db.commit()
        os.chmod(self.root / "jobs.sqlite3", 0o600)

    def add(self, spec, due):
        job_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO jobs(id,created,due,status,spec) VALUES(?,?,?,?,?)",
                            (job_id, time.time(), due, "pending", json.dumps(spec)))
        return job_id

    def remove_pending(self, job_id):
        with self.db:
            cursor = self.db.execute("DELETE FROM jobs WHERE id=? AND status='pending'", (job_id,))
        if not cursor.rowcount:
            raise ValueError(f"Could not roll back pending job {job_id}")

    def jobs(self, pending=False):
        query = "SELECT * FROM jobs"
        if pending:
            query += " WHERE status='pending'"
        return [dict(row) for row in self.db.execute(query + " ORDER BY due,created")]

    def get(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown job: {job_id}")
        return dict(row)

    def note(self, job_id, message):
        with self.db:
            self.db.execute("UPDATE jobs SET note=? WHERE id=?", (message, job_id))

    def update_pending(self, job_id, spec, due, revision, before_commit=None):
        """Compare and swap; hold the write lock through the wake update."""
        with self.db:
            cursor = self.db.execute(
                "UPDATE jobs SET spec=?,due=?,note='',revision=revision+1 "
                "WHERE id=? AND status='pending' AND revision=?",
                (json.dumps(spec), due, job_id, revision))
            if not cursor.rowcount:
                raise ValueError("Task changed or started running; refresh before editing it.")
            if before_commit:
                before_commit()

    def claim(self, job_id, revision=None):
        with self.db:
            cursor = self.db.execute(
                "UPDATE jobs SET status='running',started=?,note='' WHERE id=? AND status='pending' "
                "AND (? IS NULL OR revision=?)",
                (time.time(), job_id, revision, revision))
        return cursor.rowcount == 1

    def finish(self, job_id, status, code=None, note=""):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,exit_code=?,note=?,finished=? WHERE id=?",
                            (status, code, note, time.time(), job_id))
            if status == "succeeded":
                self._next_occurrence(job_id)
            elif status in ("failed", "interrupted") and json.loads(self.get(job_id)["spec"]).get("recurrence"):
                self.db.execute("UPDATE jobs SET note=note || ? WHERE id=?",
                                (" · Recurrence stopped; review this run before scheduling again", job_id))

    def _next_occurrence(self, job_id):
        row = self.get(job_id)
        spec = json.loads(row["spec"])
        if not spec.get("recurrence"):
            return
        spec["recurrence_after"] = max(time.time(), spec.get("reset_at") or 0)
        spec["reset_at"] = None
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(id,created,due,status,spec,note,previous_id) VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], time.time(), time.time(), "pending", json.dumps(spec),
             "Waiting for the next reported usage reset", job_id))

    def set_thread(self, job_id, thread_id):
        with self.db:
            self.db.execute("UPDATE jobs SET thread_id=? WHERE id=?", (thread_id, job_id))

    def set_turn(self, job_id, turn_id):
        with self.db:
            self.db.execute("UPDATE jobs SET turn_id=? WHERE id=?", (turn_id, job_id))

    def cloud_submitted(self, job_id, url, note):
        with self.db:
            self.db.execute("UPDATE jobs SET status='submitted',cloud_url=?,note=?,finished=? WHERE id=?",
                            (url, note, time.time(), job_id))
            self._next_occurrence(job_id)

    def cancel(self, job_id):
        self.get(job_id)
        with self.db:
            cursor = self.db.execute("UPDATE jobs SET status='cancelled',note='Cancelled by user' "
                                     "WHERE id=? AND status='pending'", (job_id,))
        if not cursor.rowcount:
            raise ValueError("Only pending jobs can be cancelled; stop the worker to interrupt a run.")
