import contextlib
import importlib.util
import io
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("schedulex_installer", SOURCE / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / "source"
        source.mkdir()
        shutil.copytree(SOURCE / "schedulex", source / "schedulex")
        (source / "README.md").write_text("test")
        app = source / "macos/Schedulex.app/Contents"
        (app / "Resources").mkdir(parents=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "SchedulexArchitecture": platform.machine(), "CFBundleIdentifier": "local.schedulex.menubar"}))
        self.args = types.SimpleNamespace(prefix=self.root / "installed runtime", bin_dir=self.root / "bin",
                    app_dir=self.root / "apps", state_dir=self.root / "state $(not-executed)",
                    codex="/usr/bin/true", no_start=True, no_shell=True)
        self.patch = patch.object(installer, "ROOT", source)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def install(self):
        with patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(installer.sys, "platform", "darwin"), \
                patch.object(installer.platform, "mac_ver", return_value=("14.0", (), "")), \
                contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.args)

    def test_both_real_launchers_and_quoting(self):
        self.install()
        for name in ("schx", "schedulex"):
            result = subprocess.run([str(self.args.bin_dir / name), "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("menubar", result.stdout)
            result = subprocess.run([str(self.args.bin_dir / name), "list"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.args.state_dir / "jobs.sqlite3").exists())
        self.assertTrue((self.args.app_dir / "Schedulex.app/Contents/Resources/configuration.json").exists())

    def test_reinstall_and_unrelated_file_protection(self):
        self.install()
        self.install()
        (self.args.bin_dir / "schx").write_text("unrelated")
        with self.assertRaisesRegex(ValueError, "unrelated command"):
            self.install()

    def test_reinstall_waits_for_old_menu_agent_to_unload(self):
        results = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                   subprocess.CompletedProcess([], 1)]
        with patch.object(installer, "launchctl", side_effect=results) as control, \
                patch.object(installer.time, "sleep") as sleep:
            installer.unload_agent("gui/501/test")
        sleep.assert_called_once_with(0.2)
        self.assertEqual(control.call_count, 3)

    def test_installer_handles_archive_without_empty_resources_directory(self):
        (installer.ROOT / "macos/Schedulex.app/Contents/Resources").rmdir()
        self.install()
        self.assertTrue((self.args.app_dir / "Schedulex.app/Contents/Resources/configuration.json").exists())


if __name__ == "__main__":
    unittest.main()
