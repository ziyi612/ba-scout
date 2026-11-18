"""Independent offline UI/report regression probe; no Tk window or network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ba_scout import ui
from ba_scout.data_summary import build_data_summary
from ba_scout.roster import Roster


def make_window():
    roster = Roster("980021", [{"id": 10, "name": "我方", "team": "Alpha"},
                                     {"id": 20, "name": "对手全名", "team": "Bravo"}],
                    "2026-10-25T12:00:00Z", "unused", "2026-10-25 20:00:00")
    window = SimpleNamespace(
        current_roster=roster, states={20: {"status": "error", "data": None, "message": "站点未返回"}},
        opening_states={}, opening20_states={}, recon_states={}, enemy_ids=[20],
        opening_window_seconds_active=120, is_replay=False, summary={}, summary20={},
        args=SimpleNamespace(self_test=False), root=Mock(), status=Mock(), closed=False,
        scan_id=21, recon_scan_id=31, last_export_path=None, report_scan_id=None,
        last_export_scan_id=None, last_report_scan_id=None, config={"deadline": 20, "self_id": 10},
        started=100.0, core_elapsed=None, scanning=True, auto_report_attempted=False,
        auto_report_attempted_scan_id=None, auto_report_path=None,
        players=roster.players, stale_history_ids=set(), hwnd=0,
        hotkeys=SimpleNamespace(poll=lambda: []), compact=Mock(), report_button=Mock(),
        table=Mock(), render_rows=Mock(), update_summary=Mock(), selected_id=None,
        opening_view_seconds=20, analysis_mode="opening", analysis_button=Mock(), expanded=True,
        select_team=Mock(), show_view=Mock(), summary_text=Mock(), detail_text=Mock(), detail_title=Mock(),
        button=Mock(),
    )
    window.export_report = MethodType(ui.ScoutWindow.export_report, window)
    for method in ("maybe_auto_report", "open_report", "start_scan", "tick", "opening_window_label", "primary_window_label"):
        setattr(window, method, MethodType(getattr(ui.ScoutWindow, method), window))
    window.controller = SimpleNamespace(poll_events=lambda: [], start=Mock(return_value=22))
    window.recon = SimpleNamespace(poll_events=lambda: [], submit_history=Mock(), mark_unavailable=Mock(), start=Mock(return_value=32))
    return window


def attach_summary(window):
    window.summary = build_data_summary(window.current_roster, window.states, window.opening_states,
                                        window.recon_states, window.enemy_ids, window_seconds=120)
    window.summary20 = build_data_summary(window.current_roster, window.states, window.opening20_states,
                                          window.recon_states, window.enemy_ids, window_seconds=20)


class IndependentExportTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="auto-report-review-", dir=ROOT / "work")
        self.addCleanup(self.scratch.cleanup)
        self.target = Path(self.scratch.name)
        self.root_patch = patch.object(ui, "ROOT", self.target)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_automatic_style_export_creates_actual_utf8_report_without_clipboard_or_opening(self):
        window = make_window()
        attach_summary(window)
        with patch.object(ui, "copy_file_to_clipboard") as clipboard, patch("os.startfile") as opener:
            path = window.export_report(copy_file=False)
        self.assertIsInstance(path, Path)
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("阵容对局ID：980021", text)
        self.assertIn("前20秒", text)
        self.assertIn("前120秒", text)
        self.assertIn("站点未返回", text)
        self.assertIn("未取得有效开局样本", text)
        self.assertNotIn("0胜", text)
        self.assertNotIn("总部署 0 次", text)
        self.assertNotIn("完整观察窗口内没有部署记录", text)
        clipboard.assert_not_called()
        opener.assert_not_called()
        window.root.update.assert_not_called()

    def test_zero_valid_samples_never_claim_zero_deployments(self):
        window = make_window()
        for seconds, states in [(20, window.opening20_states), (120, window.opening_states)]:
            states[20] = {"status": "timeout", "finished": True, "message": "本轮时间预算已截止",
                          "data": {"window_seconds": seconds, "sample_count": 0,
                                   "requested_count": 5, "units": [], "matches": [],
                                   "unusable_matches": [{"match_id": "7", "reason": "请求超时"}]}}
        attach_summary(window)
        path = window.export_report(copy_file=False)
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("有效样本 0/5 局", text)
        self.assertIn("请求超时", text)
        self.assertIn("未取得有效开局样本", text)
        self.assertNotIn("完整观察窗口内没有部署记录", text)

    def test_no_roster_does_not_write(self):
        window = make_window()
        window.current_roster = None
        self.assertIsNone(window.export_report(copy_file=False))
        self.assertEqual(list(self.target.rglob("*.txt")), [])

    def test_no_enemy_identity_keeps_core_records_and_marks_team_unconfirmed(self):
        window = make_window()
        window.enemy_ids = []
        attach_summary(window)
        path = window.export_report(copy_file=False)
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("敌我未判定", text)
        self.assertIn("对手全名", text)
        self.assertNotIn("对手 ·", text)

    def test_directory_failure_returns_without_unhandled_exception(self):
        window = make_window()
        with patch.object(Path, "mkdir", side_effect=PermissionError("模拟只读目录")), self.assertLogs(level="ERROR"):
            self.assertIsNone(window.export_report(copy_file=False))
        self.assertIn("TXT 导出失败", window.status.set.call_args.args[0])

    def test_diagnostic_json_failure_keeps_written_report_accessible(self):
        window = make_window()
        with patch.object(ui, "atomic_json", side_effect=PermissionError("模拟快照目录不可写")), self.assertLogs(level="WARNING"):
            path = window.export_report(copy_file=False)
        self.assertTrue(path.is_file())
        self.assertEqual(window.last_export_path, path)
        self.assertEqual(window.report_scan_id, window.scan_id)

    def test_deadline_partial_report_preserves_real_units_and_missing_samples_once(self):
        window = make_window()
        for seconds, states in [(20, window.opening20_states), (120, window.opening_states)]:
            states[20] = {"status": "partial", "finished": False, "message": "已取得 1/5 场，其他记录未返回",
                          "data": {"sample_count": 1, "requested_count": 5, "window_seconds": seconds,
                                   "matches": [{"match_id": "7", "source_url": "https://dash.batrace.top/match/7"}],
                                   "units": [{"id": 5, "name": "测试完整单位型号", "matches_with_unit": 1,
                                              "match_ids": ["7"], "total_spawns": 2, "first_offset_median": 4}],
                                   "unusable_matches": [{"match_id": "8", "reason": "未取得对局数据"}]}}
        attach_summary(window)
        with patch.object(ui.time, "monotonic", return_value=120.0), \
                patch.object(ui, "copy_file_to_clipboard") as clipboard, patch("os.startfile") as opener:
            window.maybe_auto_report()
            path = window.auto_report_path
            first = path.read_bytes()
            # A callback after the fixed deadline must not create a second file.
            window.opening_states[20]["data"]["sample_count"] = 2
            window.maybe_auto_report()
        text = first.decode("utf-8-sig")
        self.assertIn("有效样本 1/5 局", text)
        self.assertIn("测试完整单位型号", text)
        self.assertIn("出现 1/1 局；总部署 2 次", text)
        self.assertIn("未取得对局数据", text)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(len(list((self.target / "exports").glob("*.txt"))), 1)
        clipboard.assert_not_called()
        opener.assert_not_called()
        window.root.update.assert_not_called()

    def test_manual_open_builds_current_snapshot_without_preventing_final_auto_report(self):
        window = make_window()
        attach_summary(window)
        with patch.object(ui.tk, "Toplevel") as dialog, patch.object(ui.tk, "Frame"), \
                patch.object(ui.tk, "Scrollbar"), patch.object(ui.tk, "Text") as content, \
                patch.object(ui, "copy_file_to_clipboard") as clipboard, patch("os.startfile") as opener:
            result = window.open_report()
        self.assertIs(result, dialog.return_value)
        self.assertIsNone(window.auto_report_attempted_scan_id)
        self.assertEqual(window.report_scan_id, window.scan_id)
        self.assertIn("980021", content.return_value.insert.call_args.args[1])
        manual_path = window.last_export_path
        self.assertTrue(manual_path.is_file())
        window.scanning = False
        set_opening(window, finished=True)
        attach_summary(window)
        with patch.object(ui.time, "monotonic", return_value=105.0):
            window.maybe_auto_report()
        self.assertTrue(window.auto_report_path.is_file())
        self.assertNotEqual(window.auto_report_path, manual_path)
        self.assertEqual(len(list((self.target / "exports").glob("*.txt"))), 2)
        clipboard.assert_not_called()
        opener.assert_not_called()
        window.root.update.assert_not_called()


def opening_event(*, finished, scan_id=31, player=20):
    windows = {}
    for seconds in (20, 120):
        windows[str(seconds)] = {"status": "error" if finished else "pending", "finished": finished,
                                  "message": "全部未取得" if finished else "仍在查询",
                                  "data": {"sample_count": 0, "requested_count": 5, "window_seconds": seconds,
                                           "units": [], "matches": [], "unusable_matches": []}}
    return {"type": "opening", "scan_id": scan_id, "player_id": player,
            **windows["120"], "windows": windows}


def set_opening(window, *, finished):
    event = opening_event(finished=finished)
    window.opening_states[20] = event
    window.opening20_states[20] = event["windows"]["20"]


def queue_poll(events):
    def poll():
        current = events[:]
        events.clear()
        return current
    return poll


class AutomaticReportStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(ui.time, "monotonic", return_value=105.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.window = make_window()
        self.window.export_report = Mock(return_value=ROOT / "work" / "automatic-report-test.txt")

    def test_waits_for_core_even_when_cached_history_is_finished(self):
        window = self.window
        set_opening(window, finished=True)
        window.maybe_auto_report()
        window.export_report.assert_not_called()

    def test_waits_for_both_windows_and_every_confirmed_enemy(self):
        window = self.window
        window.scanning = False
        set_opening(window, finished=True)
        window.opening20_states[20] = {"finished": False}
        window.maybe_auto_report()
        window.export_report.assert_not_called()
        window.opening20_states[20]["finished"] = True
        window.enemy_ids.append(21)
        window.maybe_auto_report()
        window.export_report.assert_not_called()
        window.opening_states[21] = {"finished": True}
        window.opening20_states[21] = {"finished": True}
        window.maybe_auto_report()
        window.export_report.assert_called_once_with(copy_file=False)

    def test_terminal_failures_still_produce_report_only_once(self):
        window = self.window
        window.scanning = False
        set_opening(window, finished=True)
        for _ in range(5):
            window.maybe_auto_report()
        window.export_report.assert_called_once_with(copy_file=False)
        self.assertEqual(window.auto_report_attempted_scan_id, window.scan_id)

    def test_common_deadline_produces_available_snapshot_without_wait_extension(self):
        window = self.window
        self.now.return_value = 119.999
        window.maybe_auto_report()
        window.export_report.assert_not_called()
        self.now.return_value = 120.0
        window.maybe_auto_report()
        window.export_report.assert_called_once_with(copy_file=False)

    def test_closed_empty_roster_and_cancelled_generation_never_export(self):
        self.now.return_value = 125.0
        for key, value in [("closed", True), ("current_roster", None), ("players", []), ("scan_id", None)]:
            with self.subTest(key=key):
                window = make_window()
                window.export_report = Mock()
                setattr(window, key, value)
                window.maybe_auto_report()
                window.export_report.assert_not_called()

    def test_export_exception_and_returned_failure_do_not_loop(self):
        for error in (PermissionError("模拟写入失败"), None):
            with self.subTest(error=repr(error)):
                window = make_window()
                window.scanning = False
                set_opening(window, finished=True)
                window.export_report = Mock(side_effect=error) if error else Mock(return_value=None)
                if error:
                    with self.assertLogs(level="ERROR"):
                        window.maybe_auto_report()
                else:
                    window.maybe_auto_report()
                for _ in range(3):
                    window.maybe_auto_report()
                window.export_report.assert_called_once_with(copy_file=False)

    def test_same_match_refresh_clears_previous_report_and_allows_new_automatic_report(self):
        window = self.window
        window.auto_report_attempted_scan_id = window.scan_id
        window.auto_report_path = window.last_export_path = Path("F:/obsolete-report.txt")
        window.report_scan_id = window.scan_id
        window.start_scan(window.current_roster, force=True)
        self.assertEqual(window.scan_id, 22)
        self.assertEqual(window.recon_scan_id, 32)
        for key in ("auto_report_attempted_scan_id", "auto_report_path", "last_export_path", "report_scan_id"):
            self.assertIsNone(getattr(window, key))
        window.scanning = False
        set_opening(window, finished=True)
        window.maybe_auto_report()
        window.export_report.assert_called_once_with(copy_file=False)

    def test_late_old_round_events_cannot_finish_current_round(self):
        window = self.window
        window.controller.poll_events = lambda: [{"type": "finished", "scan_id": 20, "elapsed": 1}]
        window.recon.poll_events = lambda: [opening_event(finished=True, scan_id=30)]
        window.tick()
        window.export_report.assert_not_called()
        self.assertTrue(window.scanning)
        self.assertEqual(window.opening_states, {})
        window.root.after.assert_called_once_with(60, window.tick)

    def test_fresh_profile_replaces_old_terminal_history_before_auto_decision(self):
        window = self.window
        set_opening(window, finished=True)
        core_events = [{"type": "player", "scan_id": 21, "player_id": 20, "status": "ok",
                        "data": {"recent_match_ids": [123, 124]}},
                       {"type": "finished", "scan_id": 21, "elapsed": 1}]
        recon_events = []
        window.controller.poll_events = queue_poll(core_events)
        window.recon.poll_events = queue_poll(recon_events)
        window.recon.submit_history.side_effect = lambda *args: recon_events.append(opening_event(finished=False))
        snapshots = []
        def write(**kwargs):
            snapshots.append(deepcopy(window.opening_states))
            return ROOT / "work" / "automatic-report-test.txt"
        window.export_report.side_effect = write
        window.tick()
        window.export_report.assert_not_called()
        self.assertFalse(window.opening_states[20]["finished"])
        recon_events.append(opening_event(finished=True))
        window.tick()
        window.export_report.assert_called_once_with(copy_file=False)
        self.assertTrue(snapshots[0][20]["finished"])
        self.assertGreaterEqual(window.update_summary.call_count, 2)

    def test_summary_is_rebuilt_before_final_snapshot_in_same_tick(self):
        window = self.window
        core_events = [{"type": "finished", "scan_id": 21, "elapsed": 1}]
        recon_events = [opening_event(finished=True)]
        window.controller.poll_events = queue_poll(core_events)
        window.recon.poll_events = queue_poll(recon_events)
        order = []
        window.update_summary.side_effect = lambda: order.append("summary")
        window.export_report.side_effect = lambda **kwargs: order.append("export") or ROOT / "work" / "automatic-report-test.txt"
        window.tick()
        self.assertEqual(order, ["summary", "export"])

    def test_manual_snapshot_does_not_consume_automatic_final_attempt(self):
        window = self.window
        window.report_scan_id = window.scan_id
        window.last_export_path = ROOT / "work" / "earlier-manual-snapshot.txt"
        window.scanning = False
        set_opening(window, finished=True)
        window.maybe_auto_report()
        window.export_report.assert_called_once_with(copy_file=False)

    def test_manual_open_does_not_reuse_previous_round_file(self):
        window = self.window
        window.auto_report_path = Mock()
        window.report_scan_id = window.scan_id - 1
        window.export_report.return_value = None
        window.open_report()
        window.export_report.assert_called_once_with(copy_file=False)
        window.auto_report_path.is_file.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
