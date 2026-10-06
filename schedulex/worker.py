import datetime as dt
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from . import codex, power


def in_window(window, timezone, now):
    if not window:
        return True
    local = dt.datetime.fromtimestamp(now, ZoneInfo(timezone))
    minute = local.hour * 60 + local.minute
    start, end = [int(v[:2]) * 60 + int(v[3:]) for v in window.split("-")]
    return start <= minute < end if start < end else minute >= start or minute < end


def next_window_start(window, timezone, now):
    local = dt.datetime.fromtimestamp(now, ZoneInfo(timezone))
    hour, minute = [int(value) for value in window[:5].split(":")]
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += dt.timedelta(days=1)
    return candidate.timestamp()


def retry_for_allowance(value, limit_id, min_remaining, now):
    try:
        selected = codex.bucket(value, limit_id)
    except codex.CodexError:
        return now + 300
    windows = [selected.get(name) for name in ("primary", "secondary")]
    windows = [window for window in windows if window]
    waiting = []
    reached = bool(selected.get("rateLimitReachedType"))
    for window in windows:
        used = window.get("usedPercent")
        reset = window.get("resetsAt")
        if reset and (reached or used is None or 100 - used < min_remaining):
            waiting.append(float(reset))
    if waiting:
        return max(now + 15, max(waiting) + 30)
    return now + 300


def reschedule_wake(job_id, due, reason):
    try:
        power.retry_wake(job_id, max(due, time.time() + 15))
    except power.PowerError as error:
        print(f"{job_id}: waiting ({reason}); could not reschedule Mac wake: {error}", flush=True)


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
    power.cancel_wake(row["id"], quiet=True)
    print(f"Starting {row['id']} in {spec['cwd']}", flush=True)
    process = None
    awake_guard = None
    try:
        logs = store.root / "runs" / row["id"]
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A new run only starts once. Nonzero exits may already have changed files.
        with (logs / "events.jsonl").open("wb") as stdout, (logs / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(codex.command(binary, spec, logs / "answer.txt"),
                                       stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       start_new_session=True, cwd=spec["cwd"], env=codex.subscription_env())
            if sys.platform == "darwin":
                awake_guard = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(process.pid)],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL)
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
    finally:
        if awake_guard is not None:
            try:
                awake_guard.wait(timeout=3)
            except subprocess.TimeoutExpired:
                awake_guard.terminate()


def submit_cloud(store, binary, row):
    spec = json.loads(row["spec"])
    if not store.claim(row["id"]):
        return
    power.cancel_wake(row["id"], quiet=True)
    print(f"Submitting {row['id']} to Codex Cloud ({spec['cloud_env']})", flush=True)
    args = [binary, "cloud", "exec", "--env", spec["cloud_env"]]
    if spec.get("model"):
        args += ["-c", f"model={json.dumps(spec['model'])}"]
    if spec.get("effort"):
        args += ["-c", f"model_reasoning_effort={json.dumps(spec['effort'])}"]
    args.append(spec["prompt"])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=spec["timeout"],
                                env=codex.subscription_env())
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if result.returncode:
            store.finish(row["id"], "failed", result.returncode,
                         output or "Codex Cloud rejected the task submission")
            print(f"{row['id']}: cloud submission failed", flush=True)
            return
        urls = re.findall(r"https?://[^\s<>]+", output)
        url = urls[0].rstrip(".,)") if urls else None
        note = "Submitted to Codex Cloud" + (f" · {output}" if output else "")
        store.cloud_submitted(row["id"], url, note[:2000])
        print(f"{row['id']}: submitted to Codex Cloud" + (f" ({url})" if url else ""), flush=True)
    except subprocess.TimeoutExpired:
        store.finish(row["id"], "interrupted",
                     note="Cloud submission timed out; check Codex Cloud before retrying")
    except (OSError, subprocess.SubprocessError) as error:
        store.finish(row["id"], "failed", note=f"Could not submit to Codex Cloud: {error}")


def tick(store, binary, now=None):
    now = time.time() if now is None else now
    for row in store.jobs(pending=True):
        if row["due"] > now:
            continue
        spec = json.loads(row["spec"])
        if not in_window(spec["window"], spec["timezone"], now):
            store.note(row["id"], "Waiting for allowed hours")
            reschedule_wake(row["id"], next_window_start(spec["window"], spec["timezone"], now),
                            "allowed hours")
            continue
        if spec["idle_minutes"]:
            try:
                idle = idle_seconds()
            except (OSError, ValueError, subprocess.SubprocessError):
                store.note(row["id"], "Idle time unavailable; waiting")
                reschedule_wake(row["id"], now + 300, "idle time unavailable")
                continue
            if idle < spec["idle_minutes"] * 60:
                store.note(row["id"], "Waiting for keyboard/mouse inactivity")
                remaining = spec["idle_minutes"] * 60 - idle
                reschedule_wake(row["id"], now + max(15, remaining), "keyboard/mouse inactivity")
                continue
        try:
            value = codex.snapshot(binary)
            ready, reason = codex.allowance(value, spec["limit_id"], spec["min_remaining"])
        except (codex.CodexError, OSError, ValueError) as error:
            store.note(row["id"], str(error))
            reschedule_wake(row["id"], now + 300, "usage unavailable")
            continue
        if not ready:
            store.note(row["id"], reason)
            reschedule_wake(row["id"], retry_for_allowance(value, spec["limit_id"],
                                                            spec["min_remaining"], now), reason)
            continue
        if spec.get("destination") == "cloud":
            submit_cloud(store, binary, row)
        else:
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
