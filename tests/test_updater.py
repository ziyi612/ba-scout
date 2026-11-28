from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import warnings
import zipfile

from ba_scout import updater

ROOT = Path(__file__).resolve().parents[1]
_WINDOWS_POWERSHELL = shutil.which("powershell.exe")
_BASE_FILES = {
    "BA-Scout/BA-Scout.exe": b"new program",
    "BA-Scout/data/prep-layout-default.json": b"new default",
    "BA-Scout/_internal/base_library.zip": b"new library",
    "BA-Scout/_internal/ba_scout/install_update.ps1": b"future helper",
    "BA-Scout/docs/USER_GUIDE.zh-CN.txt": b"documentation",
}


@contextmanager
def temp_root():
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as directory:
        yield Path(directory)


def zip_bytes(extras=()) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in tuple(_BASE_FILES.items()) + tuple(extras):
            archive.writestr(name, content)
    return output.getvalue()


class ReleaseTests(unittest.TestCase):
    def metadata(self, version="0.12.0"):
        return {
            "tag_name": f"v{version}", "draft": False, "prerelease": False, "body": "发布记录",
            "assets": [{
                "name": f"ba-scout-{version}-windows-x64.zip", "state": "uploaded",
                "size": 1200, "browser_download_url": updater._asset_url(version),
                "digest": "sha256:" + "12" * 32,
            }],
        }

    def check(self, metadata, current="0.11.1"):
        with patch.object(updater, "_open", return_value=io.BytesIO(json.dumps(metadata).encode())) as request:
            release = updater.check_update(current)
            self.assertEqual(request.call_args.args, (updater.LATEST_RELEASE,))
            self.assertEqual(request.call_args.kwargs, {"metadata": True})
            return release

    def test_only_new_stable_versions_become_update_candidates(self):
        release = self.check(self.metadata("0.12.10"), current="0.12.9")
        self.assertEqual((release.version, release.notes, release.sha256), ("0.12.10", "发布记录", "12" * 32))
        self.assertIsNone(self.check(self.metadata(), current="0.12.0"))
        self.assertIsNone(self.check(self.metadata(), current="1.0.0"))
        with self.assertRaises(updater.UpdateError):
            self.check({**self.metadata(), "prerelease": True})
        with self.assertRaises(updater.UpdateError):
            self.check(self.metadata("0.12.0-rc.1"))

    def test_wrong_release_asset_and_absent_digest_are_rejected(self):
        for change in (
            {"digest": None},
            {"size": True},
            {"browser_download_url": "https://example.com/archive.zip"},
            {"browser_download_url": "http://github.com/archive.zip"},
            {"state": "new"},
        ):
            with self.subTest(change=change):
                metadata = self.metadata()
                metadata["assets"][0].update(change)
                with self.assertRaises(updater.UpdateError):
                    self.check(metadata)


class StageTests(unittest.TestCase):
    def stage(self, root, content, digest=None):
        release = updater.Release("0.12.1", "", updater._asset_url("0.12.1"), len(content),
                                  digest or hashlib.sha256(content).hexdigest())
        with patch.object(updater, "install_supported", return_value=True):
            with patch.object(updater, "_open", return_value=io.BytesIO(content)):
                return updater.stage_update(root, release)

    def test_stage_checks_digest_and_does_not_touch_current_files(self):
        with temp_root() as root:
            config = root / "config.json"
            config.write_text("personal")
            executable = root / "BA-Scout.exe"
            executable.write_bytes(b"previous app")
            content = zip_bytes()
            with self.assertRaisesRegex(updater.UpdateError, "SHA-256"):
                self.stage(root, content, "00" * 32)
            self.assertEqual(list((root / "tmp").iterdir()), [])
            staged = self.stage(root, content)
            self.assertEqual(staged.parent, root / "tmp")
            payload = staged / "payload"
            self.assertEqual((payload / "BA-Scout.exe").read_bytes(), b"new program")
            self.assertEqual((payload / "data" / "prep-layout-default.json").read_bytes(), b"new default")
            self.assertFalse((payload / "docs").exists())
            self.assertEqual(config.read_text(), "personal")
            self.assertEqual(executable.read_bytes(), b"previous app")

    def test_zip_rejects_unsafe_or_conflicting_paths_before_extraction(self):
        symlink = zipfile.ZipInfo("BA-Scout/_internal/linked")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        reparse_point = zipfile.ZipInfo("BA-Scout/_internal/reparse")
        reparse_point.external_attr = 0x400
        cases = (
            (("BA-Scout/../../outside.txt", b"",),),
            (("BA-Scout/_internal/COn.txt", b"",),),
            (("BA-Scout/_internal/case.", b"",),),
            (("BA-Scout/_internal/base_library.zip:extra", b"",),),
            (("BA-Scout/_internal/base_LIBRARY.zip", b"",),),
            (("BA-Scout/_internal", b"",),),
            (("BA-Scout/config.json", b"",),),
            ((symlink, b"x"),),
            ((reparse_point, b"x"),),
            (("BA-Scout/_internal/folder/", b""), ("BA-Scout/_internal/folder/", b"")),
        )
        for extras in cases:
            with self.subTest(extras=extras), temp_root() as root:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    content = zip_bytes(extras)
                with self.assertRaises(updater.UpdateError):
                    self.stage(root, content)
                self.assertEqual(list((root / "tmp").iterdir()), [])

    def test_zip_parser_rejects_unsanitized_windows_separator(self):
        info = zipfile.ZipInfo(r"BA-Scout/_internal\path")
        with self.assertRaises(updater.UpdateError):
            updater._zip_parts(info)

    def test_unpacked_source_cannot_start_installation(self):
        with temp_root() as root:
            self.assertFalse(updater.install_supported(root))
            with self.assertRaises(updater.UpdateError):
                updater.launch_install(root, root / "tmp" / "stage")


