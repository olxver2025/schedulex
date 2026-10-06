"""Use the supported CLI and app-server; never read or copy auth tokens."""
import json
import os
import selectors
import shutil
import subprocess
import time


class CodexError(Exception):
    pass


def subscription_env():
    # Explicit API credentials can override the stored subscription login.
    return {key: value for key, value in os.environ.items()
            if key not in ("OPENAI_API_KEY", "CODEX_API_KEY")}


def executable(value="codex"):
    path = shutil.which(value)
    if not path:
        raise CodexError("Codex CLI not found. Install it or pass --codex /path/to/codex.")
    return os.path.abspath(path)


class AppServer:
    def __init__(self, binary):
        self.process = subprocess.Popen(
            [binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, env=subscription_env(),
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.serial = 0

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params=None, timeout=30):
        self.serial += 1
        request_id = self.serial
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("id") == request_id:
                    if "error" in event:
                        raise CodexError(f"{method}: {event['error'].get('message', 'request failed')}")
                    return event["result"]
                if "id" in event and "method" in event:
                    self.send({"id": event["id"], "error": {
                        "code": -32601, "message": "Schedulex does not handle interactive requests"
                    }})
            events = self.selector.select(max(0, deadline - time.monotonic()))
            if events:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise CodexError("Codex app-server exited before responding. Check `codex app-server` "
                                     "locally for login or state-directory permission errors.")
                self.buffer += chunk
        raise CodexError(f"Timed out reading {method}; no task was started.")

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def snapshot(binary):
    server = AppServer(binary)
    try:
        server.request("initialize", {"clientInfo": {
            "name": "schedulex", "title": "Schedulex", "version": "0.1.0"
        }})
        server.send({"method": "initialized", "params": {}})
        account = server.request("account/read", {"refreshToken": True}).get("account")
        if not account or account.get("type") != "chatgpt":
            raise CodexError("Sign in with your subscription using `codex login` first.")
        if account.get("planType") in (None, "free", "unknown"):
            raise CodexError("A confirmed ChatGPT subscription is required.")
        limits = server.request("account/rateLimits/read")
        return {"plan": account["planType"], "limits": limits}
    finally:
        server.close()


def bucket(snapshot_value, limit_id="codex"):
    limits = snapshot_value["limits"]
    buckets = limits.get("rateLimitsByLimitId")
    if buckets:
        if limit_id not in buckets:
            raise CodexError(f"No usage bucket {limit_id!r}; available: {', '.join(buckets)}")
        return buckets[limit_id]
    legacy = limits.get("rateLimits")
    if legacy and legacy.get("limitId") in (None, limit_id):
        return legacy
    raise CodexError("Codex did not return the selected usage bucket.")


def five_hour_reset(value, limit_id="codex"):
    selected = bucket(value, limit_id)
    for key in ("primary", "secondary"):
        window = selected.get(key)
        if window and window.get("windowDurationMins") == 300 and window.get("resetsAt"):
            return window["resetsAt"]
    raise CodexError("No five-hour reset time is available; schedule with --at instead.")


def allowance(value, limit_id="codex", min_remaining=1):
    selected = bucket(value, limit_id)
    if value["limits"].get("ordinaryUsageAllowed") is False or selected.get("spendControlReached"):
        return False, "Codex reports usage unavailable; waiting"
    windows = [selected.get(key) for key in ("primary", "secondary")]
    windows = [w for w in windows if w]
    if not windows or any(w.get("usedPercent") is None for w in windows):
        return False, "Usage allowance unavailable; waiting"
    if selected.get("rateLimitReachedType"):
        return False, "Codex reports a reached limit; waiting for allowance"
    if any(100 - w["usedPercent"] < min_remaining for w in windows):
        return False, "Insufficient subscription allowance; waiting for reset"
    return True, "Ready"


def command(binary, job, output):
    args = [binary, "-a", "never", "exec", "--json", "--color", "never",
            "--sandbox", job["sandbox"], "--cd", job["cwd"],
            "--skip-git-repo-check", "--output-last-message", str(output),
            "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"']
    if job["model"]:
        args += ["--model", job["model"]]
    args.append("-")
    return args
