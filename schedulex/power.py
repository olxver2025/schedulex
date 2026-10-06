"""macOS scheduled-wake helper client and idle-sleep guard."""
import json
import filecmp
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile


class PowerError(ValueError):
    pass


def socket_path(uid=None):
    uid = os.getuid() if uid is None else uid
    return f"/var/run/local.schedulex.power.{uid}.sock"


def request(action, job_id, due=None, timeout=8):
    message = {"action": action, "job": job_id}
    if due is not None:
        message["due"] = float(due)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(socket_path())
        client.sendall(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
        response = bytearray()
        while len(response) < 4096:
            chunk = client.recv(1024)
            if not chunk:
                break
            response.extend(chunk)
            if b"\n" in chunk:
                break
    except OSError as error:
        raise PowerError(f"Could not reach Schedulex's macOS wake service: {error}") from error
    finally:
        client.close()
    try:
        result = json.loads(bytes(response).split(b"\n", 1)[0])
    except (ValueError, TypeError) as error:
        raise PowerError("Schedulex's macOS wake service returned an invalid response") from error
    if not result.get("ok"):
        raise PowerError(result.get("error") or "The macOS wake request failed")
    return True


def schedule_wake(job_id, due):
    if sys.platform == "darwin":
        request("schedule", job_id, due)


def cancel_wake(job_id, quiet=False):
    if sys.platform != "darwin":
        return
    try:
        request("cancel", job_id)
    except PowerError:
        if not quiet:
            raise


def cancel_all_wakes():
    if sys.platform == "darwin":
        request("cancel-all", "")


def retry_wake(job_id, due):
    """Update the one-shot wake when a pending job must wait after waking."""
    if sys.platform != "darwin":
        return
    request("schedule", job_id, due)


def helper_label(uid=None):
    uid = os.getuid() if uid is None else uid
    return f"local.schedulex.power.{uid}"


def helper_paths(uid=None):
    label = helper_label(uid)
    return (f"/Library/LaunchDaemons/{label}.plist",
            f"/Library/PrivilegedHelperTools/{label}",
            f"system/{label}")


def find_helper_binary():
    candidates = []
    configured = os.environ.get("SCHEDULEX_POWER_HELPER")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend((
        Path.home() / "Applications/Schedulex.app/Contents/MacOS/SchedulexPowerHelper",
        Path(__file__).resolve().parents[1] / "dist/Schedulex.app/Contents/MacOS/SchedulexPowerHelper",
        Path(__file__).resolve().parents[1] / "macos/Schedulex.app/Contents/MacOS/SchedulexPowerHelper",
    ))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise PowerError("Timed wake support is missing; rerun the Schedulex macOS installer")


def _run_admin_shell(script_path, prompt):
    command = "/bin/sh " + shlex.quote(str(script_path))
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    apple_script = (f'do shell script "{escaped}" with administrator privileges '
                    f'with prompt "{prompt}"')
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", apple_script],
                                capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired as error:
        raise PowerError("Timed out waiting for macOS administrator authorization; unlock the Mac and retry") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Administrator authorization was not granted"
        raise PowerError(detail)


def install_helper(binary, uid=None):
    """Install the small root service that owns pmset wake events for this user."""
    if sys.platform != "darwin":
        raise PowerError("Scheduled wake support is available only on macOS")
    uid = os.getuid() if uid is None else uid
    label = helper_label(uid)
    plist_path, helper_path, target = helper_paths(uid)
    if (Path(plist_path).is_file() and Path(helper_path).is_file() and
            filecmp.cmp(binary, helper_path, shallow=False)):
        active = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True)
        if active.returncode == 0:
            return
    socket_name = socket_path(uid)
    document = {
        "Label": label,
        "ProgramArguments": [helper_path, str(uid)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "Sockets": {"Listener": {
            "SockPathName": socket_name,
            "SockPathMode": 0o666,
            "SockFamily": "Unix",
            "SockType": "Stream",
        }},
        "StandardOutPath": "/var/log/schedulex-power.log",
        "StandardErrorPath": "/var/log/schedulex-power-error.log",
        "Umask": 0o077,
    }
    with tempfile.TemporaryDirectory(prefix="schedulex-power-", dir="/private/tmp") as staging:
        stage = Path(staging)
        staged_binary = stage / "SchedulexPowerHelper"
        staged_plist = stage / f"{label}.plist"
        shutil.copyfile(binary, staged_binary)
        staged_binary.chmod(0o755)
        staged_plist.write_bytes(plistlib.dumps(document))
        staged_plist.chmod(0o644)
        script = stage / "install.sh"
        lines = [
            "set -e",
            f"/bin/launchctl bootout {shlex.quote(target)} >/dev/null 2>&1 || true",
            "/usr/bin/install -d -o root -g wheel -m 755 /Library/PrivilegedHelperTools",
            f"/usr/bin/install -o root -g wheel -m 755 {shlex.quote(str(staged_binary))} {shlex.quote(helper_path)}",
            f"/usr/bin/install -o root -g wheel -m 644 {shlex.quote(str(staged_plist))} {shlex.quote(plist_path)}",
            f"/bin/launchctl enable {shlex.quote(target)}",
            f"/bin/launchctl bootstrap system {shlex.quote(plist_path)}",
        ]
        script.write_text("\n".join(lines) + "\n")
        script.chmod(0o755)
        _run_admin_shell(script, "Schedulex needs administrator access to install scheduled wake support.")


def remove_helper(uid=None):
    if sys.platform != "darwin":
        return
    uid = os.getuid() if uid is None else uid
    plist_path, helper_path, target = helper_paths(uid)
    if not Path(plist_path).exists() and not Path(helper_path).exists():
        return
    label = helper_label(uid)
    socket_name = socket_path(uid)
    registry_path = f"/Library/Application Support/Schedulex/Power/{uid}.plist"
    with tempfile.TemporaryDirectory(prefix="schedulex-power-remove-", dir="/private/tmp") as staging:
        script = Path(staging) / "remove.sh"
        script.write_text("\n".join((
            "set -e",
            f"/bin/launchctl bootout {shlex.quote(target)} >/dev/null 2>&1 || true",
            f"/bin/rm -f {shlex.quote(plist_path)} {shlex.quote(helper_path)} {shlex.quote(socket_name)}",
            f"/bin/rm -f {shlex.quote(registry_path)}",
        )) + "\n")
        script.chmod(0o755)
        _run_admin_shell(script, "Schedulex needs administrator access to remove scheduled wake support.")
