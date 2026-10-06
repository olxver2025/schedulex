#!/usr/bin/env python3
"""Per-user installer. No pip, root access, or Python runtime dependencies."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
MARKER = "schedulex-install.json"


def locate_codex(override):
    candidates = [override] if override else [shutil.which("codex"),
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        "/Applications/Codex.app/Contents/Resources/codex"]
    for value in candidates:
        if value and Path(value).is_file() and os.access(value, os.X_OK):
            return str(Path(value).resolve())
    raise ValueError("Codex CLI not found. Install Codex or use --codex /absolute/path/to/codex")


def launchctl(*args, check=True):
    return subprocess.run(["launchctl", *args], check=check, capture_output=True, text=True)


def unload_agent(target):
    launchctl("bootout", target, check=False)
    # bootout can return before the old service disappears from the domain.
    deadline = time.monotonic() + 10
    while launchctl("print", target, check=False).returncode == 0:
        if time.monotonic() >= deadline:
            raise ValueError(f"Timed out stopping {target}; retry installation after it exits")
        time.sleep(0.2)


def install(args):
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11+ is required")
    if sys.platform != "darwin":
        raise ValueError("This installer is for macOS; Linux users can pip install . for both CLI aliases")
    if int(platform.mac_ver()[0].split(".")[0]) < 14:
        raise ValueError("The native menu bar app requires macOS 14 or newer")
    binary = locate_codex(args.codex)
    prefix = args.prefix.expanduser().resolve()
    bin_dir = args.bin_dir.expanduser().resolve()
    app_dir = args.app_dir.expanduser().resolve()
    state = args.state_dir.expanduser().resolve()
    target_app = app_dir / "Schedulex.app"
    # Refuse to replace unrelated files; reinstallation only owns our marked paths.
    if prefix.exists() and not (prefix / MARKER).is_file():
        raise ValueError(f"Install directory already exists without a Schedulex marker: {prefix}")
    for name in ("schx", "schedulex"):
        wrapper = bin_dir / name
        if wrapper.exists() and "# Schedulex managed launcher" not in wrapper.read_text():
            raise ValueError(f"Refusing to replace an unrelated command: {wrapper}")
    if target_app.exists():
        info = target_app / "Contents/Info.plist"
        if not info.exists() or plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") != "local.schedulex.menubar":
            raise ValueError(f"Refusing to replace an unrelated app: {target_app}")
    bundle = ROOT / "macos" / "Schedulex.app"
    if not bundle.exists():
        bundle = ROOT / "dist" / "Schedulex.app"
    bundle_info = bundle / "Contents/Info.plist"
    if not bundle_info.exists() or plistlib.loads(bundle_info.read_bytes()).get("SchedulexArchitecture") != platform.machine():
        from scripts.build_macos import build
        bundle = build()
    # Prepare payload before replacing an existing install.
    prefix.parent.mkdir(parents=True, exist_ok=True)
    stage = prefix.with_name(prefix.name + ".installing")
    if stage.exists():
        raise ValueError(f"An installation staging directory already exists: {stage}")
    stage.mkdir(mode=0o700)
    try:
        shutil.copytree(ROOT / "schedulex", stage / "schedulex", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "README.md", stage / "README.md")
        configuration = {"python": sys.executable, "source": str(prefix), "codex": binary,
                         "stateDir": str(state), "path": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
                         "codexHome": os.environ.get("CODEX_HOME")}
        (stage / MARKER).write_text(json.dumps(configuration, indent=2))
        backup = prefix.with_name(prefix.name + ".previous")
        if backup.exists():
            raise ValueError(f"Previous installation backup exists: {backup}")
        if prefix.exists():
            # Stop our worker before replacing its runtime. Keep the queue and history.
            if not args.no_start:
                env = dict(os.environ, PYTHONPATH=str(prefix))
                subprocess.run([sys.executable, "-P", "-m", "schedulex", "--state-dir", str(state),
                                "service", "uninstall"], env=env, check=True)
            prefix.rename(backup)
        stage.rename(prefix)
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper_text = ("#!/bin/sh\n# Schedulex managed launcher\n"
                    f"export PYTHONPATH={shlex.quote(str(prefix))}\n"
                    "if [ -z \"${SCHEDULEX_HOME:-}\" ]; then\n"
                    f"  export SCHEDULEX_HOME={shlex.quote(str(state))}\nfi\n"
                    f"exec {shlex.quote(sys.executable)} -P -m schedulex --codex {shlex.quote(binary)} \"$@\"\n")
    for name in ("schx", "schedulex"):
        path = bin_dir / name
        path.write_text(wrapper_text)
        path.chmod(0o755)
    app_dir.mkdir(parents=True, exist_ok=True)
    if target_app.exists():
        shutil.rmtree(target_app)
    shutil.copytree(bundle, target_app)
    (target_app / "Contents/Resources").mkdir(parents=True, exist_ok=True)
    (target_app / "Contents/Resources/configuration.json").write_text(json.dumps(configuration))
    subprocess.run(["codesign", "--force", "--sign", "-", str(target_app)], check=True)
    if not args.no_shell:
        shell_file = Path.home() / ".zshrc"
        existing = shell_file.read_text() if shell_file.exists() else ""
        line = f"export PATH={shlex.quote(str(bin_dir))}:\"$PATH\" # Schedulex CLI\n"
        if line.strip() not in existing:
            with shell_file.open("a") as stream:
                stream.write("\n" + line)
    if not args.no_start:
        env = dict(os.environ, PYTHONPATH=str(prefix))
        subprocess.run([sys.executable, "-P", "-m", "schedulex", "--state-dir", str(state),
                        "--codex", binary, "service", "install"], env=env, check=True)
        agent = Path.home() / "Library/LaunchAgents/local.schedulex.menubar.plist"
        agent.parent.mkdir(parents=True, exist_ok=True)
        target = f"gui/{os.getuid()}/local.schedulex.menubar"
        unload_agent(target)
        agent.write_bytes(plistlib.dumps({"Label": "local.schedulex.menubar",
                    "ProgramArguments": [str(target_app / "Contents/MacOS/Schedulex")],
                    "RunAtLoad": True, "KeepAlive": False,
                    "StandardErrorPath": str(state / "menubar-error.log")}))
        agent.chmod(0o600)
        launchctl("bootstrap", f"gui/{os.getuid()}", str(agent))
    print(f"Installed schx and schedulex in {bin_dir}")
    print(f"Installed menu bar app: {target_app}")
    print("Open a new Terminal, then try: schx list")
    if args.no_start:
        print("Background services were not started (--no-start)")


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Install Schedulex CLI and menu bar app for the current macOS user")
    p.add_argument("--prefix", type=Path, default=Path.home() / "Library/Application Support/Schedulex")
    p.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    p.add_argument("--app-dir", type=Path, default=Path.home() / "Applications")
    p.add_argument("--state-dir", type=Path, default=Path.home() / ".local/share/schedulex")
    p.add_argument("--codex")
    p.add_argument("--no-start", action="store_true", help="Do not start or register background services")
    p.add_argument("--no-shell", action="store_true", help="Do not add the command directory to .zshrc")
    args = p.parse_args()
    try:
        install(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Installation failed: {error}")


if __name__ == "__main__":
    main()
