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
