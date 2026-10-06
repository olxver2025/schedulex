import datetime as dt
import fcntl
import json
import os
import re
import signal
import subprocess
import time
from zoneinfo import ZoneInfo

from . import codex


def in_window(window, timezone, now):
    if not window:
        return True
    local = dt.datetime.fromtimestamp(now, ZoneInfo(timezone))
    minute = local.hour * 60 + local.minute
    start, end = [int(v[:2]) * 60 + int(v[3:]) for v in window.split("-")]
    return start <= minute < end if start < end else minute >= start or minute < end


def idle_seconds():
    result = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
                            capture_output=True, text=True, timeout=10, check=True)
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
    if not match:
        raise ValueError("Could not read macOS idle time")
    return int(match[1]) / 1_000_000_000


def stop_process(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def execute(store, binary, row):
    spec = json.loads(row["spec"])
    if not store.claim(row["id"]):
        return
    print(f"Starting {row['id']} in {spec['cwd']}", flush=True)
    process = None
    try:
        logs = store.root / "runs" / row["id"]
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A new run only starts once. Nonzero exits may already have changed files.
        with (logs / "events.jsonl").open("wb") as stdout, (logs / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(codex.command(binary, spec, logs / "answer.txt"),
                                       stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       start_new_session=True, cwd=spec["cwd"], env=codex.subscription_env())
            try:
                process.communicate(spec["prompt"].encode(), timeout=spec["timeout"])
            except subprocess.TimeoutExpired:
                stop_process(process)
                store.finish(row["id"], "interrupted", process.returncode,
                             "Task timed out; inspect changes before scheduling again")
                return
        event_failure = False
        completed = False
        with (logs / "events.jsonl").open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") in ("turn.failed", "error"):
                    event_failure = True
                if event.get("type") == "turn.completed":
                    completed = True
        success = process.returncode == 0 and completed and not event_failure
        store.finish(row["id"], "succeeded" if success else "failed", process.returncode,
                     "" if success else "Inspect logs and workspace; task is not automatically retried")
        print(f"{row['id']}: {'succeeded' if success else 'failed'}; results in {logs}", flush=True)
    except BaseException:
        if process is not None:
            stop_process(process)
        store.finish(row["id"], "interrupted", note="Worker stopped; inspect changes before scheduling again")
        raise


def tick(store, binary, now=None):
    now = time.time() if now is None else now
    for row in store.jobs(pending=True):
        if row["due"] > now:
            continue
        spec = json.loads(row["spec"])
        if not in_window(spec["window"], spec["timezone"], now):
            store.note(row["id"], "Waiting for allowed hours")
            continue
        if spec["idle_minutes"]:
            try:
                idle = idle_seconds()
            except (OSError, ValueError, subprocess.SubprocessError):
                store.note(row["id"], "Idle time unavailable; waiting")
                continue
            if idle < spec["idle_minutes"] * 60:
                store.note(row["id"], "Waiting for keyboard/mouse inactivity")
                continue
        try:
            value = codex.snapshot(binary)
            ready, reason = codex.allowance(value, spec["limit_id"], spec["min_remaining"])
        except (codex.CodexError, OSError, ValueError) as error:
            store.note(row["id"], str(error))
            continue
        if not ready:
            store.note(row["id"], reason)
            continue
        execute(store, binary, row)
        return True
    return False


def run(store, binary, once=False, interval=60):
    lock = (store.root / "worker.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError("A worker is already running for this queue")
    previous_handler = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        # A crashed worker's task may have made changes. Never duplicate its execution.
        for row in store.jobs():
            if row["status"] == "running":
                store.finish(row["id"], "interrupted", note="Previous worker stopped; review workspace and logs")
        while True:
            tick(store, binary)
            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        lock.close()
