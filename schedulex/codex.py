"""Use the supported CLI and app-server; never read or copy auth tokens."""
import base64
import hashlib
import struct
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
    def __init__(self, binary, shared=False):
        if shared:
            result = subprocess.run([binary, "app-server", "daemon", "start"],
                                    capture_output=True, text=True, timeout=30, env=subscription_env())
            if result.returncode:
                raise CodexError("Could not start the shared Codex server: " + result.stderr.strip())
        self.process = subprocess.Popen(
            [binary, "app-server"] + (["proxy"] if shared else []), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, env=subscription_env(),
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.serial = 0
        self.notifications = []
        self.shared = shared
        self.fragments = b""
        if shared:
            try:
                self.upgrade()
            except BaseException:
                self.close()
                raise

    def send(self, message):
        payload = json.dumps(message).encode()
        if self.shared:
            self.send_frame(payload)
            return
        self.process.stdin.write(payload + b"\n")
        self.process.stdin.flush()

    def send_frame(self, payload, opcode=1):
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.process.stdin.write(header + mask + masked)
        self.process.stdin.flush()

    def read_bytes(self, count, deadline):
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Timed out waiting for Codex")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise CodexError("Codex app-server disconnected; inspect the chat before retrying")
            self.buffer += chunk
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def upgrade(self):
        # `app-server proxy` forwards raw socket bytes, including WebSocket framing.
        key = base64.b64encode(os.urandom(16)).decode()
        self.process.stdin.write(("GET /rpc HTTP/1.1\r\nHost: codex-app-server\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 10
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            header += self.read_bytes(1, deadline)
            if len(header) > 16384:
                raise CodexError("Invalid shared Codex server handshake")
        expected = base64.b64encode(hashlib.sha1((key +
            "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        fields = dict(line.split(":", 1) for line in header.decode().split("\r\n")[1:] if ":" in line)
        fields = {k.lower(): v.strip() for k, v in fields.items()}
        if not header.startswith(b"HTTP/1.1 101 ") or fields.get("sec-websocket-accept") != expected:
            raise CodexError("Shared Codex server rejected the WebSocket handshake")

    def read_frame_event(self, deadline):
        while True:
            first, second = self.read_bytes(2, deadline)
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self.read_bytes(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read_bytes(8, deadline))[0]
            if length > 64 * 1024 * 1024:
                raise CodexError("Shared Codex server message is too large")
            mask = self.read_bytes(4, deadline) if second & 128 else None
            payload = self.read_bytes(length, deadline)
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            opcode = first & 15
            if opcode == 8:
                raise CodexError("Shared Codex server closed the connection")
            if opcode == 9:
                self.send_frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise CodexError("Unexpected shared Codex server frame")
            self.fragments += payload
            if len(self.fragments) > 64 * 1024 * 1024:
                raise CodexError("Shared Codex server message is too large")
            if first & 128:
                payload, self.fragments = self.fragments, b""
                return json.loads(payload)

    def request(self, method, params=None, timeout=30):
        self.serial += 1
        request_id = self.serial
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.read_event(deadline)
            except TimeoutError as error:
                raise CodexError(f"Timed out reading {method}") from error
            if event.get("id") == request_id:
                if "error" in event:
                    raise CodexError(f"{method}: {event['error'].get('message', 'request failed')}")
                return event["result"]
            if "id" in event and "method" in event:
                self.send({"id": event["id"], "error": {
                    "code": -32601, "message": "Schedulex does not handle interactive requests"
                }})
            else:
                self.notifications.append(event)
        raise CodexError(f"Timed out reading {method}")

    def read_event(self, deadline):
        if self.shared:
            return self.read_frame_event(deadline)
        while True:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.strip():
                    return json.loads(line)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Timed out waiting for Codex")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise CodexError("Codex app-server disconnected; inspect the chat before retrying")
            self.buffer += chunk

    def next_event(self, deadline):
        if self.notifications:
            return self.notifications.pop(0)
        return self.read_event(deadline)

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
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


def catalog(binary):
    server = AppServer(binary)
    try:
        server.request("initialize", {"clientInfo": {"name": "schedulex", "title": "Schedulex", "version": "0.2.0"}})
        server.send({"method": "initialized", "params": {}})
        entries = []
        cursor = None
        seen = set()
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = server.request("model/list", params)
            for model in result["data"]:
                entries.append({"id": model["model"], "name": model["displayName"],
                                "efforts": [e["reasoningEffort"] for e in model["supportedReasoningEfforts"]],
                                "defaultEffort": model.get("defaultReasoningEffort")})
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen or len(seen) >= 50:
                raise CodexError("Model catalog pagination did not finish")
            seen.add(cursor)
        try:
            config = server.request("config/read", {"includeLayers": False}).get("config", {})
        except CodexError:
            config = {}
        return {"models": entries, "defaultModel": config.get("model"),
                "defaultEffort": config.get("model_reasoning_effort")}
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
    return usage_reset(value, limit_id, "five-hour")


def usage_reset(value, limit_id="codex", period="five-hour"):
    minutes = {"five-hour": 300, "weekly": 10080}[period]
    selected = bucket(value, limit_id)
    for key in ("primary", "secondary"):
        window = selected.get(key)
        if window and window.get("windowDurationMins") == minutes and window.get("resetsAt"):
            return float(window["resetsAt"])
    raise CodexError(f"No {period} reset time is available; waiting for Codex to report it.")


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


def interrupt(binary, thread_id, turn_id):
    server = AppServer(binary, shared=True)
    try:
        server.request("initialize", {"clientInfo": {
            "name": "schedulex", "title": "Schedulex", "version": "0.2.0"}})
        server.send({"method": "initialized", "params": {}})
        server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
    finally:
        server.close()
