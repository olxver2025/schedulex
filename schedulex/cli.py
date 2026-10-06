import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from . import codex
from .store import Store
from .worker import run


def timestamp(value):
    return dt.datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def parse_time(value, now=None):
    now = time.time() if now is None else now
    match = re.fullmatch(r"\+(\d+)([mhd])", value)
    if match:
        return now + int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use an ISO date/time (2026-10-07T02:00+01:00) or +30m, +2h, +1d")
    if parsed.tzinfo is None:
        raise ValueError("Include a timezone offset in --at to avoid ambiguous local times")
    if parsed.timestamp() <= now:
        raise ValueError("Schedule time must be in the future")
    return parsed.timestamp()


def window(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("Use HH:MM-HH:MM, e.g. 23:00-07:00")
    if value[:5] == value[6:]:
        raise argparse.ArgumentTypeError("Start and end must differ; omit --window for all hours")
    return value


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def parser():
    p = argparse.ArgumentParser(description="Schedule full Codex prompts locally using your subscription.")
    p.add_argument("--state-dir", default=os.environ.get("SCHEDULEX_HOME", "~/.local/share/schedulex"))
    p.add_argument("--codex", default=os.environ.get("SCHEDULEX_CODEX", "codex"), help="Codex executable")
    commands = p.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Schedule a prompt; omission of prompt reads stdin")
    add.add_argument("prompt", nargs="?", help="Complete prompt (quote multiline text)")
    add.add_argument("--prompt-file", type=Path, help="UTF-8 file; '-' reads stdin")
    when = add.add_mutually_exclusive_group(required=True)
    when.add_argument("--at", help="ISO time with offset, or +30m / +2h / +1d")
    when.add_argument("--after-reset", action="store_true", help="Next reported five-hour reset")
    add.add_argument("--cwd", type=Path, default=Path.cwd(), help="Workspace to run in")
    add.add_argument("--window", type=window, help="Allowed start hours, e.g. 23:00-07:00")
    add.add_argument("--timezone", default="Europe/London", help="IANA timezone for allowed hours")
    add.add_argument("--idle-minutes", type=positive, default=0, help="Require Mac keyboard/mouse inactivity before starting")
    add.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only",
                     help="Explicitly grant workspace writes with workspace-write")
    add.add_argument("--model", help="Optional model; otherwise uses your Codex default")
    add.add_argument("--effort", "--reasoning-effort",
                     help="Reasoning effort (model-dependent); omitted uses your Codex default")
    add.add_argument("--limit-id", default="codex", help="Usage bucket; use limits to inspect available buckets")
    add.add_argument("--min-remaining", type=float, default=1, help="Required remaining percent in every reported window")
    add.add_argument("--timeout", type=positive, default=7200, help="Maximum task runtime in seconds")
    commands.add_parser("list", help="Show queue and job states")
    show = commands.add_parser("show", help="Show complete prompt, status and run paths")
    show.add_argument("id")
    cancel = commands.add_parser("cancel", help="Cancel a pending job")
    cancel.add_argument("id")
    commands.add_parser("limits", help="Read live subscription usage and reset times (no inference)")
    commands.add_parser("dashboard", help="JSON status for the menu bar app")
    commands.add_parser("models", help="List live models and their supported reasoning efforts")
    menu = commands.add_parser("menubar", help="Open the macOS menu bar app")
    menu.add_argument("--app", type=Path, default=Path.home() / "Applications" / "Schedulex.app")
    worker = commands.add_parser("worker", help="Run local worker; keep computer awake and online")
    worker.add_argument("--once", action="store_true", help="Check the queue and run at most one due job")
    worker.add_argument("--interval", type=positive, default=60)
    service = commands.add_parser("service", help="macOS login background worker")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    return p


