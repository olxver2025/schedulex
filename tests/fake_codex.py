#!/usr/bin/env python3
"""Protocol fixture; never makes network requests."""
import base64
import hashlib
import struct
import json
import os
from pathlib import Path
import sys
import time

mode = os.environ.get("FAKE_MODE", "success")
if "daemon" in sys.argv:
    sys.exit(0)
def emit(value):
    payload = json.dumps(value).encode()
    if "proxy" in sys.argv:
        length = len(payload)
        header = bytes([0x81, length]) if length < 126 else b"\x81\x7e" + struct.pack("!H", length)
        sys.stdout.buffer.write(header + payload)
        sys.stdout.buffer.flush()
    else:
        print(payload.decode(), flush=True)


def messages():
    if "proxy" not in sys.argv:
        for line in sys.stdin:
            yield json.loads(line)
        return
    stream = sys.stdin.buffer
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        header += stream.read(1)
    key = next(line.split(b": ")[1] for line in header.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
    sys.stdout.buffer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
    sys.stdout.buffer.flush()
    while True:
        frame = stream.read(2)
        if not frame:
            return
        size = frame[1] & 127
        if size == 126:
            size = struct.unpack("!H", stream.read(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", stream.read(8))[0]
        assert frame[1] & 128
        mask = stream.read(4)
        data = stream.read(size)
        yield json.loads(bytes(byte ^ mask[i % 4] for i, byte in enumerate(data)))


if "app-server" in sys.argv:
    initialized = False
    acknowledged = False
    for message in messages():
        method = message["method"]
        if os.environ.get("FAKE_RPC_LOG"):
            with open(os.environ["FAKE_RPC_LOG"], "a") as trace:
                trace.write(json.dumps(message) + "\n")
        if method == "initialized":
            acknowledged = True
            continue
        if method == "initialize":
            initialized = True
            result = {"userAgent": "fixture"}
        elif not initialized or not acknowledged:
            emit({"id": message["id"], "error": {"message": "Not initialized"}})
            continue
        elif method == "thread/start":
            thread_params = message["params"]
            result = {"thread": {"id": "fixture-thread"}}
        elif method in ("thread/name/set", "turn/interrupt"):
            result = {}
        elif method == "turn/start":
            params = message["params"]
            emit({"id": message["id"], "result": {"turn": {"id": "fixture-turn"}}})
            if mode == "slow":
                continue
            if mode == "incomplete":
                sys.exit(0)
            emit({"method": "item/completed", "params": {
                "threadId": "fixture-thread", "turnId": "fixture-turn",
                "item": {"type": "agentMessage", "text": "done"}}})
            emit({"method": "fixture/transport", "params": {
                "thread": thread_params, "turn": params, "argv": sys.argv}})
            emit({"method": "turn/completed", "params": {
                "threadId": "fixture-thread", "turn": {"id": "fixture-turn", "status":
                    "failed" if mode in ("failed", "eventfailed") else
                    "interrupted" if mode == "interrupted" else "completed"}}})
            continue
        elif method == "account/read":
            result = {"account": {"type": "apiKey" if mode == "apikey" else "chatgpt",
                                   "planType": "free" if mode == "free" else "plus"}}
        elif method == "model/list":
            result = {"data": [{"model": "example-model", "displayName": "Example model",
                                "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
                                "defaultReasoningEffort": "low"}], "nextCursor": None}
        elif method == "config/read":
            result = {"config": {"model": "example-model", "model_reasoning_effort": "high"}}
        elif method == "account/rateLimits/read":
            result = {"rateLimitsByLimitId": {"codex": {
                "limitId": "codex", "rateLimitReachedType": None,
                "primary": {"windowDurationMins": 300, "usedPercent": 10,
                            "resetsAt": int(time.time()) + 3600},
                "secondary": {"windowDurationMins": 10080,
                              "usedPercent": 100 if mode == "weekly" else 20,
                              "resetsAt": int(time.time()) + 86400}
            }}}
        else:
            emit({"id": message["id"], "error": {"message": "Unknown method"}})
            continue
        emit({"method": "fixture/notification", "params": {}})
        emit({"id": message["id"], "result": result})
else:
    sys.exit(2)
