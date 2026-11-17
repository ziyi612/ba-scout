"""Scheduling safety at the UI boundary; no real network or game operation."""
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from ba_scout import ui
from tests.test_auto_report import make_window, queue_poll, opening_event
from tests.test_ui_prep import prep_window, resolved_event


class WarmupTests(unittest.TestCase):
    def test_idle_warmup_returns_without_waiting_or_capturing(self):
        pending = Future()
        screen = Mock(submit_warmup=Mock(return_value=pending))
        window = SimpleNamespace(closed=False, args=SimpleNamespace(self_test=False),
                                 config={'prep_enabled': True}, ensure_prep_tools=Mock(),
                                 screen_ocr=screen, ocr_warmup_future=None)
        ui.ScoutWindow.warmup_ocr(window)
        self.assertIs(window.ocr_warmup_future, pending)
        self.assertFalse(pending.done())
        screen.submit_warmup.assert_called_once_with(timeout=8)
        screen.submit_capture_game.assert_not_called()
        ui.ScoutWindow.warmup_ocr(window)
        self.assertEqual(screen.submit_warmup.call_count, 1)

    def test_disabled_or_closed_or_self_test_never_starts_worker(self):
        for key in ('disabled', 'closed', 'self_test'):
            window = SimpleNamespace(closed=key == 'closed', args=SimpleNamespace(self_test=key == 'self_test'),
                                     config={'prep_enabled': key != 'disabled'}, ensure_prep_tools=Mock())
            ui.ScoutWindow.warmup_ocr(window)
            window.ensure_prep_tools.assert_not_called()

    def test_failed_warmup_does_not_prevent_normal_ui_tick(self):
        window = make_window()
        window.export_report = Mock(return_value=None)
        pending = Future()
        pending.set_exception(RuntimeError('test OCR startup failure'))
        window.ocr_warmup_future = pending
        with self.assertLogs(level='WARNING'):
            window.tick()
        self.assertIsNone(window.ocr_warmup_future)
        window.root.after.assert_called_with(60, window.tick)


class PrefetchUiTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(ui.time, 'monotonic', return_value=108.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.window = prep_window()
        self.window.controller.begin_prefetch = Mock(return_value=71)
        self.window.controller.prefetch = Mock(return_value=True)

    def identity(self, **updates):
        event = dict(type='identity_ready', generation=41, fight_id='980021', team='Bravo',
                     index=0, player_id=30, name='准备对手30', started=100., deadline=120., identity_final=False)
        event.update(updates)
        return event

    def send(self, *events):
        self.window.prep_controller.poll_events = queue_poll(list(events))
        self.window.poll_prep()

    def test_individual_identity_prefetches_privately_until_final_roster(self):
        window = self.window
        states, roster = window.states, window.current_roster
        self.send(self.identity(), self.identity(player_id=31, index=1))
        window.controller.begin_prefetch.assert_called_once_with('980021', 120.)
        self.assertEqual(window.controller.prefetch.call_count, 2)
        window.controller.start.assert_not_called()
        window.recon.start.assert_not_called()
        self.assertIs(window.states, states)
        self.assertIs(window.current_roster, roster)
        window.export_report.assert_not_called()
        self.send(resolved_event())
        self.assertEqual(window.controller.start.call_args.kwargs['prefetch_token'], 71)
        self.assertAlmostEqual(window.controller.start.call_args.kwargs['deadline_seconds'], 11.8)
        self.assertEqual(window.enemy_ids, [30, 31, 32])
        self.assertIsNone(window.prep_prefetch_token)

    def test_old_expired_and_ally_rows_do_not_prefetch(self):
        self.send(self.identity(generation=40), self.identity(deadline=107.), self.identity(team='Alpha'))
        self.window.controller.begin_prefetch.assert_not_called()
        self.window.controller.prefetch.assert_not_called()

    def test_cancelled_round_cannot_reuse_old_identity_or_token(self):
        self.send(self.identity())
        self.window.handle_lifecycle(dict(phase='lobby', fight_id=None))
        self.window.controller.prefetch.reset_mock()
        self.send(self.identity())
        self.assertIsNone(self.window.prep_prefetch_token)
        self.window.controller.prefetch.assert_not_called()
        self.window.controller.cancel.assert_called_once_with()

    def test_optional_prefetch_failure_does_not_block_final_normal_query(self):
        self.window.controller.begin_prefetch.side_effect = ValueError('deadline elapsed before dispatch')
        with self.assertLogs(level='WARNING'):
            self.send(self.identity(), resolved_event())
        self.window.controller.start.assert_called_once()
        self.assertNotIn('prefetch_token', self.window.controller.start.call_args.kwargs)
        self.assertEqual(self.window.enemy_ids, [30, 31, 32])

    def test_final_conflict_or_wrong_team_discards_private_results(self):
        for case in ('empty', 'own_enemy', 'expired'):
            with self.subTest(case=case):
                self.window = prep_window()
                self.window.controller.begin_prefetch = Mock(return_value=71)
                self.window.controller.prefetch = Mock(return_value=True)
                self.send(self.identity())
                event = resolved_event()
                if case == 'empty':
                    event['players'] = []
                elif case == 'own_enemy':
                    event['players'][0]['id'] = self.window.config['self_id']
                else:
                    event['deadline'] = 107.
                self.send(event)
                self.assertIsNone(self.window.prep_prefetch_token)
                self.window.controller.cancel.assert_called_once_with()
                self.window.controller.start.assert_not_called()


class ProfileFailureTests(unittest.TestCase):
    def test_failed_profile_reaches_both_windows_terminal_without_waiting_for_deadline(self):
        window = make_window()
        window.export_report = Mock(return_value='F:/test-report.txt')
        recon_events = []
        window.recon.poll_events = queue_poll(recon_events)
        window.recon.mark_unavailable.side_effect = lambda *args: recon_events.append(opening_event(finished=True))
        window.controller.poll_events = queue_poll([
            dict(type='player', scan_id=21, player_id=20, status='error', data=None, message='HTTP 503'),
            dict(type='finished', scan_id=21, elapsed=1),
        ])
        with patch.object(ui.time, 'monotonic', return_value=101.):
            window.tick()
        window.recon.mark_unavailable.assert_called_once_with(20, 'HTTP 503')
        window.export_report.assert_called_once_with(copy_file=False)

    def test_stale_profile_fallback_retains_real_history(self):
        window = make_window()
        window.export_report = Mock(return_value=None)
        window.controller.poll_events = queue_poll([
            dict(type='player', scan_id=21, player_id=20, status='stale', data={'recent_match_ids': [501, 502]}),
            dict(type='player', scan_id=21, player_id=20, status='error', data={'recent_match_ids': [501, 502]}, message='refresh failed'),
        ])
        window.tick()
        window.recon.submit_history.assert_called_once_with(20, [501, 502])
        window.recon.mark_unavailable.assert_not_called()


if __name__ == '__main__':
    unittest.main()
