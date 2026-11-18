"""Incremental preparation UI: preserve data, revoke identities and update reports."""
import unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import Mock, patch

from ba_scout import ui
from tests.test_ui_prep import prep_window, resolved_event
from tests.test_auto_report import queue_poll


def snapshot(revision=1, ids=(30, 31, 32), deadline=120):
    event = resolved_event()
    event.update(progressive=True, revision=revision, final=len(ids) == 5,
                 initial_deadline=120, deadline=deadline,
                 player_deadlines={pid: deadline for pid in ids})
    event['players'] = [{'id': pid, 'name': f'对手{pid}', 'team': 'Bravo', 'index': i}
                        for i, pid in enumerate(ids)]
    return event


class StreamingUiTests(unittest.TestCase):
    def setUp(self):
        self.window = prep_window()
        self.window.live_phase = 'waiting'
        self.window.current_roster = None
        self.window.players = []
        self.window.controller.update_roster = Mock(return_value=22)
        self.window.recon.update_players = Mock(return_value=32)
        self.clock = patch.object(ui.time, 'monotonic', return_value=108)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def emit(self, event):
        self.window.prep_controller.poll_events = queue_poll([event])
        self.window.poll_prep()

    def test_later_two_opponents_append_without_resetting_profile_or_history(self):
        self.emit(snapshot())
        w = self.window
        profile = {'status': 'ok', 'data': {'player_id': 30}}
        opening = {'finished': True, 'data': {'sample_count': 2}}
        w.states[30], w.opening_states[30] = profile, opening
        w.auto_report_attempted_scan_id = w.scan_id
        self.emit(snapshot(2, (30, 31, 32, 33, 34)))
        self.assertEqual(w.enemy_ids, [30, 31, 32, 33, 34])
        self.assertIs(w.states[30], profile)
        self.assertIs(w.opening_states[30], opening)
        self.assertEqual(w.controller.start.call_count, 1)
        self.assertEqual(w.recon.start.call_count, 1)
        w.controller.update_roster.assert_called_once()
        w.recon.update_players.assert_called_once()
        self.assertIsNone(w.auto_report_attempted_scan_id)
        self.assertIn('5/5', w.status.set.call_args.args[0])

    def test_visible_tree_keeps_game_slots_as_names_and_fields_arrive(self):
        try:
            root = ui.tk.Tk()
        except ui.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()
        w = self.window
        columns = ("name", "rating", "record", "spec", "recent", "status")
        w.table = ui.ttk.Treeview(root, columns=columns)
        w.row_font = ui.tkfont.Font(root=root, family="Microsoft YaHei UI", size=9)
        w.table.column("recent", width=194)
        w.render_rows()
        self.assertEqual(w.table.get_children(), tuple(f"slot-{i}" for i in range(5)))

        event = snapshot(ids=(31,))
        event["players"][0]["index"] = 3
        event["rows"] = [{"index": 0, "team": "Bravo", "resolution": {"status": "ambiguous"}},
                         {"index": 2, "team": "Bravo", "resolution": {"status": "timeout"}}]
        self.emit(event)
        self.assertEqual(w.table.get_children(), ("slot-0", "slot-1", "slot-2", "31", "slot-4"))
        self.assertEqual(w.table.set("slot-0", "status"), "重名待确认")
        self.assertEqual(w.table.set("slot-2", "status"), "查名超时")
        self.assertEqual(w.table.set("31", "status"), "查战绩中")
        w.states[31] = {
            "status": "partial", "data": {"player_id": 31, "from_cache": True, "rating": 1900,
                                         "wins": 6, "ranked_games": 10},
            "partial_data": {"player_id": 31, "rating": 2100, "wins": 9},
        }
        w.opening20_states[31] = {"data": {"sample_count": 3, "requested_count": 5,
                                           "units": [{"name": "AH-64D", "matches_with_unit": 2}]}}
        w.render_rows()
        self.assertEqual(w.table.set("31", "rating"), "2,100")
        self.assertIn("60.0%*", w.table.set("31", "record"))
        self.assertIn("AH-64D 2/3局", w.table.set("31", "recent"))
        self.assertEqual(w.table.set("31", "status"), "*旧缓存")

        event = snapshot(2, (32,))
        event["players"][0]["index"] = 1
        event["rows"] = [{"index": 3, "team": "Bravo", "resolution": {"status": "conflict"}}]
        self.emit(event)
        self.assertEqual(w.table.get_children(), ("slot-0", "32", "slot-2", "slot-3", "slot-4"))
        self.assertEqual(w.table.set("slot-3", "status"), "身份冲突")
        w.table.selection_set("slot-3")
        w.on_select()
        self.assertIsNone(w.selected_id)
        self.assertIsNone(ui.ScoutWindow.player_details(w))
        self.assertFalse(w.table.exists("31"))

    def test_current_explicit_zero_and_missing_inputs_remain_distinct(self):
        old = {"player_id": 31, "from_cache": True, "rating": 1800, "wins": 6, "ranked_games": 10,
               "specs": [{"key": "12_7"}]}
        partial = {"player_id": 31, "rating": 0, "wins": 0, "ranked_games": 0,
                   "specs_available": True, "specs": []}
        fields, cached = ui.overview_fields({"data": old, "partial_data": partial}, 31)
        self.assertEqual(fields["rating"], 0)
        self.assertIsNone(fields["win_rate"])
        self.assertEqual(fields["specs"], [])
        self.assertEqual(cached, [])
        fields, _ = ui.overview_fields({"data": old, "partial_data": {**partial, "player_id": 32}}, 31)
        self.assertEqual(fields["rating"], 1800)
        self.assertEqual(fields["win_rate"], 60)

    def test_changed_identity_withdraws_old_results_and_old_revision_cannot_restore(self):
        self.emit(snapshot())
        self.window.states[30] = {'status': 'ok', 'data': {'player_id': 30}}
        self.emit(snapshot(2, (31, 32)))
        self.assertNotIn(30, self.window.states)
        self.assertNotIn(30, self.window.enemy_ids)
        self.emit(snapshot(1))
        self.assertNotIn(30, self.window.enemy_ids)

    def test_empty_revocation_clears_report_and_later_names_restart_in_same_generation(self):
        self.emit(snapshot())
        self.emit(snapshot(2, ()))
        self.assertIsNone(self.window.current_roster)
        self.assertEqual(self.window.states, {})
        self.assertIsNone(self.window.auto_report_path)
        self.assertEqual(self.window.prep_generation, 41)
        self.emit(snapshot(3, (33,)))
        self.assertEqual(self.window.enemy_ids, [33])
        self.assertEqual(self.window.controller.start.call_count, 2)

    def test_waiting_status_does_not_erase_already_visible_data(self):
        self.emit(snapshot())
        self.window.summary_text.delete.reset_mock()
        self.emit(dict(type='status', generation=41, progressive=True,
                       status='stabilizing', message='仍在补齐剩余姓名'))
        self.window.summary_text.delete.assert_not_called()
        self.assertEqual(self.window.enemy_ids, [30, 31, 32])

    def test_loading_cancels_screen_capture_and_keeps_early_data(self):
        self.emit(snapshot())
        before = self.window.current_roster
        self.window.handle_lifecycle(dict(phase='loading', fight_id='980021'))
        self.window.prep_controller.cancel.assert_called_once()
        self.assertIs(self.window.current_roster, before)
        self.window.controller.cancel.assert_not_called()
        self.window.recon.cancel.assert_not_called()
        self.emit(snapshot(2, (30, 31, 32, 33)))
        self.assertEqual(self.window.enemy_ids, [30, 31, 32])

    def test_late_opponent_report_gets_remaining_window_without_requerying_existing(self):
        self.emit(snapshot())
        self.window.prep_initial_report_written = True
        with patch.object(ui.time, 'monotonic', return_value=130):
            self.emit(snapshot(2, (30, 31, 32, 33), deadline=140))
        self.assertEqual(self.window.controller.start.call_count, 1)
        self.assertGreater(self.window.report_due_at, 130)
        self.assertLessEqual(self.window.report_due_at, 140)
        self.assertEqual(self.window.started, 100)

    def test_overlay_raise_is_nonactivating(self):
        self.window.hwnd = 123
        with patch.object(ui, 'user32') as native:
            ui.ScoutWindow.raise_overlay(self.window)
        native.SetWindowPos.assert_called_once_with(123, -1, 0, 0, 0, 0, 0x53)
        native.SetForegroundWindow.assert_not_called()

    def test_initial_partial_report_is_replaced_when_same_roster_data_finishes(self):
        self.emit(snapshot())
        w = self.window
        w.export_report = Mock(return_value=Path('F:/test-report.txt'))
        with patch.object(ui.time, 'monotonic', return_value=120.1):
            w.maybe_auto_report()
        self.assertEqual(w.export_report.call_count, 1)
        self.assertTrue(w.prep_initial_report_written)
        self.assertFalse(w.auto_report_finalized)
        w.scanning = False
        w.opening_states = {pid: {'finished': True} for pid in w.enemy_ids}
        w.opening20_states = {pid: {'finished': True} for pid in w.enemy_ids}
        w.maybe_auto_report()
        self.assertEqual(w.export_report.call_count, 2)
        self.assertTrue(w.auto_report_finalized)
        w.maybe_auto_report()
        self.assertEqual(w.export_report.call_count, 2)


if __name__ == '__main__':
    unittest.main()
