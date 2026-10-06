import contextlib
import fcntl
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from schedulex import codex
from schedulex.cli import main
from schedulex.dashboard import dashboard, usage, worker_running
from schedulex.store import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def snapshot(self):
        return {"plan": "plus", "limits": {"rateLimitsByLimitId": {"codex": {
            "primary": {"windowDurationMins": 300, "usedPercent": 30, "resetsAt": 123},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 50, "resetsAt": 456}}},
            "rateLimitResetCredits": {"availableCount": 5, "credits": [
                {"id": "a", "status": "available", "title": "Full reset", "expiresAt": 789},
                {"id": "b", "status": "consumed", "title": "Used reset"}]}}}

    def test_remaining_reset_and_authoritative_bank_count(self):
        value = usage(self.snapshot())
        self.assertEqual(value["bankedResets"], 5)
        self.assertEqual(value["windows"][0]["remaining"], 70)
        self.assertEqual(value["windows"][0]["resetsAt"], 123)
        self.assertEqual(value["windows"][1]["title"], "Weekly")
        self.assertEqual(len(value["resetDetails"]), 1)
        self.assertEqual(value["resetDetails"][0]["expiresAt"], 789)

    def test_unknown_bank_and_limits_are_not_zero(self):
        value = usage({"plan": "plus", "limits": {"rateLimits": {"primary": {
            "usedPercent": None, "resetsAt": None, "windowDurationMins": None}}}})
        self.assertIsNone(value["bankedResets"])
        self.assertIsNone(value["windows"][0]["remaining"])

    def test_tasks_survive_usage_failure(self):
        prompt = "Full prompt\nsecond line"
        job = self.store.add({"prompt": prompt, "cwd": self.temp.name}, 123)
        with patch("schedulex.codex.executable", return_value="fake"), \
                patch("schedulex.codex.snapshot", side_effect=codex.CodexError("offline")):
            value = dashboard(self.store, "fake")
        self.assertEqual(value["jobs"][0]["id"], job)
        self.assertEqual(value["jobs"][0]["prompt"], prompt)
        self.assertEqual(value["usageError"], "offline")
        self.assertIsNone(value["usage"])

    def test_lock_is_live_worker_status(self):
        self.assertFalse(worker_running(self.store))
        with (self.store.root / "worker.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(worker_running(self.store))
        self.assertFalse(worker_running(self.store))

    def test_all_active_jobs_and_only_recent_terminal_history(self):
        for index in range(15):
            job = self.store.add({"prompt": f"old {index}", "cwd": self.temp.name}, index)
            self.store.finish(job, "succeeded")
        for index in range(20):
            self.store.add({"prompt": f"pending {index}", "cwd": self.temp.name}, index)
        with patch("schedulex.codex.executable", return_value="fake"), \
                patch("schedulex.codex.snapshot", return_value=self.snapshot()):
            value = dashboard(self.store, "fake")
        self.assertEqual(len(value["jobs"]), 30)
        self.assertEqual(sum(j["status"] == "pending" for j in value["jobs"]), 20)

    def test_cli_json_contract(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("schedulex.codex.executable", return_value="fake"), \
                patch("schedulex.codex.snapshot", return_value=self.snapshot()):
            main(["--state-dir", str(self.store.root), "dashboard"])
        self.assertEqual(json.loads(output.getvalue())["usage"]["bankedResets"], 5)


if __name__ == "__main__":
    unittest.main()
