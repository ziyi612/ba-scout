"""Update panel state tests without GitHub traffic or installation writes."""
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from ba_scout import __version__
from ba_scout.update_panel import AUTO_CHECK, UpdatePanel


class UpdatePanelTests(unittest.TestCase):
    def setUp(self):
        root = tk.Tcl()
        self.config = {}
        self.active = Mock(return_value=False)
        self.restart = Mock()
        self.available = Mock()
        with patch("ba_scout.update_panel.install_supported", return_value=True):
            self.panel = UpdatePanel(root, Path.cwd(), self.config, self.active, self.restart, self.available)

    def test_startup_preference_does_not_disable_manual_check(self):
        self.panel.auto_check.set(False)
        self.panel._save_preference()
        self.assertFalse(self.config[AUTO_CHECK])
        with patch.object(self.panel, "_start") as start:
            self.panel.check(automatic=True)
            start.assert_not_called()
            self.panel.check()
            self.assertEqual(start.call_args.args[-1], __version__)

    def test_locked_match_can_download_but_cannot_restart(self):
        self.active.return_value = True
        self.panel.release = Mock(version="99.0.0")
        with patch.object(self.panel, "_start") as start:
            self.panel._advance()
            self.assertEqual(start.call_args.args[0], "download")
        self.panel.staged = Path("staged")
        with patch("ba_scout.update_panel.launch_install") as launch:
            self.panel._advance()
            launch.assert_not_called()
            self.restart.assert_not_called()
            self.active.return_value = False
            self.panel._advance()
            launch.assert_called_once_with(self.panel.root_dir, self.panel.staged)
        self.restart.assert_called_once_with()

    def test_install_launch_failure_leaves_app_open(self):
        self.panel.release = Mock(version="99.0.0")
        self.panel.staged = Path("staged")
        with patch("ba_scout.update_panel.launch_install", side_effect=OSError("blocked")), self.assertLogs():
            self.panel._advance()
        self.restart.assert_not_called()

    def test_new_release_replaces_old_stage(self):
        self.panel.release = Mock(version="98.0.0")
        self.panel.staged = Path("old-staged")
        self.panel.pending = "check"
        self.panel.results.put((Mock(version="99.0.0"), None))
        self.panel._poll()
        self.assertIsNone(self.panel.staged)
        self.available.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
