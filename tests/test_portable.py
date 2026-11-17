"""Isolated portable configuration/storage checks; no GUI or network."""
from __future__ import annotations

import importlib.util
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent.parent
(SOURCE / 'work').mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(SOURCE))
from ba_scout import settings, setup
from ba_scout.engine import ScanController
from ba_scout.recon_engine import RecentRecon
from ba_scout.scouting_engine import BattleRecon
from ba_scout.ui import ScoutWindow
from ba_scout import screen_ocr
from ba_scout.prep_roster import extract_prep_roster


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portable-test-", dir=SOURCE / "work")
        self.root = Path(self.temp.name)
        self.settings_root = patch.object(settings, "ROOT", self.root)
        self.setup_root = patch.object(setup, "ROOT", self.root)
        self.settings_root.start()
        self.setup_root.start()
        self.logs = self.root / "steamapps" / "common" / "test-game" / "GameLogs"
        self.logs.mkdir(parents=True)

    def tearDown(self):
        self.setup_root.stop()
        self.settings_root.stop()
        self.temp.cleanup()

    def test_defaults_are_unconfigured_with_no_personal_identity_or_game_path(self):
        config = settings.load_settings()
        self.assertEqual(config["self_id"], 0)
        self.assertEqual(config["log_dir"], "")
        self.assertFalse(setup.configuration_ready(config))

    def test_application_paths_allow_inside_and_reject_traversal_or_other_folder(self):
        self.assertEqual(settings.app_path(self.root / "data" / "cache"), self.root / "data" / "cache")
        for value in [self.root.parent / "outside-portable", self.root / ".." / "outside-portable", "relative-path", r"C:\not-a-portable-target"]:
            with self.assertRaises(ValueError):
                settings.app_path(value)

    def test_no_fixed_drive_requirement_without_writing_other_drives(self):
        for drive in ["D", "E", "F"]:
            fake_root = Path(f"{drive}:\\Portable\\DataViewer")
            with patch.object(settings, "ROOT", fake_root):
                self.assertEqual(settings.app_path(fake_root / "exports" / "out.txt"), (fake_root / "exports" / "out.txt").resolve())

    def test_read_paths_are_not_restricted_to_application_or_drive(self):
        for raw in [r"C:\Games\GameLogs", r"D:\SteamLibrary\GameLogs", r"E:\logs\latest.log"]:
            self.assertEqual(settings.read_path(raw), Path(raw).resolve())
        for value in ["", "relative-log"]:
            with self.assertRaises(ValueError):
                settings.read_path(value)

    def test_atomic_json_stays_inside_root_and_root_itself_is_not_a_file_target(self):
        target = self.root / "data" / "fixture.json"
        settings.atomic_json(target, {"name": "便携测试"})
        self.assertEqual(json.loads(target.read_text("utf-8")), {"name": "便携测试"})
        self.assertFalse(list(target.parent.glob("*.tmp")))
        with self.assertRaises(ValueError):
            settings.atomic_json(self.root, {})
        with self.assertRaises(ValueError):
            settings.atomic_json(self.root.parent / "outside-fixture.json", {})

    def test_all_three_engines_reject_external_cache_before_writing(self):
        outside = self.root.parent / (self.root.name + "-outside")
        for cls in [ScanController, RecentRecon, BattleRecon]:
            with self.assertRaises(ValueError):
                cls(str(outside), lambda *a, **kw: {})
            controller = cls(str(self.root / cls.__name__), lambda *a, **kw: {})
            controller.close()
        self.assertFalse(outside.exists())

    def test_temp_directory_is_application_local(self):
        with patch.dict(os.environ), patch.object(tempfile, "tempdir", None):
            target = settings.prepare_runtime_paths()
            self.assertEqual(target, self.root / "tmp")
            self.assertEqual(os.environ["TEMP"], str(target))
            self.assertEqual(os.environ["TMP"], str(target))
            self.assertEqual(tempfile.gettempdir(), str(target))

    def test_frozen_root_uses_executable_folder_without_any_write(self):
        spec = importlib.util.spec_from_file_location("ba_scout.portable_settings_frozen_test", SOURCE / "ba_scout" / "settings.py")
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", r"E:\Tools\Viewer\Viewer.exe"):
            spec.loader.exec_module(module)
        self.assertEqual(module.ROOT, Path(r"E:\Tools\Viewer").resolve())

    def test_initial_dialog_cancel_does_not_save_or_fill_identity(self):
        config = settings.DEFAULTS.copy()
        before = config.copy()
        with patch.object(setup, "_configuration_dialog", return_value=None) as dialog:
            self.assertIsNone(setup.ensure_configured(config))
            dialog.assert_called_once()
        self.assertEqual(config, before)
        self.assertFalse((self.root / "config.json").exists())

    def test_configured_start_skips_dialog_and_returns_independent_config(self):
        config = {**settings.DEFAULTS, "self_id": 990003, "log_dir": str(self.logs)}
        with patch.object(setup, "_configuration_dialog") as dialog:
            result = setup.ensure_configured(config)
            dialog.assert_not_called()
        self.assertEqual(result, config)
        self.assertIsNot(result, config)

    def test_invalid_identity_or_wrong_directory_requires_configuration(self):
        for value in [0, -1, True, "", "name", "1.5"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    setup.validate_configuration({"self_id": value, "log_dir": str(self.logs)})
        with self.assertRaises(ValueError):
            setup.validate_configuration({"self_id": 990003, "log_dir": str(self.root)})
        self.assertFalse(setup.configuration_ready({"self_id": 990003, "log_dir": str(self.root)}))

    def test_recent_roster_returns_candidates_without_selecting_or_saving(self):
        lines = ["[2026-10-25 18:00:00]", "Log: [NET_ROOM] GameRoom entered, fight id: 100", "Log: Player list:"]
        lines += [f"ID: {990000+i}, Name: 测试玩家{i}, Team: {'Alpha' if i < 5 else 'Bravo'}" for i in range(10)]
        (self.logs / "Gamelog__fixture.log").write_text("\n".join(lines) + "\n\n", encoding="utf-8")
        choices = setup.load_player_choices(str(self.logs))
        self.assertEqual(len(choices), 10)
        self.assertEqual(choices[3], {"id": 990003, "name": "测试玩家3", "team": "Alpha"})
        self.assertFalse((self.root / "config.json").exists())
        self.assertEqual(settings.DEFAULTS["self_id"], 0)

    def test_manifest_lookup_follows_config_and_disables_unknown_build(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "tactical-catalog.json").write_text(json.dumps({"schema": 1, "source": {"build_id": "123"}, "units": {}}), encoding="utf-8")
        manifest = self.root / "steamapps" / "appmanifest_1604270.acf"
        manifest.write_text('"AppState" { "buildid" "123" }', encoding="utf-8")
        instance = object.__new__(ScoutWindow)
        instance.config = {"log_dir": str(self.logs)}
        with patch("ba_scout.ui.ROOT", self.root):
            data, note = instance.load_tactical_catalog()
            self.assertIsNotNone(data)
            self.assertEqual(note, "")
            manifest.write_text('"AppState" { "buildid" "999" }', encoding="utf-8")
            data, note = instance.load_tactical_catalog()
            self.assertIsNone(data)
            self.assertIn("版本", note)

    def test_saved_zero_or_bad_identity_returns_to_unconfigured_state(self):
        settings.atomic_json(self.root / "config.json", {"self_id": "bad", "log_dir": str(self.logs)})
        self.assertEqual(settings.load_settings()["self_id"], 0)

    def test_portable_mutex_is_distinct_from_original_and_constant_across_copies(self):
        from ba_scout import windows
        calls = []
        fake = SimpleNamespace(CreateMutexW=lambda _a, _b, name: (calls.append(name), 123)[1], CloseHandle=lambda _h: True)
        with patch.object(windows, "kernel32", fake):
            first, second = windows.SingleInstance(), windows.SingleInstance()
            first.close()
            second.close()
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0], "Local\\BrokenArrowScout_Portable_06")

    def test_packaged_preset_has_only_ten_generic_normalized_boxes(self):
        preset = json.loads((SOURCE / "data" / "prep-layout-default.json").read_text("utf-8-sig"))
        self.assertEqual(set(preset), {"Alpha", "Bravo"})
        for config in preset.values():
            self.assertEqual(set(config), {"rows"})
            self.assertEqual(len(config["rows"]), 5)
            for box in config["rows"]:
                self.assertEqual(len(box), 4)
                self.assertTrue(all(type(number) in (int, float) for number in box))
        self.assertTrue(extract_prep_roster([], preset, image_size=(1920, 1080))["geometry_valid"])
        self.assertEqual(len(screen_ocr.ScreenOCR._regions(preset)), 10)

    def test_preset_loader_returns_independent_copies_and_rejects_overlap(self):
        preset = copy.deepcopy(settings.DEFAULTS["prep_regions"])
        settings.atomic_json(self.root / "data" / "prep-layout-default.json", preset)
        first, second = settings.default_prep_regions(), settings.default_prep_regions()
        first["Alpha"]["rows"][0][0] = 0.99
        self.assertEqual(second, preset)
        invalid = copy.deepcopy(preset)
        invalid["Bravo"]["rows"][0] = invalid["Alpha"]["rows"][0][:]
        settings.atomic_json(self.root / "data" / "prep-layout-default.json", invalid)
        self.assertEqual(settings.default_prep_regions(), {})

    def test_missing_or_malformed_preset_disables_automatic_regions(self):
        self.assertEqual(settings.default_prep_regions(), {})
        target = self.root / "data" / "prep-layout-default.json"
        target.parent.mkdir()
        target.write_text("{bad-json", encoding="utf-8")
        self.assertEqual(settings.default_prep_regions(), {})
        target.write_text('{"Alpha": null, "Bravo": null}', encoding="utf-8")
        self.assertEqual(settings.default_prep_regions(), {})

    def test_settings_load_does_not_share_nested_regions_with_other_load_or_defaults(self):
        original = copy.deepcopy(settings.DEFAULTS)
        first, second = settings.load_settings(), settings.load_settings()
        self.assertTrue(first["prep_regions"])
        first["prep_regions"]["Alpha"]["rows"][0][0] = 0.99
        self.assertEqual(second, original)
        self.assertEqual(settings.DEFAULTS, original)

    def test_saved_calibration_retained_and_loads_do_not_alias(self):
        custom = copy.deepcopy(settings.DEFAULTS["prep_regions"])
        custom["Alpha"]["rows"].reverse()
        settings.atomic_json(self.root / "config.json", {"prep_regions": custom})
        first, second = settings.load_settings(), settings.load_settings()
        self.assertEqual(first["prep_regions"], custom)
        first["prep_regions"]["Alpha"]["rows"].clear()
        self.assertEqual(second["prep_regions"], custom)
        self.assertEqual(settings.load_settings()["prep_regions"], custom)

    def test_setup_validation_and_configured_start_do_not_share_nested_regions(self):
        original = {**copy.deepcopy(settings.DEFAULTS), "self_id": 990003, "log_dir": str(self.logs)}
        validated = setup.validate_configuration(original)
        ready = setup.ensure_configured(original)
        validated["prep_regions"]["Alpha"]["rows"].clear()
        ready["prep_regions"]["Bravo"]["rows"].clear()
        self.assertEqual(len(original["prep_regions"]["Alpha"]["rows"]), 5)
        self.assertEqual(len(original["prep_regions"]["Bravo"]["rows"]), 5)
        self.assertEqual(len(settings.DEFAULTS["prep_regions"]["Alpha"]["rows"]), 5)

    def test_ocr_default_scratch_and_worker_are_application_local(self):
        with patch.object(screen_ocr, "ROOT", self.root):
            client = screen_ocr.ScreenOCR()
        try:
            self.assertEqual(client.temp_dir, self.root / "tmp" / "ocr")
            self.assertEqual(client.worker_script, SOURCE / "ba_scout" / "ocr_worker.ps1")
            self.assertTrue(client.worker_script.is_file())
            self.assertTrue(client._thread.daemon)
        finally:
            client.close()

    def test_ocr_rejects_external_scratch_before_creation(self):
        outside = self.root.parent / (self.root.name + "-external-ocr")
        with self.assertRaises(ValueError):
            screen_ocr.ScreenOCR(outside)
        self.assertFalse(outside.exists())

    def test_ocr_frozen_worker_resolves_beside_module_not_executable(self):
        fake_module = self.root / "_internal" / "ba_scout" / "screen_ocr.py"
        with patch.object(screen_ocr, "__file__", str(fake_module)):
            client = screen_ocr.ScreenOCR(self.root / "tmp" / "ocr")
        try:
            self.assertEqual(client.worker_script, fake_module.with_name("ocr_worker.ps1"))
        finally:
            client.close()

    def test_ocr_selected_png_is_read_only_and_may_be_outside_root(self):
        # This file is outside the patched application root but inside the checkout.
        external = SOURCE / "work" / (self.root.name + "-selected.png")
        external.write_bytes(b"test-selected-input")
        self.addCleanup(external.unlink)
        client = screen_ocr.ScreenOCR(self.root / "tmp" / "ocr")
        try:
            with patch.object(client, "_submit", return_value="accepted") as submit:
                self.assertEqual(client.submit_extract(external), "accepted")
                self.assertEqual(submit.call_args.args[0]["image_path"], str(external.resolve()))
            self.assertEqual(external.read_bytes(), b"test-selected-input")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
