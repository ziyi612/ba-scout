"""Offline regression checks against real lifecycle/render/poll UI methods."""
from __future__ import annotations

import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from ba_scout import ui
from ba_scout.roster import Roster
from tests.test_auto_report import make_window, opening_event, queue_poll


def populated_window():
    window = make_window()
    window.controller.cancel = Mock()
    window.recon.cancel = Mock()
    window.summary_heading = Mock()
    window.summary_confidence = Mock()
    window.catalog = None
    window.catalog_note = ""
    window.enemy_button = Mock()
    window.view_mode = "data"
    window.ally_button = Mock()
    window.window20_button = Mock()
    window.window_primary_button = Mock()
    window.team_mode = "enemy"
    window.summary_text.yview.return_value = (0.0, 1.0)
    window.table.get_children.return_value = ("old-player",)
    window.table.selection.return_value = []
    window.config["log_dir"] = str(Path(__file__).resolve().parents[1] / "work")
    window.config["prep_enabled"] = False
    window.last_log_error = None
    window.live_phase = "game_room"
    window.live_fight_id = "980021"
    window.selected_id = 20
    window.card_detail_id = "20"
    window.is_replay = True
    window.core_elapsed = 3.0
    window.stale_history_ids = {20}
    window.states = {20: {"status": "cached", "data": {"player_id": 20, "name": "上一局名字"}}}
    window.recon_states = {20: {"status": "ok", "data": {"player_id": 20, "all_units": [{"name": "上一局单位"}]}}}
    old = opening_event(finished=True)
    window.opening_states = {20: old}
    window.opening20_states = {20: old["windows"]["20"]}
    window.summary = {"players": [{"name": "上一局名字"}], "old": True}
    window.summary20 = {"players": [{"name": "上一局名字"}], "old": True}
    window.auto_report_attempted_scan_id = window.scan_id
    window.auto_report_path = window.last_export_path = Path("F:/old-test-report.txt")
    window.report_scan_id = window.scan_id
    window.export_report = Mock()
    for method in ("handle_lifecycle", "poll_log", "render_rows", "update_summary", "select_team",
                   "selected_opening_states", "opening_row", "on_select", "restart_prep_if_waiting"):
        setattr(window, method, MethodType(getattr(ui.ScoutWindow, method), window))
    return window


