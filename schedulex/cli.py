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
from . import power
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
    p = argparse.ArgumentParser(description="Schedule prompts for local Codex runs or Codex Cloud.")
    p.add_argument("--state-dir", default=os.environ.get("SCHEDULEX_HOME", "~/.local/share/schedulex"))
    p.add_argument("--codex", default=os.environ.get("SCHEDULEX_CODEX", "codex"), help="Codex executable")
    commands = p.add_subparsers(dest="command", required=True)
    for command in ("add", "edit"):
        editing = command == "edit"
        add = commands.add_parser(command, help="Edit/reschedule a pending task" if editing else
                                  "Schedule a prompt; omission of prompt reads stdin")
        def option_default(value):
            return argparse.SUPPRESS if editing else value
        if editing:
            add.add_argument("id")
            add.add_argument("--revision", type=int, help="Reject edits if the task has changed")
        add.add_argument("prompt", nargs="?", default=option_default(None), help="Complete prompt")
        add.add_argument("--prompt-file", type=Path, default=option_default(None), help="UTF-8 file; '-' reads stdin")
        when = add.add_mutually_exclusive_group(required=not editing)
        when.add_argument("--at", default=option_default(None), help="ISO time with offset, or +30m / +2h / +1d")
        when.add_argument("--after-reset", action="store_true", default=option_default(False), help="Next reported five-hour reset")
        when.add_argument("--after-weekly-reset", action="store_true", default=option_default(False), help="Next reported weekly limit reset")
        when.add_argument("--repeat-reset", choices=("five-hour", "weekly", "none") if editing else ("five-hour", "weekly"),
                          default=option_default(None), help="Repeat after each reported reset; no clock-based intervals")
        add.add_argument("--project", "--cwd", dest="cwd", type=Path, default=option_default(Path.cwd()))
        add.add_argument("--cloud-env", default=option_default(None))
        add.add_argument("--window", type=window, default=option_default(None))
        add.add_argument("--timezone", default=option_default("Europe/London"))
        add.add_argument("--idle-minutes", type=positive, default=option_default(0))
        add.add_argument("--sandbox", choices=("read-only", "workspace-write"), default=option_default("read-only"))
        add.add_argument("--model", default=option_default(None))
        add.add_argument("--effort", "--reasoning-effort", default=option_default(None))
        add.add_argument("--limit-id", default=option_default("codex"))
        add.add_argument("--min-remaining", type=float, default=option_default(1))
        add.add_argument("--timeout", type=positive, default=option_default(7200))
        if editing:
            for field in ("window", "idle", "model", "effort"):
                add.add_argument(f"--clear-{field}", action="store_true")
            add.add_argument("--local", action="store_true", help="Switch from Cloud to a local project")
    commands.add_parser("list", help="Show queue and job states")
    show = commands.add_parser("show", help="Show complete prompt, status and run paths")
    show.add_argument("id")
    cancel = commands.add_parser("cancel", help="Cancel a pending job")
    cancel.add_argument("id")
    interrupt = commands.add_parser("interrupt", help="Interrupt a running local Codex task")
    interrupt.add_argument("id")
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


