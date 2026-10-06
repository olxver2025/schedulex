"""Create a double-clickable macOS installer archive with a prebuilt native app."""
from pathlib import Path
import platform
import zipfile

from build_macos import build, ROOT


def main():
    app = build()
    target = ROOT / "dist" / f"Schedulex-0.3.0-macos-{platform.machine()}-installer.zip"
    files = [ROOT / "install.command", ROOT / "install.py", ROOT / "README.md", ROOT / "macos/Schedulex.swift"]
    files += list((ROOT / "schedulex").glob("*.py"))
    files += list((ROOT / "scripts").glob("*.py"))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, Path("Schedulex Installer") / path.relative_to(ROOT))
        for path in app.rglob("*"):
            if path.is_file():
                archive.write(path, Path("Schedulex Installer/macos/Schedulex.app") / path.relative_to(app))
    print(target)


if __name__ == "__main__":
    main()