class UiLifecycleTests(unittest.TestCase):
    def assert_phase_clears_previous_match(self, phase, text, fight_id="980022"):
        window = populated_window()
        window.handle_lifecycle({"type": "lifecycle", "phase": phase, "fight_id": fight_id})
        window.controller.cancel.assert_called_once_with()
        window.recon.cancel.assert_called_once_with()
        self.assertEqual(window.live_phase, phase)
        self.assertEqual(window.live_fight_id, fight_id)
        for field in ("scan_id", "recon_scan_id", "started", "core_elapsed", "current_roster", "selected_id",
                      "auto_report_attempted_scan_id", "auto_report_path", "last_export_path", "report_scan_id"):
            self.assertIsNone(getattr(window, field), field)
        for field in ("states", "recon_states", "opening_states", "opening20_states"):
            self.assertEqual(getattr(window, field), {}, field)
        self.assertEqual(window.players, [])
        self.assertEqual(window.enemy_ids, [])
        self.assertEqual(window.stale_history_ids, set())
        self.assertFalse(window.scanning)
        self.assertFalse(window.is_replay)
        window.report_button.config.assert_called_with(state="disabled")
        window.table.delete.assert_called_with("old-player")
        self.assertIsNone(window.card_detail_id)
        cards = window.cards.set_rows.call_args.args[0]
        if phase == "waiting":
            self.assertEqual([iid for iid, _, _ in cards], [f"slot-{i}" for i in range(5)])
        else:
            self.assertEqual(cards, [])
        if phase == "waiting":
            self.assertEqual([row.kwargs["iid"] for row in window.table.insert.call_args_list],
                             [f"slot-{index}" for index in range(5)])
            self.assertTrue(all(row.kwargs["values"][-1] == "等名字" for row in window.table.insert.call_args_list))
        else:
            window.table.insert.assert_not_called()
        for summary in (window.summary, window.summary20):
            self.assertEqual(summary["players"], [])
            self.assertEqual(summary["context"]["fight_id"], "未取得")
            self.assertNotIn("old", summary)
        window.summary_text.delete.assert_called_with("1.0", "end")
        visible_summary = "".join(call.args[1] for call in window.summary_text.insert.call_args_list)
        self.assertNotIn("上一局名字", visible_summary)
        self.assertNotIn("上一局单位", visible_summary)
        self.assertIn(text, window.status.set.call_args.args[0])
        window.summary_heading.set.assert_called_with("等待本局数据")
        window.detail_title.set.assert_called_with("当前没有已确认的本局名单")
        window.maybe_auto_report()
        window.open_report()
        window.export_report.assert_not_called()
        return window

    def test_waiting_clears_previous_match_and_report(self):
        self.assert_phase_clears_previous_match("waiting", "等待本局名单")

    def test_loading_clears_previous_match_and_report(self):
        self.assert_phase_clears_previous_match("loading", "正在加载")

    def test_game_room_without_roster_does_not_keep_previous_players(self):
        self.assert_phase_clears_previous_match("game_room", "等待完整十人名单")

    def test_ended_clears_previous_match_and_report(self):
        self.assert_phase_clears_previous_match("ended", "已结束", fight_id="980021")

    def test_lobby_clears_previous_match_and_report(self):
        self.assert_phase_clears_previous_match("lobby", "回到大厅", fight_id=None)

    def test_new_log_session_clears_previous_match_and_report(self):
        self.assert_phase_clears_previous_match("session", "监听本次游戏日志", fight_id=None)

    def test_old_events_after_lifecycle_cannot_repopulate_or_export(self):
        window = populated_window()
        window.controller.poll_events = lambda: [
            {"type": "player", "scan_id": 21, "player_id": 20, "status": "ok", "data": {"player_id": 20}},
            {"type": "finished", "scan_id": 21, "elapsed": 2},
        ]
        window.recon.poll_events = lambda: [
            {"type": "recon", "scan_id": 31, "player_id": 20, "status": "ok", "data": {"all_units": []}},
            opening_event(finished=True),
        ]
        window.handle_lifecycle({"phase": "lobby"})
        summary_before = window.summary
        window.tick()
        self.assertIs(window.summary, summary_before)
        self.assertEqual(window.states, {})
        self.assertEqual(window.recon_states, {})
        self.assertEqual(window.opening_states, {})
        self.assertEqual(window.opening20_states, {})
        self.assertIsNone(window.current_roster)
        window.recon.submit_history.assert_not_called()
        window.export_report.assert_not_called()
        window.root.after.assert_called_with(60, window.tick)

    def test_new_roster_after_lifecycle_restores_fresh_query_and_report_button(self):
        window = populated_window()
        window.handle_lifecycle({"phase": "waiting", "fight_id": "980022"})
        roster = Roster("980022", [{"id": 10, "name": "我方", "team": "Alpha"},
                                  {"id": 30, "name": "本局新对手", "team": "Bravo"}],
                        "2026-10-25T14:00:00Z", "unused", "2026-10-25 22:00:00")
        window.start_scan(roster)
        self.assertIs(window.current_roster, roster)
        self.assertEqual(window.enemy_ids, [30])
        self.assertEqual(window.scan_id, 22)
        self.assertEqual(window.recon_scan_id, 32)
        self.assertTrue(window.scanning)
        self.assertFalse(window.is_replay)
        self.assertIsNone(window.auto_report_attempted_scan_id)
        self.assertIsNone(window.auto_report_path)
        window.report_button.config.assert_called_with(state="normal")
        window.controller.start.assert_called_once_with(roster, 10, force=False)
        window.recon.start.assert_called_once_with([30], deadline=19.0, window_seconds=120, history_limit=5)
        self.assertEqual(window.summary["context"]["fight_id"], "980022")
        self.assertEqual([p["player_id"] for p in window.summary["players"]], [30])
        self.assertEqual(window.table.insert.call_args.kwargs["values"][0], "本局新对手")
        window.export_report.assert_not_called()

    def test_poll_log_processes_waiting_then_roster_in_source_order(self):
        window = populated_window()
        roster = Roster("980022", [{"id": 10, "name": "我方", "team": "Alpha"},
                                  {"id": 30, "name": "本局新对手", "team": "Bravo"}], "now", "unused", "now")
        events = [{"type": "lifecycle", "phase": "waiting", "fight_id": "980022"},
                  {"type": "roster", "roster": roster}]
        window.watcher = SimpleNamespace(poll_events=queue_poll(events))
        window.poll_log()
        self.assertIs(window.current_roster, roster)
        self.assertEqual(window.live_phase, "game_room")
        self.assertEqual(window.live_fight_id, "980022")
        window.controller.cancel.assert_called_once_with()
        window.controller.start.assert_called_once_with(roster, 10, force=False)
        self.assertEqual(window.states, {})
        self.assertIsNone(window.last_log_error)
        window.root.after.assert_called_with(500, window.poll_log)

    def test_poll_log_final_lobby_invalidates_roster_from_same_batch(self):
        window = populated_window()
        events = [{"type": "roster", "roster": window.current_roster},
                  {"type": "lifecycle", "phase": "ended", "fight_id": "980021"},
                  {"type": "lifecycle", "phase": "lobby", "fight_id": None}]
        window.watcher = SimpleNamespace(poll_events=queue_poll(events))
        window.poll_log()
        self.assertIsNone(window.current_roster)
        self.assertIsNone(window.scan_id)
        self.assertEqual(window.live_phase, "lobby")
        self.assertEqual(window.controller.cancel.call_count, 2)
        self.assertEqual(window.recon.cancel.call_count, 2)
        window.report_button.config.assert_called_with(state="disabled")
        window.maybe_auto_report()
        window.export_report.assert_not_called()

    def test_closed_poll_does_not_consume_pending_log_events(self):
        window = populated_window()
        window.closed = True
        window.watcher = SimpleNamespace(poll_events=Mock())
        window.poll_log()
        window.watcher.poll_events.assert_not_called()
        window.root.after.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
