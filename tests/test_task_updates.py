import contextlib
import io
import json
import time
import unittest
from unittest.mock import patch

from schedulex import codex, power
from schedulex.cli import main
from schedulex.dashboard import dashboard
from schedulex.store import Store
from schedulex.worker import tick
import test_schedulex as base


class TaskUpdateTests(unittest.TestCase):
    setUp = base.SchedulerTests.setUp
    tearDown = base.SchedulerTests.tearDown
    spec = base.SchedulerTests.spec
    add = base.SchedulerTests.add

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--state-dir", str(self.store.root), "--codex", str(self.fake), *args])

    def snapshot(self, five=None, weekly=None):
        return {"plan": "plus", "limits": {"rateLimitsByLimitId": {"codex": {
            "primary": {"windowDurationMins": 300, "usedPercent": 10, "resetsAt": five},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 20, "resetsAt": weekly}}}}}

    def test_edit_and_reschedule_preserve_id_and_unspecified_settings(self):
        job = self.add(due=time.time() + 120, sandbox="workspace-write", model="example-model",
                       effort="high", timeout=999, min_remaining=20)
        original = self.store.get(job)
        self.cli("edit", job, "--at", "+3h", "new café 🦆\nfull prompt")
        row = self.store.get(job)
        spec = json.loads(row["spec"])
        self.assertEqual(row["created"], original["created"])
        self.assertGreater(row["due"], time.time() + 10700)
        self.assertEqual(spec["prompt"], "new café 🦆\nfull prompt")
        self.assertEqual((spec["sandbox"], spec["model"], spec["effort"], spec["timeout"], spec["min_remaining"]),
                         ("workspace-write", "example-model", "high", 999, 20))
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(row["revision"], 1)

    def test_prompt_only_edit_keeps_time_and_clear_options(self):
        job = self.add(due=time.time() + 100, model="example-model", effort="high", window="23:00-07:00")
        before = self.store.get(job)
        self.cli("edit", job, "--clear-model", "--clear-effort", "--clear-window", "--clear-idle", "changed")
        after = self.store.get(job)
        self.assertEqual(before["due"], after["due"])
        spec = json.loads(after["spec"])
        self.assertEqual((spec["model"], spec["effort"], spec["window"], spec["idle_minutes"]), (None, None, None, 0))

    def test_edit_rejects_running_terminal_and_stale_revision(self):
        pending = self.add()
        self.cli("edit", pending, "changed")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("edit", pending, "--revision", "0", "stale")
        self.assertEqual(json.loads(self.store.get(pending)["spec"])["prompt"], "changed")
        self.store.claim(pending)
        for status in ("running", "succeeded", "cancelled"):
            if status != "running":
                self.store.finish(pending, status)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.cli("edit", pending, "--at", "+2h")

    def test_edit_rolls_back_if_wake_update_fails(self):
        job = self.add(due=time.time() + 100)
        before = self.store.get(job)
        with patch("schedulex.cli.power.schedule_wake", side_effect=power.PowerError("helper offline")), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("edit", job, "--at", "+2h", "new prompt")
        self.assertEqual(self.store.get(job), before)

    def test_worker_does_not_execute_a_spec_changed_during_preflight(self):
        job = self.add()
        def snapshot(binary):
            row = self.store.get(job)
            spec = json.loads(row["spec"])
            spec["prompt"] = "replacement"
            self.store.update_pending(job, spec, time.time() + 3600, row["revision"])
            return self.snapshot(time.time() + 3600, time.time() + 86400)
        with patch("schedulex.worker.codex.snapshot", side_effect=snapshot):
            tick(self.store, str(self.fake))
        self.assertEqual(self.store.get(job)["status"], "pending")
        self.assertFalse((self.store.root / "runs" / job).exists())

    def test_repeat_choices_capture_only_matching_reported_reset(self):
        now = time.time()
        with patch("schedulex.codex.snapshot", return_value=self.snapshot(now + 3600, now + 86400)):
            for period, reset in (("five-hour", now + 3600), ("weekly", now + 86400)):
                self.cli("add", "--repeat-reset", period, "repeat this")
                row = next(r for r in self.store.jobs(pending=True) if json.loads(r["spec"])["recurrence"] == period)
                self.assertEqual(row["due"], reset + 30)
                self.assertEqual(json.loads(row["spec"])["reset_at"], reset)
        with patch("schedulex.codex.snapshot", return_value=self.snapshot(None, None)), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("add", "--repeat-reset", "weekly", "unavailable")
        self.assertEqual(len(self.store.jobs()), 2)

    def test_same_recurring_edit_keeps_the_captured_reset(self):
        reset = time.time() + 3600
        job = self.add(due=reset + 30, recurrence="five-hour", reset_at=reset, schedule="repeat-reset")
        with patch("schedulex.codex.snapshot", side_effect=AssertionError("must preserve captured reset")):
            self.cli("edit", job, "--repeat-reset", "five-hour", "--limit-id", "codex", "changed")
        self.assertEqual(self.store.get(job)["due"], reset + 30)
        self.assertEqual(json.loads(self.store.get(job)["spec"])["prompt"], "changed")

    def test_success_keeps_history_and_waits_for_distinct_future_reset(self):
        for period in ("five-hour", "weekly"):
            with self.subTest(period=period):
                now = time.time()
                reset = now - 60
                job = self.add(due=reset + 30, recurrence=period, reset_at=reset, schedule="repeat-reset")
                # Old timestamps must not cause another execution or an invented interval.
                stale = self.snapshot(reset, reset)
                with patch("schedulex.worker.codex.snapshot", return_value=stale), patch("schedulex.worker.power.retry_wake"):
                    self.assertTrue(tick(self.store, str(self.fake)))
                    next_row = next(r for r in self.store.jobs(pending=True) if r["previous_id"] == job)
                    self.assertEqual(self.store.get(job)["status"], "succeeded")
                    self.assertIsNone(json.loads(next_row["spec"])["reset_at"])
                    self.assertFalse(tick(self.store, str(self.fake)))
                    # Repeated terminal bookkeeping cannot create another successor.
                    self.store.finish(job, "succeeded")
                    self.assertEqual(sum(r["previous_id"] == job for r in self.store.jobs()), 1)
                future = now + (7200 if period == "five-hour" else 172800)
                live = self.snapshot(future if period == "five-hour" else now + 300,
                                     future if period == "weekly" else now + 86400)
                with patch("schedulex.worker.codex.snapshot", return_value=live), patch("schedulex.worker.power.retry_wake"):
                    self.assertFalse(tick(self.store, str(self.fake)))
                    self.assertEqual(self.store.get(next_row["id"])["due"], future + 30)
                self.store.cancel(next_row["id"])

    def test_recurrence_survives_database_reopen_and_missing_usage(self):
        job = self.add(recurrence="weekly", reset_at=time.time() - 60, schedule="repeat-reset")
        self.store.finish(job, "succeeded")
        other = Store(self.store.root)
        try:
            with patch("schedulex.worker.codex.snapshot", side_effect=codex.CodexError("offline")), \
                    patch("schedulex.worker.power.retry_wake"):
                self.assertFalse(tick(other, str(self.fake)))
            next_row = other.jobs(pending=True)[0]
            self.assertEqual(next_row["note"], "offline")
            self.assertIsNone(json.loads(next_row["spec"])["reset_at"])
        finally:
            other.db.close()

    def test_recurring_failure_and_interruption_stop_without_retry(self):
        for status in ("failed", "interrupted"):
            job = self.add(recurrence="five-hour", reset_at=time.time() - 60)
            self.store.finish(job, status)
            self.assertFalse(self.store.jobs(pending=True))
            self.assertIn("Recurrence stopped", self.store.get(job)["note"])

    def test_missed_resets_do_not_create_catchup_backlog(self):
        now = time.time()
        job = self.add(due=now - 86400, recurrence="five-hour", reset_at=now - 86430)
        future = now + 1200
        with patch("schedulex.worker.codex.snapshot", return_value=self.snapshot(future, now + 86400)), \
                patch("schedulex.worker.power.retry_wake"):
            tick(self.store, str(self.fake))
            self.assertEqual(len(self.store.jobs()), 2)
            next_row = self.store.jobs(pending=True)[0]
            self.assertEqual(next_row["due"], future + 30)
            self.assertFalse(tick(self.store, str(self.fake)))
            self.store.cancel(next_row["id"])
            self.assertFalse(tick(self.store, str(self.fake), now=future + 31))

    def test_repeating_run_does_not_repeat_twice_at_the_same_reset(self):
        now = time.time()
        job = self.add(due=now - 30, recurrence="five-hour", reset_at=now - 60)
        reset = now + 3600
        live = self.snapshot(reset, now + 86400)
        with patch("schedulex.worker.codex.snapshot", return_value=live), patch("schedulex.worker.power.retry_wake"):
            self.assertTrue(tick(self.store, str(self.fake)))
            second = self.store.jobs(pending=True)[0]
            self.assertTrue(tick(self.store, str(self.fake), now=reset + 31))
            self.assertEqual(self.store.get(second["id"])["status"], "succeeded")
            self.assertEqual(len(self.store.jobs()), 3)
            third = self.store.jobs(pending=True)[0]
            self.assertIsNone(json.loads(third["spec"])["reset_at"])
            self.assertFalse(tick(self.store, str(self.fake), now=reset + 60))
            self.assertEqual(len(self.store.jobs()), 3)

    def test_cancel_during_reset_lookup_does_not_rearm_the_cancelled_task(self):
        job = self.add(recurrence="weekly", reset_at=None, recurrence_after=0)
        def snapshot(binary):
            self.store.cancel(job)
            return self.snapshot(time.time() + 3600, time.time() + 86400)
        with patch("schedulex.worker.codex.snapshot", side_effect=snapshot), \
                patch("schedulex.worker.power.retry_wake") as wake:
            self.assertFalse(tick(self.store, str(self.fake)))
        wake.assert_not_called()
        self.assertEqual(self.store.get(job)["status"], "cancelled")

    def test_completion_feed_is_not_truncated_with_recent_cards(self):
        for index in range(15):
            job = self.add(prompt=f"finished {index}")
            self.store.finish(job, "succeeded")
        with patch("schedulex.codex.executable", return_value="fake"), \
                patch("schedulex.codex.snapshot", side_effect=codex.CodexError("offline")):
            value = dashboard(self.store, "fake")
        self.assertEqual(len(value["jobs"]), 10)
        self.assertEqual(len(value["completions"]), 15)
        self.assertTrue(all(c["finished"] and c["status"] == "succeeded" for c in value["completions"]))


if __name__ == "__main__":
    unittest.main()
