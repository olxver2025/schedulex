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
    if not store.claim(row["id"], row["revision"]):
        return
    spec = json.loads(store.get(row["id"])["spec"])
    power.cancel_wake(row["id"], quiet=True)
    print(f"Starting {row['id']} in {spec['cwd']}", flush=True)
    server = None
    thread_id = turn_id = None
    awake_guard = None
    try:
        logs = store.root / "runs" / row["id"]
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        server = codex.AppServer(binary, shared=True)
        if sys.platform == "darwin":
            awake_guard = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(server.process.pid)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server.request("initialize", {"clientInfo": {
            "name": "schedulex", "title": "Schedulex", "version": "0.2.0"}})
        server.send({"method": "initialized", "params": {}})
        config = {"forced_login_method": "chatgpt"}
        if spec.get("effort"):
            config["model_reasoning_effort"] = spec["effort"]
        thread = server.request("thread/start", {
            "cwd": spec["cwd"], "model": spec.get("model"), "modelProvider": "openai",
            "approvalPolicy": "never", "sandbox": spec["sandbox"], "config": config,
            "ephemeral": False, "threadSource": "user"})["thread"]
        thread_id = thread["id"]
        store.set_thread(row["id"], thread_id)
        server.request("thread/name/set", {"threadId": thread_id,
                       "name": "ScheduleX: " + spec["prompt"].strip().splitlines()[0][:80]})
        deadline = time.monotonic() + spec["timeout"]
        turn_id = server.request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": spec["prompt"], "text_elements": []}],
            "effort": spec.get("effort")})["turn"]["id"]
        store.set_turn(row["id"], turn_id)
        with (logs / "events.jsonl").open("w") as events, (logs / "stderr.log").open("w"):
            while True:
                event = server.next_event(deadline)
                events.write(json.dumps(event) + "\n")
                events.flush()
                params = event.get("params", {})
                if params.get("threadId") != thread_id:
                    continue
                if "id" in event and "method" in event:
                    server.send({"id": event["id"], "error": {
                        "code": -32601, "message": "Scheduled tasks cannot request interactive input"}})
                if event.get("method") == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and params.get("turnId") == turn_id:
                        (logs / "answer.txt").write_text(item.get("text", ""))
                if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    status = params["turn"]["status"]
                    outcome = {"completed": "succeeded", "interrupted": "interrupted"}.get(status, "failed")
                    store.finish(row["id"], outcome, 0 if outcome == "succeeded" else None,
                                 "" if outcome == "succeeded" else "Review the Codex chat before scheduling again")
                    print(f"{row['id']}: {outcome}; Codex chat {thread_id}", flush=True)
                    return
    except BaseException as error:
        if server is not None and thread_id is not None:
            try:
                # The daemon owns the turn: killing the proxy alone would leave it running.
                if turn_id is None:
                    turns = server.request("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=10)["thread"].get("turns", [])
                    if turns:
                        turn_id = turns[-1]["id"]
                if turn_id is not None:
                    server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
            except (codex.CodexError, OSError, TimeoutError):
                pass
        store.finish(row["id"], "interrupted", note=f"Task stopped: {error}; review the Codex chat before retrying")
        if not isinstance(error, (TimeoutError, codex.CodexError, OSError)):
            raise
    finally:
        if server is not None:
            server.close()
        if awake_guard is not None:
            try:
                awake_guard.wait(timeout=3)
            except subprocess.TimeoutExpired:
                awake_guard.terminate()


def submit_cloud(store, binary, row):
    if not store.claim(row["id"], row["revision"]):
        return
    spec = json.loads(store.get(row["id"])["spec"])
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


def arm_recurrences(store, binary, now):
    """Discover future resets without deriving either interval from wall-clock time."""
    value = None
    for row in store.jobs(pending=True):
        spec = json.loads(row["spec"])
        if not spec.get("recurrence") or spec.get("reset_at") is not None:
            continue
        try:
            if value is None:
                value = codex.snapshot(binary)
            reset = codex.usage_reset(value, spec["limit_id"], spec["recurrence"])
            if reset <= max(now, spec.get("recurrence_after", 0)):
                raise codex.CodexError("Waiting for a new reported usage reset")
            spec["reset_at"] = reset
            store.update_pending(row["id"], spec, reset + 30, row["revision"])
            reschedule_wake(row["id"], reset + 30, "next usage reset")
        except (codex.CodexError, OSError, ValueError) as error:
            current = store.get(row["id"])
            if current["status"] != "pending" or current["revision"] != row["revision"]:
                continue
            store.note(row["id"], str(error))
            reschedule_wake(row["id"], now + 300, "next usage reset unavailable")


def tick(store, binary, now=None):
    now = time.time() if now is None else now
    arm_recurrences(store, binary, now)
    for row in store.jobs(pending=True):
        if row["due"] > now:
            continue
        spec = json.loads(row["spec"])
        if spec.get("recurrence") and spec.get("reset_at") is None:
            continue
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
        # Arrange the next wake before releasing this run's opportunity to dispatch.
        arm_recurrences(store, binary, time.time())
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
                if row.get("thread_id") and row.get("turn_id"):
                    try:
                        codex.interrupt(binary, row["thread_id"], row["turn_id"])
                    except (codex.CodexError, OSError, TimeoutError) as error:
                        print(f"Could not interrupt previous Codex turn: {error}", flush=True)
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
