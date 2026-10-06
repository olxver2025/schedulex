"""macOS integration check: temporary LaunchAgent and fake Codex, no inference."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

from schedulex.store import Store


def main():
    if sys.platform != "darwin":
        raise SystemExit("This check requires macOS")
    label = f"local.schedulex.verify.{os.getpid()}"
    target = f"gui/{os.getuid()}/{label}"
    with tempfile.TemporaryDirectory(prefix="schedulex-launchd-") as temp:
        root = Path(temp)
        fake = root / "codex"
        shutil.copy(Path(__file__).with_name("fake_codex.py"), fake)
        fake.chmod(0o700)
        store = Store(root / "state")
        job = store.add({"prompt": "scheduled launchd test\n", "cwd": str(root),
                         "sandbox": "read-only", "model": None, "window": None,
                         "timezone": "Europe/London", "idle_minutes": 0,
                         "min_remaining": 1, "limit_id": "codex", "timeout": 30}, time.time() + 2)
        plist = root / "worker.plist"
        source = str(Path(__file__).resolve().parent.parent)
        document = {"Label": label, "ProgramArguments": [sys.executable, "-m", "schedulex",
                    "--state-dir", str(store.root), "--codex", str(fake), "worker", "--interval", "1"],
                    "WorkingDirectory": source, "RunAtLoad": True,
                    "EnvironmentVariables": {"PYTHONPATH": source, "PATH": os.environ["PATH"]},
                    "StandardOutPath": str(root / "stdout.log"), "StandardErrorPath": str(root / "stderr.log")}
        plist.write_bytes(plistlib.dumps(document))
        loaded = False
        try:
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
            loaded = True
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and store.get(job)["status"] in ("pending", "running"):
                time.sleep(0.2)
            row = store.get(job)
            if row["status"] != "succeeded":
                log = root / "stderr.log"
                raise RuntimeError(f"Service test failed: {row}; {log.read_text() if log.exists() else 'no log'}")
            events = [json.loads(line) for line in (store.root / "runs" / job / "events.jsonl").read_text().splitlines()]
            transport = next(e["params"] for e in events if e.get("method") == "fixture/transport")
            assert transport["turn"]["input"][0]["text"] == "scheduled launchd test\n"
            print("PASS: launchd started the worker and executed a future queued prompt")
        finally:
            if loaded:
                subprocess.run(["launchctl", "bootout", target], check=True)
            store.db.close()


if __name__ == "__main__":
    main()