def add_job(args, store):
    if args.prompt is not None and args.prompt_file is not None:
        raise ValueError("Choose a prompt argument or --prompt-file")
    if args.prompt_file and str(args.prompt_file) != "-":
        prompt = args.prompt_file.read_text(encoding="utf-8")
    elif args.prompt is not None:
        prompt = args.prompt
    else:
        if sys.stdin.isatty():
            print("Type your complete prompt, then press Ctrl-D:", file=sys.stderr)
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise ValueError("Prompt cannot be empty")
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError("--cwd must be an existing directory")
    ZoneInfo(args.timezone)
    if args.idle_minutes and sys.platform != "darwin":
        raise ValueError("--idle-minutes currently requires macOS; use --window on other platforms")
    if not 0 < args.min_remaining <= 100:
        raise ValueError("--min-remaining must be greater than 0 and at most 100")
    if args.after_reset:
        due = max(time.time(), codex.five_hour_reset(codex.snapshot(codex.executable(args.codex)), args.limit_id)) + 30
    else:
        due = parse_time(args.at)
    spec = {"prompt": prompt, "cwd": str(cwd), "sandbox": args.sandbox,
            "model": args.model, "effort": args.effort, "window": args.window, "timezone": args.timezone,
            "idle_minutes": args.idle_minutes, "min_remaining": args.min_remaining,
            "limit_id": args.limit_id, "timeout": args.timeout,
            "schedule": "after-reset" if args.after_reset else "at"}
    job_id = store.add(spec, due)
    print(f"Scheduled {job_id} for {timestamp(due)} ({args.sandbox})")
    print("Run `schedulex worker` or install the background worker with `schedulex service install`.")


def service(args, store):
    if sys.platform != "darwin":
        raise ValueError("Background service installation supports macOS; run `schedulex worker` on Linux")
    label = "local.schedulex.worker"
    target = f"gui/{os.getuid()}/{label}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if args.action == "status":
        result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
        print(result.stdout if result.returncode == 0 else "Schedulex background service is not loaded")
    elif args.action == "uninstall":
        result = subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True)
        if result.returncode:
            status = subprocess.run(["launchctl", "print", target], capture_output=True)
            if status.returncode == 0:
                raise ValueError(f"Service is still loaded: {result.stderr.strip()}")
        plist.unlink(missing_ok=True)
        print("Background service removed; queued jobs and results preserved")
    else:
        if plist.exists():
            raise ValueError("Service already installed; uninstall it before changing its queue or binary")
        binary = codex.executable(args.codex)
        package_root = str(Path(__file__).resolve().parent.parent)
        document = {
            "Label": label,
            "ProgramArguments": [sys.executable, "-m", "schedulex", "--state-dir", str(store.root),
                                 "--codex", binary, "worker"],
            "WorkingDirectory": package_root,
            "EnvironmentVariables": {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
                "PYTHONPATH": package_root,
                **({"CODEX_HOME": os.environ["CODEX_HOME"]} if "CODEX_HOME" in os.environ else {}),
            },
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
            "StandardOutPath": str(store.root / "worker.log"),
            "StandardErrorPath": str(store.root / "worker-error.log"),
        }
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as stream:
            plistlib.dump(document, stream)
        os.chmod(plist, 0o600)
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                                capture_output=True, text=True)
        if result.returncode:
            plist.unlink(missing_ok=True)
            raise ValueError(f"launchctl could not load the service: {result.stderr.strip()}")
        print(f"Installed background worker for {store.root}")


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "menubar":
            if sys.platform != "darwin" or not args.app.is_dir():
                raise ValueError("Install the macOS app using install.command first")
            subprocess.run(["open", str(args.app)], check=True)
            return
        if args.command == "limits":
            value = codex.snapshot(codex.executable(args.codex))
            print(json.dumps(value, indent=2))
            return
        if args.command == "models":
            print(json.dumps(codex.catalog(codex.executable(args.codex)), indent=2))
            return
        store = Store(args.state_dir)
        if args.command == "dashboard":
            from .dashboard import dashboard
            print(json.dumps(dashboard(store, args.codex), ensure_ascii=False))
        elif args.command == "add":
            add_job(args, store)
        elif args.command == "list":
            for row in store.jobs():
                print(f"{row['id']}  {row['status']:12}  {timestamp(row['due'])}  {row['note']}")
        elif args.command == "show":
            row = store.get(args.id)
            row["spec"] = json.loads(row["spec"])
            row["runs"] = str(store.root / "runs" / args.id)
            print(json.dumps(row, indent=2, ensure_ascii=False))
        elif args.command == "cancel":
            store.cancel(args.id)
            print(f"Cancelled {args.id}")
        elif args.command == "worker":
            run(store, codex.executable(args.codex), args.once, args.interval)
        elif args.command == "service":
            service(args, store)
    except (codex.CodexError, ValueError, OSError, KeyError) as error:
        print(f"schedulex: {error}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if store:
            store.db.close()


if __name__ == "__main__":
    main()
