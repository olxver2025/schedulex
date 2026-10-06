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

    def claim(self, job_id):
        with self.db:
            cursor = self.db.execute(
                "UPDATE jobs SET status='running',started=?,note='' WHERE id=? AND status='pending'",
                (time.time(), job_id))
        return cursor.rowcount == 1

    def finish(self, job_id, status, code=None, note=""):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,exit_code=?,note=?,finished=? WHERE id=?",
                            (status, code, note, time.time(), job_id))

    def cloud_submitted(self, job_id, url, note):
        with self.db:
            self.db.execute("UPDATE jobs SET status='submitted',cloud_url=?,note=?,finished=? WHERE id=?",
                            (url, note, time.time(), job_id))

    def cancel(self, job_id):
        self.get(job_id)
        with self.db:
            cursor = self.db.execute("UPDATE jobs SET status='cancelled',note='Cancelled by user' "
                                     "WHERE id=? AND status='pending'", (job_id,))
        if not cursor.rowcount:
            raise ValueError("Only pending jobs can be cancelled; stop the worker to interrupt a run.")
