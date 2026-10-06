import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from schedulex import codex
from schedulex.cli import main, parse_time, parser
from schedulex.store import Store
from schedulex.worker import in_window, run, tick


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.fake = self.root / "codex"
        shutil.copy(Path(__file__).with_name("fake_codex.py"), self.fake)
        self.fake.chmod(0o700)
        self.env = patch.dict(os.environ, {"FAKE_MODE": "success"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.store.db.close()
        self.temp.cleanup()

    def spec(self, **changes):
        value = {"prompt": "Line one\nfix café 🦆\n`$HOME` remains literal\n",
                 "cwd": str(self.root), "sandbox": "read-only", "model": None,
                 "window": None, "timezone": "Europe/London", "idle_minutes": 0,
                 "min_remaining": 1, "limit_id": "codex", "timeout": 30, "schedule": "at"}
        value.update(changes)
        return value

    def add(self, due=None, **changes):
        return self.store.add(self.spec(**changes), time.time() - 1 if due is None else due)

    def test_real_subprocess_handshake_and_reset(self):
        value = codex.snapshot(str(self.fake))
        self.assertEqual(value["plan"], "plus")
        self.assertGreater(codex.five_hour_reset(value), time.time() + 3500)
        self.assertEqual(codex.allowance(value), (True, "Ready"))

    def test_api_key_and_free_rejected(self):
        for mode in ("apikey", "free"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"FAKE_MODE": mode}):
                with self.assertRaises(codex.CodexError):
                    codex.snapshot(str(self.fake))

    def test_missing_five_hour_and_unknown_bucket_rejected(self):
        value = {"limits": {"rateLimitsByLimitId": {"codex": {"primary": {
            "windowDurationMins": 60, "resetsAt": 123, "usedPercent": 0}}}}}
        with self.assertRaises(codex.CodexError):
            codex.five_hour_reset(value)
        with self.assertRaises(codex.CodexError):
            codex.bucket(value, "other")

    def test_unknown_allowance_waits(self):
        self.assertFalse(codex.allowance({"limits": {"rateLimits": {"primary": None}}})[0])

    def test_usage_denied_and_api_environment(self):
        value = codex.snapshot(str(self.fake))
        value["limits"]["ordinaryUsageAllowed"] = False
        self.assertFalse(codex.allowance(value)[0])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture", "CODEX_API_KEY": "fixture"}):
            self.assertNotIn("OPENAI_API_KEY", codex.subscription_env())
            self.assertNotIn("CODEX_API_KEY", codex.subscription_env())

    def test_reset_secondary_and_legacy(self):
        value = {"limits": {"rateLimits": {"secondary": {
            "windowDurationMins": 300, "resetsAt": 42, "usedPercent": 0}}}}
        self.assertEqual(codex.five_hour_reset(value), 42)

    def test_full_prompt_and_permission_flags(self):
        job = self.add(sandbox="workspace-write", model="example-model")
        self.assertTrue(tick(self.store, str(self.fake)))
        self.assertEqual(self.store.get(job)["status"], "succeeded")
        logs = self.store.root / "runs" / job
        self.assertEqual((logs / "received.txt").read_text(), self.spec()["prompt"])
        args = json.loads((logs / "argv.json").read_text())
        self.assertIn("workspace-write", args)
        self.assertIn("never", args)
        self.assertIn("example-model", args)
        self.assertIn('model_provider="openai"', args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertFalse(tick(self.store, str(self.fake)))

    def test_future_cancel_and_weekly_gate(self):
        future = self.add(due=time.time() + 3600)
        cancel = self.add()
        self.store.cancel(cancel)
        waiting = self.add()
        with patch.dict(os.environ, {"FAKE_MODE": "weekly"}):
            self.assertFalse(tick(self.store, str(self.fake)))
        self.assertEqual(self.store.get(future)["status"], "pending")
        self.assertEqual(self.store.get(cancel)["status"], "cancelled")
        self.assertIn("allowance", self.store.get(waiting)["note"])
        self.assertTrue(tick(self.store, str(self.fake)))
        self.assertEqual(self.store.get(waiting)["status"], "succeeded")

    def test_failed_and_incomplete_runs_never_retry(self):
        for mode in ("failed", "eventfailed", "incomplete"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"FAKE_MODE": mode}):
                job = self.add()
                tick(self.store, str(self.fake))
                self.assertEqual(self.store.get(job)["status"], "failed")
                self.assertFalse(tick(self.store, str(self.fake)))

    def test_timeout_does_not_retry(self):
        job = self.add(timeout=1)
        with patch.dict(os.environ, {"FAKE_MODE": "slow"}):
            tick(self.store, str(self.fake))
        self.assertEqual(self.store.get(job)["status"], "interrupted")
        self.assertFalse(tick(self.store, str(self.fake)))

    def test_recover_interrupted_running_job(self):
        job = self.add()
        self.store.claim(job)
        run(self.store, str(self.fake), once=True)
        self.assertEqual(self.store.get(job)["status"], "interrupted")
        self.assertFalse((self.store.root / "runs" / job).exists())

    def test_serial_execution(self):
        first, second = self.add(), self.add()
        tick(self.store, str(self.fake))
        self.assertEqual(self.store.get(first)["status"], "succeeded")
        self.assertEqual(self.store.get(second)["status"], "pending")

    def test_idle_gate_and_unavailable(self):
        job = self.add(idle_minutes=15)
        with patch("schedulex.worker.idle_seconds", return_value=30):
            self.assertFalse(tick(self.store, str(self.fake)))
        with patch("schedulex.worker.idle_seconds", side_effect=ValueError("unavailable")):
            self.assertFalse(tick(self.store, str(self.fake)))
        with patch("schedulex.worker.idle_seconds", return_value=1000):
            self.assertTrue(tick(self.store, str(self.fake)))
        self.assertEqual(self.store.get(job)["status"], "succeeded")

    def test_overnight_and_dst_windows(self):
        def utc(iso):
            return dt.datetime.fromisoformat(iso).timestamp()
        self.assertTrue(in_window("23:00-07:00", "Europe/London", utc("2026-10-06T23:00+01:00")))
        self.assertTrue(in_window("23:00-07:00", "Europe/London", utc("2026-10-07T06:59+01:00")))
        self.assertFalse(in_window("23:00-07:00", "Europe/London", utc("2026-10-07T07:00+01:00")))
        self.assertTrue(in_window("01:00-03:00", "Europe/London", utc("2026-10-25T01:30+01:00")))
        self.assertTrue(in_window("01:00-03:00", "Europe/London", utc("2026-10-25T01:30+00:00")))

    def test_window_gate_and_network_fail_closed(self):
        job = self.add(window="23:00-07:00")
        noon = dt.datetime.fromisoformat("2026-10-07T12:00+01:00").timestamp()
        self.assertFalse(tick(self.store, str(self.fake), now=noon))
        night = dt.datetime.fromisoformat("2026-10-07T23:00+01:00").timestamp()
        with patch("schedulex.codex.snapshot", side_effect=codex.CodexError("offline")):
            self.assertFalse(tick(self.store, str(self.fake), now=night))
        self.assertEqual(self.store.get(job)["note"], "offline")

    def test_cli_add_reset_and_full_file(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text(self.spec()["prompt"])
        with contextlib.redirect_stdout(io.StringIO()):
            main(["--state-dir", str(self.store.root), "--codex", str(self.fake),
                  "add", "--after-reset", "--prompt-file", str(prompt), "--cwd", str(self.root)])
        row = self.store.jobs()[0]
        self.assertGreater(row["due"], time.time() + 3600)
        self.assertEqual(json.loads(row["spec"])["prompt"], prompt.read_text())

    def test_cli_stdin_and_persistence(self):
        with patch("sys.stdin", io.StringIO(self.spec()["prompt"])), contextlib.redirect_stdout(io.StringIO()):
            main(["--state-dir", str(self.store.root), "add", "--at", "+2h"])
        other = Store(self.store.root)
        try:
            self.assertEqual(len(other.jobs()), 1)
            self.assertEqual(json.loads(other.jobs()[0]["spec"])["prompt"], self.spec()["prompt"])
        finally:
            other.db.close()

    def test_time_validation(self):
        self.assertEqual(parse_time("+2h", now=100), 7300)
        for value in ("tomorrow", "2026-10-07T02:00", "2000-01-01T00:00Z", "+-1h"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time(value)

    def test_lock_rejects_second_worker(self):
        import fcntl
        with (self.store.root / "worker.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "already running"):
                run(self.store, str(self.fake), once=True)

    def test_mac_service_install_status_and_uninstall(self):
        import plistlib
        from schedulex.cli import service
        args = parser().parse_args(["--codex", str(self.fake), "service", "install"])
        with patch("schedulex.cli.sys.platform", "darwin"), patch("schedulex.cli.Path.home", return_value=self.root), \
                patch("schedulex.cli.subprocess.run") as launchctl, contextlib.redirect_stdout(io.StringIO()):
            launchctl.return_value = subprocess.CompletedProcess([], 0, "loaded", "")
            service(args, self.store)
            plist = self.root / "Library/LaunchAgents/local.schedulex.worker.plist"
            value = plistlib.loads(plist.read_bytes())
            self.assertTrue(value["RunAtLoad"])
            self.assertTrue(value["KeepAlive"])
            self.assertIn(str(self.store.root), value["ProgramArguments"])
            self.assertIn(str(self.fake), value["ProgramArguments"])
            self.assertEqual(launchctl.call_args.args[0][1], "bootstrap")
            args.action = "status"
            service(args, self.store)
            self.assertEqual(launchctl.call_args.args[0][1], "print")
            args.action = "uninstall"
            service(args, self.store)
            self.assertFalse(plist.exists())
            self.assertTrue((self.store.root / "jobs.sqlite3").exists())

    def test_sigterm_interrupts_active_task(self):
        import signal
        job = self.add()
        env = dict(os.environ, FAKE_MODE="slow")
        process = subprocess.Popen([sys.executable, "-m", "schedulex", "--state-dir", str(self.store.root),
                                    "--codex", str(self.fake), "worker"], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while self.store.get(job)["status"] != "running" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.store.get(job)["status"], "running")
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertEqual(self.store.get(job)["status"], "interrupted")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
