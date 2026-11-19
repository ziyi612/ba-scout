"""Preparation identity/UI wiring tests without real Tk, OCR, or network."""
from __future__ import annotations

import unittest
from dataclasses import asdict
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from ba_scout import ui
from ba_scout.data_summary import build_data_summary, summary_sections, summary_text
from ba_scout.prep_dialog import PrepDialog
from ba_scout.reporting import build_scout_report
from ba_scout.roster import Roster
from tests.test_auto_report import queue_poll
from tests.test_ui_lifecycle import populated_window


def prep_window():
    window = populated_window()
    window.config.update(prep_enabled=True, prep_regions={"Alpha": [.1, .1, .3, .5], "Bravo": [.6, .1, .3, .5]})
    window.prep_controller = SimpleNamespace(start=Mock(return_value=42), cancel=Mock(), poll_events=lambda: [])
    window.prep_generation = 41
    window.prep_rows = []
    window.prep_dialog = None
    window.screen_ocr = Mock()
    window.ensure_prep_tools = Mock()
    for method in ("poll_prep", "restart_prep_if_waiting", "start_prep_manual", "show_prep_state", "record_prep_event"):
        setattr(window, method, MethodType(getattr(ui.ScoutWindow, method), window))
    return window


def resolved_event(*, generation=41, source="screen", fight_id="980021"):
    players = [{"id": pid, "name": f"准备对手{pid}", "team": "Bravo", "index": index,
                "identity_source": "screen-exact"} for index, pid in enumerate((30, 31, 32))]
    rows = [{"team": "Bravo", "index": index, "resolution": {"status": "resolved", "player_id": pid, "name": f"准备对手{pid}"}}
            for index, pid in enumerate((30, 31, 32))]
    rows += [{"team": "Bravo", "index": 3, "resolution": {"status": "ambiguous", "reason": "同名两人待选择"}},
             {"team": "Bravo", "index": 4, "resolution": {"status": "missing", "reason": "第五行未识别"}}]
    return {"type": "resolved", "generation": generation, "fight_id": fight_id, "source": source,
            "players": players, "rows": rows, "started": 100.0, "deadline": 120.0}


def old_dialog():
    class Value:
        def __init__(self, value):
            self.value = value
            self.config = Mock()

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    keys = [(team, index) for team in ("Alpha", "Bravo") for index in range(5)]
    dialog = SimpleNamespace(entries={key: Value("上局旧姓名") for key in keys},
                             selectors={key: Value("上局旧姓名 · ID 88") for key in keys},
                             candidates={key: {"上局旧姓名 · ID 88": {"name": "上局旧姓名", "id": 88}} for key in keys},
                             note=Mock(), owner=SimpleNamespace(start_prep_manual=Mock()), exists=lambda: True)
    dialog.update_rows = MethodType(PrepDialog.update_rows, dialog)
    return dialog


class PreparationUiTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(ui.time, "monotonic", return_value=108.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def resolve(self, window, **kwargs):
        events = [resolved_event(**kwargs)]
        window.prep_controller.poll_events = queue_poll(events)
        window.poll_prep()

    def test_partial_three_opponents_without_self_id_are_grouped_by_explicit_calibration(self):
        window = prep_window()
        self.resolve(window)
        self.assertNotIn(window.config["self_id"], [p["id"] for p in window.players])
        self.assertEqual(window.enemy_ids, [30, 31, 32])
        self.assertEqual(window.current_roster.self_team, "Alpha")
        self.assertEqual(window.current_roster.source, "screen_names")
        self.assertFalse(window.is_replay)
        self.assertEqual([p["player_id"] for p in window.summary["players"]], [30, 31, 32])
        rendered = [call.kwargs["values"][0] for call in window.table.insert.call_args_list]
        self.assertEqual(rendered, ["准备对手30", "准备对手31", "准备对手32", "对手 4", "对手 5"])
        self.assertEqual([entry.kwargs["values"][-1] for entry in window.table.insert.call_args_list[-2:]],
                         ["重名待确认", "等名字"])
        window.show_view.assert_called_with("cards")
        self.assertIn("3/5", window.status.set.call_args.args[0])

    def test_name_lookup_time_is_deducted_from_both_network_budgets(self):
        window = prep_window()
        self.resolve(window)
        self.assertEqual(window.started, 100.0)
        self.assertAlmostEqual(window.controller.start.call_args.kwargs["deadline_seconds"], 11.8)
        self.assertAlmostEqual(window.recon.start.call_args.kwargs["deadline"], 11.8)
        self.assertLessEqual(108.0 + window.controller.start.call_args.kwargs["deadline_seconds"], 120)

    def test_expired_name_result_does_not_start_a_fresh_twenty_second_budget(self):
        window = prep_window()
        old = window.current_roster
        with patch.object(ui.time, "monotonic", return_value=120.1):
            self.resolve(window)
        window.controller.start.assert_not_called()
        window.recon.start.assert_not_called()
        self.assertIs(window.current_roster, old)
        self.assertIn("截止", window.status.set.call_args.args[0])

    def test_waiting_starts_preparation_controller_after_clearing_old_data(self):
        window = prep_window()
        window.handle_lifecycle({"phase": "waiting", "fight_id": "980022"})
        self.assertIsNone(window.current_roster)
        window.prep_controller.cancel.assert_called_with()
        window.prep_controller.start.assert_called_once_with("980022", window.config["prep_regions"])
        self.assertEqual(window.prep_generation, 42)

    def test_waiting_without_calibration_or_when_disabled_does_not_start_capture(self):
        for config in ({"prep_regions": {}}, {"prep_enabled": False}):
            with self.subTest(config=config):
                window = prep_window()
                window.config.update(config)
                window.handle_lifecycle({"phase": "waiting", "fight_id": "980022"})
                window.prep_controller.start.assert_not_called()

    def test_lobby_cancels_preparation_and_ignores_old_generation(self):
        window = prep_window()
        self.resolve(window)
        window.handle_lifecycle({"phase": "lobby", "fight_id": None})
        window.controller.start.reset_mock()
        window.recon.start.reset_mock()
        self.resolve(window)
        self.assertIsNone(window.prep_generation)
        self.assertEqual(window.prep_rows, [])
        self.assertIsNone(window.current_roster)
        window.controller.start.assert_not_called()
        window.recon.start.assert_not_called()
        window.prep_controller.cancel.assert_called_once_with()

    def test_same_fight_loading_and_game_room_keep_early_roster(self):
        for phase in ("loading", "game_room"):
            with self.subTest(phase=phase):
                window = prep_window()
                self.resolve(window)
                roster, states, scan = window.current_roster, window.states, window.scan_id
                window.handle_lifecycle({"phase": phase, "fight_id": "980021"})
                self.assertIs(window.current_roster, roster)
                self.assertIs(window.states, states)
                self.assertEqual(window.scan_id, scan)
                window.controller.cancel.assert_not_called()
                window.recon.cancel.assert_not_called()
                window.prep_controller.cancel.assert_not_called()

    def test_other_fight_loading_clears_early_roster(self):
        window = prep_window()
        self.resolve(window)
        window.handle_lifecycle({"phase": "loading", "fight_id": "980022"})
        self.assertIsNone(window.current_roster)
        self.assertIsNone(window.scan_id)
        window.controller.cancel.assert_called_once_with()
        window.recon.cancel.assert_called_once_with()

    def test_new_generation_rejects_old_status_and_resolved_events(self):
        window = prep_window()
        window.prep_generation = 42
        window.status.set.reset_mock()
        window.prep_controller.poll_events = lambda: [{"type": "status", "generation": 41, "message": "旧消息"}, resolved_event()]
        window.poll_prep()
        window.controller.start.assert_not_called()
        window.status.set.assert_not_called()

    def test_authoritative_roster_replaces_screen_identity_and_rejects_late_preparation(self):
        window = prep_window()
        self.resolve(window)
        authoritative = Roster("980021", [{"id": 10, "name": "本人", "team": "Alpha"},
                                           {"id": 40, "name": "正式对手", "team": "Bravo"}], "now", "GameLogs/test.log", "now")
        window.watcher = SimpleNamespace(poll_events=queue_poll([{"type": "roster", "roster": authoritative}]))
        window.poll_log()
        self.assertIs(window.current_roster, authoritative)
        self.assertEqual(window.enemy_ids, [40])
        self.assertEqual(window.current_roster.source, "game_log")
        self.assertIsNone(window.prep_generation)
        window.prep_controller.cancel.assert_called_once_with()
        window.controller.start.reset_mock()
        self.resolve(window)
        self.assertIs(window.current_roster, authoritative)
        window.controller.start.assert_not_called()

    def test_preparation_report_keeps_identity_source_and_unresolved_rows_explicit(self):
        for source in ("screen", "manual"):
            with self.subTest(source=source):
                window = prep_window()
                self.resolve(window, source=source)
                text = build_scout_report(window.current_roster, {}, {}, {}, window.enemy_ids,
                                          is_replay=False, data_summary=window.summary,
                                          opening20_states={}, data_summary20=window.summary20)
                self.assertIn("待游戏", text)
                self.assertIn("尚未由游戏数字 ID 名单校核", text)
                self.assertIn("同名两人待选择", text)
                self.assertIn("第五行未识别", text)
                self.assertIn("未匹配的人未计入统计", text)
                self.assertIn("对手 · 3 人", text)
                self.assertNotIn("模式：当前阵容查询", text)
                self.assertIn("待游戏ID校核", summary_sections(window.summary)[0][1])
                self.assertIn("模式：准备姓名名单（待游戏ID校核）", summary_text(window.summary))
                encoded = asdict(window.current_roster)
                self.assertEqual(encoded["source"], "screen_names" if source == "screen" else "manual_names")
                self.assertEqual(encoded["self_team"], "Alpha")

    def test_manual_id_correction_cancels_old_round_without_restart_from_waiting(self):
        window = prep_window()
        window.live_phase, window.live_fight_id = "waiting", "980021"
        rows = [{"team": "Bravo", "index": 0, "variants": ["同名"], "player_id": 77, "name": "同名"}]
        window.start_prep_manual(rows)
        self.assertIsNone(window.current_roster)
        window.controller.cancel.assert_called_once_with()
        window.recon.cancel.assert_called_once_with()
        window.prep_controller.cancel.assert_called_once_with()
        window.prep_controller.start.assert_called_once_with("980021", {}, manual_rows=rows)
        self.assertEqual(window.prep_generation, 42)

    def test_manual_dialog_only_forwards_explicitly_selected_same_name_id(self):
        for selected in ("", "同名 · ID 77"):
            with self.subTest(selected=selected):
                key = ("Bravo", 0)
                dialog = SimpleNamespace(entries={key: Mock(get=lambda: "同名")},
                                         selectors={key: Mock(get=lambda: selected)},
                                         candidates={key: {"同名 · ID 76": {"id": 76, "name": "同名"},
                                                           "同名 · ID 77": {"id": 77, "name": "同名"}}},
                                         owner=SimpleNamespace(start_prep_manual=Mock()), note=Mock())
                PrepDialog.query_names(dialog)
                row = dialog.owner.start_prep_manual.call_args.args[0][0]
                if selected:
                    self.assertEqual(row["player_id"], 77)
                else:
                    self.assertNotIn("player_id", row)
                    self.assertEqual(row["variants"], ["同名"])

    def test_partial_dialog_update_clears_all_old_names_selections_and_candidates(self):
        dialog = old_dialog()
        current_key = ("Bravo", 2)
        dialog.update_rows([{"team": current_key[0], "index": current_key[1], "variants": ["本局新姓名"],
                             "resolution": {"candidates": [{"id": 77, "name": "本局新姓名"}]}}])
        for key in dialog.entries:
            self.assertEqual(dialog.selectors[key].get(), "")
            if key == current_key:
                self.assertEqual(dialog.entries[key].get(), "本局新姓名")
                self.assertEqual(dialog.candidates[key], {"本局新姓名 · ID 77": {"id": 77, "name": "本局新姓名"}})
            else:
                self.assertEqual(dialog.entries[key].get(), "")
                self.assertEqual(dialog.candidates[key], {})
                dialog.selectors[key].config.assert_called_with(values=[])
        PrepDialog.query_names(dialog)
        submitted = dialog.owner.start_prep_manual.call_args.args[0]
        self.assertEqual(submitted, [{"team": "Bravo", "index": 2, "variants": ["本局新姓名"]}])

    def test_lifecycle_clears_open_dialog_before_another_match_can_be_submitted(self):
        for phase in ("waiting", "ended", "lobby"):
            with self.subTest(phase=phase):
                window = prep_window()
                window.config["prep_enabled"] = False
                window.prep_dialog = old_dialog()
                window.handle_lifecycle({"phase": phase, "fight_id": "980022" if phase == "waiting" else None})
                dialog = window.prep_dialog
                self.assertTrue(all(entry.get() == "" for entry in dialog.entries.values()))
                self.assertTrue(all(selector.get() == "" for selector in dialog.selectors.values()))
                self.assertTrue(all(candidates == {} for candidates in dialog.candidates.values()))
                PrepDialog.query_names(dialog)
                dialog.owner.start_prep_manual.assert_not_called()

    def test_own_id_in_opponent_region_refuses_both_query_pipelines_and_requests_correction(self):
        for source in ("screen", "manual"):
            with self.subTest(source=source):
                window = prep_window()
                window.current_roster = None
                window.players = []
                event = resolved_event(source=source)
                event["players"][0].update(id=window.config["self_id"], name="本人被放到对手区域")
                event["rows"][0]["resolution"].update(player_id=window.config["self_id"], name="本人被放到对手区域")
                window.prep_controller.poll_events = queue_poll([event])
                window.poll_prep()
                window.controller.start.assert_not_called()
                window.recon.start.assert_not_called()
                self.assertIsNone(window.current_roster)
                self.assertEqual(window.players, [])
                self.assertEqual(window.prep_rows, event["rows"])
                self.assertIn("自己的 ID 出现在对手区域", window.status.set.call_args.args[0])
                self.assertIn("核对双方分组", window.status.set.call_args.args[0])
                window.compact.set.assert_called_with("准备阶段 · 阵营待校正")

    def test_placeholder_status_waits_for_real_names_without_starting_stats(self):
        window = prep_window()
        window.current_roster = None
        window.players = []
        window.started = None
        window.scanning = False
        events = [{"type": "status", "generation": 41, "status": "names_unavailable",
                   "message": "敌方栏暂显示 Player name 占位文字；等待真实昵称出现后自动查询。",
                   "started": None, "deadline": None, "placeholder_count": 5}]
        window.prep_controller.poll_events = queue_poll(events)
        window.poll_prep()
        window.controller.start.assert_not_called()
        window.recon.start.assert_not_called()
        self.assertIsNone(window.started)
        self.assertIsNone(window.current_roster)
        window.compact.set.assert_called_with("等待真实对手昵称")
        window.summary_heading.set.assert_called_with("敌方姓名暂未显示")
        self.assertIn("出现真实名字后自动继续", window.summary_confidence.set.call_args.args[0])
        self.resolve(window)
        self.assertEqual(window.current_roster.source, "screen_names")
        self.assertEqual(window.enemy_ids, [30, 31, 32])
        self.assertNotEqual(window.summary_heading.set.call_args.args[0], "敌方姓名暂未显示")
        self.assertEqual(window.started, 100.0)

    def test_request_limit_is_visible_without_claiming_all_names_are_confirmed(self):
        window = prep_window()
        event = {"type": "status", "generation": 41, "status": "request_limit",
                 "message": "本轮姓名搜索上限（8次），部分待确认。"}
        window.prep_controller.poll_events = queue_poll([event])
        with self.assertLogs(level="INFO") as log:
            window.poll_prep()
        window.compact.set.assert_called_with("准备阶段 · 姓名搜索已达上限")
        window.summary_heading.set.assert_called_with("准备名单 · 部分待确认")
        self.assertIn("部分待确认", window.summary_confidence.set.call_args.args[0])
        self.assertIn("status=request_limit", "\n".join(log.output))
        window.controller.start.assert_not_called()

    def test_waiting_expands_collapsed_window_before_any_ids_and_logs_start(self):
        window = prep_window()
        window.expanded = False
        window.toggle = Mock(side_effect=lambda: setattr(window, "expanded", True))
        with self.assertLogs(level="INFO") as log:
            window.handle_lifecycle({"phase": "waiting", "fight_id": "980022"})
        window.toggle.assert_called_once_with()
        self.assertTrue(window.expanded)
        self.assertIsNone(window.current_roster)
        self.assertIn("正在读取姓名", window.summary_heading.set.call_args.args[0])
        self.assertIn("Preparation started fight=980022 generation=42", "\n".join(log.output))
        self.assertIn("Preparation cancelled fight=980021 generation=41", "\n".join(log.output))
        window.root.focus_force.assert_not_called()
        window.root.lift.assert_not_called()
        window.controller.start.assert_not_called()

    def test_insufficient_rows_are_visible_in_main_summary_and_collapsed_bar(self):
        window = prep_window()
        event = {"type": "status", "generation": 41, "status": "waiting", "fight_id": "980021",
                 "message": "等待连续两帧稳定昵称：当前可查询 1/5 行，已稳定 1/5 行。"}
        window.prep_controller.poll_events = queue_poll([event])
        with self.assertLogs(level="INFO") as log:
            window.poll_prep()
        self.assertIn("1/5", window.compact.set.call_args.args[0])
        self.assertIn("当前可查询 1/5", window.summary_confidence.set.call_args.args[0])
        body = "".join(call.args[1] for call in window.summary_text.insert.call_args_list)
        self.assertIn("当前可查询 1/5", body)
        self.assertIn("queryable=1 stable=1", "\n".join(log.output))
        window.controller.start.assert_not_called()

    def test_capture_failure_is_visible_and_logs_only_codes_not_raw_candidates(self):
        for capture, visible in (("not_foreground", "请切回游戏"), ("occluded", "遮挡"),
                                 ("black_frame", "未能读取"), ("timeout", "超时")):
            with self.subTest(capture=capture):
                window = prep_window()
                event = {"type": "status", "generation": 41, "status": "capture_error", "capture_status": capture,
                         "message": "原始失败信息_私有昵称_123", "rows": [],
                         "candidates": [{"name": "绝不能记入日志的候选", "id": 9988}]}
                window.prep_controller.poll_events = queue_poll([event, event])
                with self.assertLogs(level="INFO") as log:
                    window.poll_prep()
                self.assertIn(visible, window.compact.set.call_args.args[0])
                self.assertIn(visible, window.summary_heading.set.call_args.args[0])
                self.assertIn("原始失败信息", window.summary_confidence.set.call_args.args[0])
                text = "\n".join(log.output)
                self.assertEqual(text.count("Preparation status"), 1)
                self.assertIn("capture=" + capture, text)
                self.assertNotIn("私有昵称", text)
                self.assertNotIn("绝不能记入日志", text)
                self.assertNotIn("9988", text)

    def test_invalid_calibration_displays_correction_in_both_views(self):
        window = prep_window()
        window.prep_controller.poll_events = queue_poll([{"type": "status", "generation": 41,
            "status": "waiting", "capture_status": "invalid_geometry", "message": "昵称区域校准无效，暂不查询身份。"}])
        window.poll_prep()
        self.assertIn("区域需校正", window.compact.set.call_args.args[0])
        self.assertIn("校准无效", window.summary_heading.set.call_args.args[0])
        self.assertIn("重新设置", window.summary_confidence.set.call_args.args[0])

    def test_zero_id_finish_records_counts_and_preserves_failure_reason_on_screen(self):
        window = prep_window()
        event = resolved_event()
        event["players"] = []
        event["rows"] = [{"team": "Bravo", "index": i, "variants": ["不应记录的昵称"],
                           "resolution": {"status": "timeout", "reason": "身份查询预算已截止"}}
                          for i in range(5)]
        window.prep_controller.poll_events = queue_poll([event])
        with self.assertLogs(level="INFO") as log:
            window.poll_prep()
        text = "\n".join(log.output)
        self.assertIn("Preparation finished", text)
        self.assertIn('"timeout": 5', text)
        self.assertIn("reason=no_players", text)
        self.assertNotIn("不应记录的昵称", text)
        self.assertIn("预算已截止", window.summary_confidence.set.call_args.args[0])
        self.assertIn("名单待校正", window.compact.set.call_args.args[0])
        window.controller.start.assert_not_called()

    def test_old_generation_does_not_change_diagnostic_log_or_screen(self):
        window = prep_window()
        window.prep_generation = 42
        window.prep_controller.poll_events = queue_poll([{"type": "status", "generation": 41,
            "status": "capture_error", "capture_status": "occluded", "message": "旧轮错误"}, resolved_event()])
        with patch.object(ui.logging, "info") as log:
            window.poll_prep()
        log.assert_not_called()
        window.summary_heading.set.assert_not_called()

    def test_resolver_unmatched_and_incomplete_remain_distinct_in_diagnostic_counts(self):
        window = prep_window()
        event = resolved_event()
        event['players'] = []
        event['rows'] = [{'resolution': {'status': status}} for status in ('unmatched', 'incomplete')]
        window.prep_controller.poll_events = queue_poll([event])
        with self.assertLogs(level='INFO') as log:
            window.poll_prep()
        text = '\n'.join(log.output)
        self.assertIn('"unmatched": 1', text)
        self.assertIn('"incomplete": 1', text)
        self.assertNotIn('"other"', text)

    def test_cancel_logs_previous_generation_summary_then_clears_diagnostic_state(self):
        window = prep_window()
        window.prep_last_status_key = (41, "waiting", None, 1, 1)
        with self.assertLogs(level="INFO") as log:
            window.handle_lifecycle({"phase": "loading", "fight_id": "980021"})
        self.assertIn("Preparation cancelled fight=980021 generation=41", "\n".join(log.output))
        self.assertIn("reason=loading", "\n".join(log.output))
        self.assertIsNone(window.prep_generation)
        self.assertIsNone(window.prep_last_status_key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