@unittest.skipUnless(sys.platform == "win32" and _WINDOWS_POWERSHELL, "Windows PowerShell is required")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(temp_root()) / "便携 [安装]"
        self.stage = self.root / "tmp" / "ba-scout-update-test"
        self.payload = self.stage / "payload"
        for root, version in ((self.root, "old"), (self.payload, "new")):
            (root / "_internal").mkdir(parents=True)
            (root / "_internal" / "library.txt").write_text(version, encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "prep-layout-default.json").write_text(version, encoding="utf-8")
            (root / "BA-Scout.exe").write_text(version, encoding="utf-8")
        (self.root / "config.json").write_text("personal")
        (self.root / "data" / "encounters.sqlite3").write_text("personal")
        (self.root / "exports").mkdir()
        (self.root / "exports" / "report.txt").write_text("personal")
        (self.root / "_internal" / "old-only.txt").write_text("old")
        name = "Local\\BrokenArrowScout_UpdateTest_" + uuid4().hex
        script = (ROOT / "ba_scout" / updater._HELPER).read_text("utf-8-sig")
        self.runner = self.stage / updater._HELPER
        self.runner.write_text(script.replace(r"Local\BrokenArrowScout_Portable_06", name), "utf-8-sig")
        self.mutex_name = name

    def install(self):
        completed = subprocess.run(
            [_WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.runner), "-Root", str(self.root), "-StageName", self.stage.name,
             "-ParentPid", "0", "-NoRestart"],
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        status_file = self.stage / "install-result.txt"
        if not status_file.is_file():
            self.fail(completed.stderr.decode(errors="replace"))
        return completed.returncode, status_file.read_text("utf-8")

    def verify_contents(self, expected):
        self.assertEqual((self.root / "_internal" / "library.txt").read_text("utf-8"), expected)
        self.assertEqual((self.root / "BA-Scout.exe").read_text("utf-8"), expected)
        self.assertEqual((self.root / "data" / "prep-layout-default.json").read_text("utf-8"), expected)
        self.assertEqual((self.root / "config.json").read_text(), "personal")
        self.assertEqual((self.root / "data" / "encounters.sqlite3").read_text(), "personal")
        self.assertEqual((self.root / "exports" / "report.txt").read_text(), "personal")

    def test_success_replaces_runtime_and_leaves_previous_runtime_in_backup(self):
        code, result = self.install()
        self.assertEqual(code, 0, result)
        self.verify_contents("new")
        self.assertFalse((self.root / "_internal" / "old-only.txt").exists())
        self.assertEqual((self.stage / "backup" / "_internal" / "old-only.txt").read_text(), "old")
        self.assertEqual((self.stage / "backup" / "BA-Scout.exe").read_text(), "old")

    def test_locked_executable_rolls_back_preset_and_internal(self):
        with (self.root / "BA-Scout.exe").open("rb"):
            code, result = self.install()
        self.assertEqual(code, 1, result)
        self.assertNotIn("Rollback failed", result)
        self.verify_contents("old")
        self.assertEqual((self.payload / "_internal" / "library.txt").read_text("utf-8"), "new")

    def test_preflight_failure_leaves_all_existing_files_in_place(self):
        (self.payload / "data" / "prep-layout-default.json").unlink()
        code, result = self.install()
        self.assertEqual(code, 1, result)
        self.assertNotIn("Rollback failed", result)
        self.assertFalse((self.stage / "backup").exists())
        self.verify_contents("old")

    def test_existing_application_mutex_blocks_install(self):
        from ba_scout.windows import kernel32

        mutex = kernel32.CreateMutexW(None, False, self.mutex_name)
        self.assertTrue(mutex)
        try:
            code, result = self.install()
        finally:
            kernel32.CloseHandle(mutex)
        self.assertEqual(code, 1, result)
        self.assertIn("already running", result)
        self.verify_contents("old")
