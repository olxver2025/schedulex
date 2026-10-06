#!/usr/bin/env python3
"""Protocol fixture; never makes network requests."""
import json
import os
from pathlib import Path
import sys
import time

mode = os.environ.get("FAKE_MODE", "success")
if "app-server" in sys.argv:
    initialized = False
    acknowledged = False
    for line in sys.stdin:
        message = json.loads(line)
        method = message["method"]
        if method == "initialized":
            acknowledged = True
            continue
        if method == "initialize":
            initialized = True
            result = {"userAgent": "fixture"}
        elif not initialized or not acknowledged:
            print(json.dumps({"id": message["id"], "error": {"message": "Not initialized"}}), flush=True)
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
            print(json.dumps({"id": message["id"], "error": {"message": "Unknown method"}}), flush=True)
            continue
        print(json.dumps({"method": "fixture/notification", "params": {}}), flush=True)
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
elif "exec" in sys.argv:
    prompt = sys.stdin.read()
    output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    (output.parent / "received.txt").write_text(prompt)
    (output.parent / "argv.json").write_text(json.dumps(sys.argv))
    if mode == "slow":
        time.sleep(10)
    if mode == "failed":
        print(json.dumps({"type": "turn.failed", "error": {"message": "quota exhausted"}}))
        sys.exit(1)
    if mode == "eventfailed":
        print(json.dumps({"type": "error", "message": "failed"}))
    output.write_text("done")
    if mode != "incomplete":
        print(json.dumps({"type": "turn.completed"}))
else:
    sys.exit(2)