def save_job(args, store):
    editing = args.command == "edit"
    row = store.get(args.id) if editing else None
    if row and row["status"] != "pending":
        raise ValueError("Only pending tasks can be edited or rescheduled")
    spec = json.loads(row["spec"]) if row else {}
    supplied = vars(args)
    prompt_arg = supplied.get("prompt")
    prompt_file = supplied.get("prompt_file")
    if prompt_arg is not None and prompt_file is not None:
        raise ValueError("Choose a prompt argument or --prompt-file")
    if prompt_file and str(prompt_file) != "-":
        spec["prompt"] = prompt_file.read_text(encoding="utf-8")
    elif prompt_arg is not None:
        spec["prompt"] = prompt_arg
    elif prompt_file or not editing:
        if sys.stdin.isatty():
            print("Type your complete prompt, then press Ctrl-D:", file=sys.stderr)
        spec["prompt"] = sys.stdin.read()
    if not spec.get("prompt", "").strip():
        raise ValueError("Prompt cannot be empty")
    for field in ("sandbox", "model", "effort", "window", "timezone", "idle_minutes",
                  "min_remaining", "limit_id", "timeout", "cloud_env"):
        if field in supplied:
            spec[field] = supplied[field]
    for field, target in (("window", "window"), ("idle", "idle_minutes"), ("model", "model"), ("effort", "effort")):
        if supplied.get("clear_" + field):
            if target in supplied:
                raise ValueError(f"Choose --clear-{field} or an override, not both")
            spec[target] = 0 if field == "idle" else None
    if supplied.get("local"):
        if supplied.get("cloud_env"):
            raise ValueError("Choose --local or --cloud-env")
        spec["cloud_env"] = None
    cloud_env = spec.get("cloud_env")
    if cloud_env is not None:
        cloud_env = cloud_env.strip()
        if not cloud_env:
            raise ValueError("--cloud-env cannot be empty")
    spec["cloud_env"] = cloud_env
    spec["destination"] = "cloud" if cloud_env else "local"
    if "cwd" in supplied:
        spec["cwd"] = str(supplied["cwd"].expanduser().resolve())
    if not cloud_env:
        if not spec.get("cwd") or not Path(spec["cwd"]).is_dir():
            raise ValueError("--project/--cwd must be an existing directory")
    else:
        spec["cwd"] = None
    ZoneInfo(spec["timezone"])
    if spec["idle_minutes"] and sys.platform != "darwin":
        raise ValueError("--idle-minutes currently requires macOS; use --window on other platforms")
    if not 0 < spec["min_remaining"] <= 100:
        raise ValueError("--min-remaining must be greater than 0 and at most 100")
    due = row["due"] if row else None
    recurrence = supplied.get("repeat_reset")
    if (editing and recurrence is None and not supplied.get("at") and not supplied.get("after_reset")
            and not supplied.get("after_weekly_reset") and spec.get("recurrence")
            and json.loads(row["spec"]).get("limit_id") != spec["limit_id"]):
        recurrence = spec["recurrence"]
    if recurrence in ("five-hour", "weekly"):
        # Editing the same series preserves its current occurrence and reset identity.
        same = (editing and spec.get("recurrence") == recurrence and
                json.loads(row["spec"]).get("limit_id") == spec["limit_id"])
        spec["recurrence"] = recurrence
        spec["schedule"] = "repeat-reset"
        if not same:
            reset = codex.usage_reset(codex.snapshot(codex.executable(args.codex)), spec["limit_id"], recurrence)
            if reset <= time.time():
                raise ValueError("Codex has not reported a future reset yet; try again after refreshing usage")
            spec["reset_at"] = reset
            spec.pop("recurrence_after", None)
            due = reset + 30
    elif supplied.get("after_reset") or supplied.get("after_weekly_reset"):
        spec["recurrence"] = None
        weekly = supplied.get("after_weekly_reset")
        spec["schedule"] = "after-weekly-reset" if weekly else "after-reset"
        spec["reset_at"] = codex.usage_reset(codex.snapshot(codex.executable(args.codex)), spec["limit_id"],
                                            "weekly" if weekly else "five-hour")
        due = max(time.time(), spec["reset_at"]) + 30
    elif supplied.get("at"):
        spec["recurrence"] = None
        spec["schedule"] = "at"
        spec["reset_at"] = None
        due = parse_time(args.at)
    elif recurrence == "none":
        spec["recurrence"] = None
        spec["schedule"] = "at"
        spec["reset_at"] = None
    def wake():
        if sys.platform == "darwin":
            try:
                power.schedule_wake(job_id, max(due, time.time() + 15))
            except power.PowerError as error:
                raise ValueError(f"Could not schedule the Mac wake; task was not saved. {error}") from error
    if editing:
        job_id = args.id
        revision = supplied.get("revision")
        if revision is None:
            revision = row["revision"]
        store.update_pending(job_id, spec, due, revision, before_commit=wake)
    else:
        job_id = store.add(spec, due)
        try:
            wake()
        except ValueError:
            store.remove_pending(job_id)
            raise
    destination = f"Codex Cloud environment {cloud_env}" if cloud_env else spec["sandbox"]
    print(f"{'Updated' if editing else 'Scheduled'} {job_id} for {timestamp(due)} ({destination})")
    if not editing:
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
        power_target = power.helper_paths()[2]
        helper = subprocess.run(["launchctl", "print", power_target], capture_output=True, text=True)
        print(helper.stdout if helper.returncode == 0 else "Schedulex timed-wake helper is not installed")
    elif args.action == "uninstall":
        if Path(power.helper_paths()[0]).is_file():
            power.cancel_all_wakes()
            power.remove_helper()
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
        power.install_helper(power.find_helper_binary())
        for row in store.jobs(pending=True):
            try:
                power.schedule_wake(row["id"], max(row["due"], time.time() + 15))
            except power.PowerError as error:
                raise ValueError(f"Could not schedule wake for existing job {row['id']}: {error}") from error
        package_root = str(Path(__file__).resolve().parent.parent)
        document = {
            "Label": label,
            "ProgramArguments": [sys.executable, "-m", "schedulex", "--state-dir", str(store.root),
                                 "--codex", binary, "worker"],
            "WorkingDirectory": package_root,
            "EnvironmentVariables": {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
                "PYTHONPATH": package_root,
                "SCHEDULEX_POWER_SOCKET": power.socket_path(),
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
        elif args.command in ("add", "edit"):
            save_job(args, store)
        elif args.command == "list":
            for row in store.jobs():
                print(f"{row['id']}  {row['status']:12}  {timestamp(row['due'])}  {row['note']}")
        elif args.command == "show":
            row = store.get(args.id)
            row["spec"] = json.loads(row["spec"])
            row["runs"] = str(store.root / "runs" / args.id)
            print(json.dumps(row, indent=2, ensure_ascii=False))
        elif args.command == "interrupt":
            row = store.get(args.id)
            if row["status"] != "running" or not row.get("thread_id") or not row.get("turn_id"):
                raise ValueError("This task does not have a running local Codex turn")
            codex.interrupt(codex.executable(args.codex), row["thread_id"], row["turn_id"])
            print("Interruption requested; the worker will record the final status")
        elif args.command == "cancel":
            store.cancel(args.id)
            try:
                power.cancel_wake(args.id)
            except power.PowerError as error:
                print(f"Cancelled {args.id}, but the Mac may still wake once: {error}", file=sys.stderr)
            else:
                print(f"Cancelled {args.id}")
        elif args.command == "worker":
            run(store, codex.executable(args.codex), args.once, args.interval)
        elif args.command == "service":
            service(args, store)
    except (codex.CodexError, ValueError, OSError, KeyError, TimeoutError) as error:
        print(f"schedulex: {error}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if store:
            store.db.close()


if __name__ == "__main__":
    main()
