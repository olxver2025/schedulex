import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from schedulex import codex
from schedulex.cli import parser, save_job
from schedulex.store import Store
from schedulex.worker import tick


class WeeklyResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.snapshot = {"limits": {"rateLimitsByLimitId": {
            "codex": {"primary": {"windowDurationMins": 300, "resetsAt": 2000, "usedPercent": 0},
                      "secondary": {"windowDurationMins": 10080, "resetsAt": 9000, "usedPercent": 0}},
            "other": {"primary": {"windowDurationMins": 10080, "resetsAt": 12000, "usedPercent": 0}}
        }}}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def save(self, *args):
        parsed = parser().parse_args(list(args))
        with patch("schedulex.codex.executable", return_value="fixture"), \
                patch("schedulex.codex.snapshot", return_value=self.snapshot), \
                patch("schedulex.cli.time.time", return_value=1000), \
                patch("schedulex.power.schedule_wake") as wake, \
                contextlib.redirect_stdout(io.StringIO()):
            save_job(parsed, self.store)
        return wake

    def test_weekly_lookup_matches_duration_in_either_slot_and_legacy(self):
        self.assertEqual(codex.usage_reset(self.snapshot, "codex", "weekly"), 9000)
        self.assertEqual(codex.usage_reset(self.snapshot, "other", "weekly"), 12000)
        legacy = {"limits": {"rateLimits": self.snapshot["limits"]["rateLimitsByLimitId"]["other"]}}
        self.assertEqual(codex.usage_reset(legacy, period="weekly"), 12000)

    def test_weekly_cli_saves_reported_reset_buffer_and_wake(self):
        wake = self.save("add", "full\nprompt", "--after-weekly-reset", "--project", str(self.root))
        row = self.store.jobs()[0]
        spec = json.loads(row["spec"])
        self.assertEqual(row["due"], 9030)
        self.assertEqual(spec["schedule"], "after-weekly-reset")
        self.assertIsNone(spec["recurrence"])
        self.assertEqual(spec["reset_at"], 9000)
        self.assertEqual(spec["prompt"], "full\nprompt")
        wake.assert_called_once_with(row["id"], 9030)

    def test_cloud_uses_selected_bucket_weekly_reset(self):
        self.save("add", "cloud prompt", "--after-weekly-reset", "--cloud-env", "env", "--limit-id", "other")
        row = self.store.jobs()[0]
        self.assertEqual(row["due"], 12030)
        self.assertEqual(json.loads(row["spec"])["destination"], "cloud")

    def test_edit_to_weekly_keeps_prompt_and_changes_due(self):
        self.save("add", "original prompt", "--after-reset", "--project", str(self.root))
        row = self.store.jobs()[0]
        self.save("edit", row["id"], "--after-weekly-reset")
        edited = self.store.get(row["id"])
        self.assertEqual(edited["due"], 9030)
        self.assertEqual(json.loads(edited["spec"])["prompt"], "original prompt")

    def test_missing_weekly_reset_does_not_queue_or_guess(self):
        self.snapshot["limits"]["rateLimitsByLimitId"]["codex"]["secondary"]["resetsAt"] = None
        with self.assertRaisesRegex(codex.CodexError, "No weekly reset time"):
            self.save("add", "prompt", "--after-weekly-reset", "--project", str(self.root))
        self.assertEqual(self.store.jobs(), [])

    def test_schedule_options_are_mutually_exclusive(self):
        for other in (["--at", "+2h"], ["--after-reset"], ["--repeat-reset", "weekly"]):
            with self.subTest(other=other), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["add", "prompt", "--after-weekly-reset", *other])

    def test_worker_waits_until_reset_and_still_checks_all_allowance(self):
        self.save("add", "prompt", "--after-weekly-reset", "--project", str(self.root))
        row = self.store.jobs()[0]
        with patch("schedulex.codex.snapshot", return_value=self.snapshot) as read, \
                patch("schedulex.worker.reschedule_wake"), patch("schedulex.worker.execute") as execute:
            self.assertFalse(tick(self.store, "fixture", now=9029))
            read.assert_not_called()
            self.snapshot["limits"]["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 100
            self.assertFalse(tick(self.store, "fixture", now=9030))
            execute.assert_not_called()
            self.assertEqual(self.store.get(row["id"])["status"], "pending")
            self.snapshot["limits"]["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 0
            self.assertTrue(tick(self.store, "fixture", now=9031))
            execute.assert_called_once()
