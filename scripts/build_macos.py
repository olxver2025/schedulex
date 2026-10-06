"""Build an ad-hoc signed native menu bar bundle for this Mac."""
import os
from pathlib import Path
import platform
import plistlib
import subprocess


ROOT = Path(__file__).resolve().parent.parent


def build(destination=None):
    destination = Path(destination or ROOT / "dist" / "Schedulex.app")
    contents = destination / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(exist_ok=True)
    architecture = platform.machine()
    # CLT's preview SDK 27 can omit SwiftUIMacros. Prefer the installed stable
    # SDK for this macOS 14-targeted app, or allow an explicit SDK override.
    stable_sdk = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk")
    sdk = os.environ.get("SCHEDULEX_MACOS_SDK") or (str(stable_sdk) if stable_sdk.exists() else None)
    subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-O", *( ["-sdk", sdk] if sdk else [] ), "-target",
                    f"{architecture}-apple-macosx14.0", "-module-cache-path", "/tmp/schedulex-swift-cache",
                    str(ROOT / "macos" / "Schedulex.swift"), "-o", str(contents / "MacOS" / "Schedulex")], check=True)
    info = {"CFBundleExecutable": "Schedulex", "CFBundleIdentifier": "local.schedulex.menubar",
            "CFBundleName": "Schedulex", "CFBundleDisplayName": "Schedulex", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "2", "LSMinimumSystemVersion": "14.0",
            "LSUIElement": True, "NSHighResolutionCapable": True, "SchedulexArchitecture": architecture}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--sign", "-", str(destination)], check=True)
    return destination


if __name__ == "__main__":
    print(build())
