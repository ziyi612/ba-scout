"""Late preparation names join real worker pipelines without resetting peers."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import tempfile
import threading
import time
import unittest

from ba_scout.engine import ScanController
from ba_scout.scouting_engine import BattleRecon
from tests.test_engine import player_data, roster
from tests.test_scouting_engine import match_data


TEMP_ROOT = Path(__file__).resolve().parents[1] / 'work' / 'tests-temp'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def eventually(predicate, timeout=1.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.002)
    raise AssertionError('Condition did not become true')


def collect(controller, predicate, timeout=1.5):
    events = []

    def poll():
        events.extend(controller.poll_events())
        return predicate(events)

    eventually(poll, timeout)
    return events


class IncrementalDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='incremental-data-', dir=TEMP_ROOT)
        self.path = Path(self.tmp.name)
        self.controllers = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for controller in self.controllers:
            controller.close()
        self.tmp.cleanup()

    def profile(self, fetcher, **kwargs):
        ctrl = ScanController(str(self.path / 'profile'), fetcher, **kwargs)
        self.controllers.append(ctrl)
        return ctrl

    def battle(self, fetcher):
        ctrl = BattleRecon(str(self.path / 'match'), fetcher)
        self.controllers.append(ctrl)
        return ctrl

    def release(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def test_profiles_late_fourth_fifth_keep_completed_and_inflight_work(self):
        calls, gate, entered = [], self.release(), threading.Event()

        def fetch(pid, timeout):
            calls.append(pid)
            if pid == 3:
                entered.set()
                gate.wait(1)
            return player_data(pid)

        ctrl = self.profile(fetch)
        scan_id = ctrl.start(roster(ids=[1, 2, 3]), 1, deadline_seconds=.8)
        self.assertTrue(entered.wait(.5))
        first = collect(ctrl, lambda es: {1, 2} <= {e.get('player_id') for e in es})
        self.assertEqual(ctrl.update_roster(roster(ids=[1, 2, 3, 4]), 1, deadline_seconds=.8), scan_id)
        self.assertEqual(ctrl.update_roster(roster(ids=[1, 2, 3, 4, 5]), 1, deadline_seconds=.8), scan_id)
        for _ in range(3):
            ctrl.update_roster(roster(ids=[1, 2, 3, 4, 5]), 1, deadline_seconds=.8)
        gate.set()
        events = first + collect(ctrl, lambda es: any(e['type'] == 'finished' and e['total'] == 5 for e in es))
        self.assertEqual(Counter(calls), Counter(range(1, 6)))
        self.assertEqual({e['player_id'] for e in events if e['type'] == 'player'}, set(range(1, 6)))
        self.assertTrue(all(e['scan_id'] == scan_id for e in events))

    def test_finished_profile_round_accepts_new_id_without_refetching_old(self):
        calls = []
        ctrl = self.profile(lambda pid, timeout: (calls.append(pid), player_data(pid))[1])
        scan_id = ctrl.start(roster(ids=[1]), 1, deadline_seconds=.05)
        collect(ctrl, lambda es: any(e['type'] == 'finished' for e in es))
        time.sleep(.06)
        self.assertEqual(ctrl.update_roster(roster(ids=[1, 5]), 1, deadline_seconds=.2), scan_id)
        events = collect(ctrl, lambda es: any(e['type'] == 'finished' and e['total'] == 2 for e in es))
        self.assertEqual(calls, [1, 5])
        self.assertEqual([e['player_id'] for e in events if e['type'] == 'player'], [5])
        self.assertEqual(events[-1]['complete'], 2)

    def test_profile_membership_update_never_extends_existing_id_deadline(self):
        gate = self.release()
        ctrl = self.profile(lambda pid, timeout: (gate.wait(.4), player_data(pid))[1])
        ctrl.start(roster(ids=[1]), 1, deadline_seconds=.055)
        original = ctrl._active.deadlines[1]
        ctrl.update_roster(roster(ids=[1, 2]), 1, deadline_seconds=.4)
        ctrl.update_roster(roster(ids=[1, 2]), 1, deadline_seconds=.4,
                           player_deadlines={1: time.monotonic() + 10, 2: time.monotonic() + 10})
        self.assertEqual(ctrl._active.deadlines[1], original)
        events = collect(ctrl, lambda es: any(e.get('player_id') == 1 and e.get('status') == 'timeout' for e in es))
        self.assertFalse(any(e.get('player_id') == 2 and e.get('status') == 'timeout' for e in events))
        gate.set()
        done = collect(ctrl, lambda es: any(e['type'] == 'finished' for e in es))
        self.assertTrue(any(e.get('player_id') == 2 and e.get('status') == 'ok' for e in done))
        self.assertFalse((self.path / 'profile/1.json').exists())

    def test_profile_explicit_absolute_deadline_is_respected(self):
        gate = self.release()
        ctrl = self.profile(lambda pid, timeout: (gate.wait(.5), player_data(pid))[1] if pid == 2 else player_data(pid))
        ctrl.start(roster(ids=[1]), 1, deadline_seconds=.5)
        absolute = time.monotonic() + .04
        ctrl.update_roster(roster(ids=[1, 2]), 1, deadline_seconds=.5, player_deadlines={2: absolute})
        self.assertEqual(ctrl._active.deadlines[2], absolute)
        es = collect(ctrl, lambda es: any(e.get('player_id') == 2 and e.get('status') == 'timeout' for e in es))
        self.assertTrue(es)

    def test_removed_inflight_profile_never_publishes_or_writes_cache_and_readd_reuses(self):
        gate, entered, returned, calls = self.release(), threading.Event(), threading.Event(), []

        def fetch(pid, timeout):
            calls.append(pid)
            entered.set()
            gate.wait(.5)
            returned.set()
            return player_data(pid)

        ctrl = self.profile(fetch)
        ctrl.start(roster(ids=[1]), 1, deadline_seconds=.7)
        self.assertTrue(entered.wait(.5))
        original = ctrl._active.deadlines[1]
        ctrl.update_roster(roster(ids=[]), 1, deadline_seconds=.7)
        gate.set()
        self.assertTrue(returned.wait(.5))
        eventually(lambda: 1 in ctrl._active.responses)
        self.assertFalse(any(e.get('player_id') == 1 for e in ctrl.poll_events()))
        self.assertFalse((self.path / 'profile/1.json').exists())
        ctrl.update_roster(roster(ids=[1]), 1, deadline_seconds=.7)
        es = collect(ctrl, lambda es: any(e.get('player_id') == 1 and e.get('status') == 'ok' for e in es))
        self.assertEqual(calls, [1])
        self.assertEqual(ctrl._active.deadlines[1], original)
        self.assertTrue(es)

    def test_removed_queued_profile_can_rejoin_without_duplicate_request(self):
        gate, entered, calls = self.release(), threading.Event(), []

        def fetch(pid, timeout):
            calls.append(pid)
            if pid == 1:
                entered.set()
                gate.wait(.5)
            return player_data(pid)

        ctrl = self.profile(fetch, concurrency=1)
        ctrl.start(roster(ids=[1, 2]), 99, deadline_seconds=.8)
        self.assertTrue(entered.wait(.5))
        ctrl.update_roster(roster(ids=[1]), 99, deadline_seconds=.8)
        gate.set()
        eventually(lambda: 2 not in ctrl._active.scheduled)
        ctrl.update_roster(roster(ids=[1, 2]), 99, deadline_seconds=.8)
        collect(ctrl, lambda es: any(e['type'] == 'finished' and e['total'] == 2 for e in es))
        self.assertEqual(calls, [1, 2])

    def test_profile_removed_finished_events_are_filtered_and_cancel_cannot_revive(self):
        ctrl = self.profile(lambda pid, timeout: player_data(pid))
        ctrl.start(roster(ids=[1]), 1, deadline_seconds=.5)
        eventually(lambda: ctrl._active.finished)
        ctrl.update_roster(roster(ids=[1, 2]), 1, deadline_seconds=.5)
        events = collect(ctrl, lambda es: any(e['type'] == 'finished' for e in es))
        self.assertTrue(all(e['total'] == 2 for e in events if e['type'] == 'finished'))
        ctrl.cancel()
        with self.assertRaises(ValueError):
            ctrl.update_roster(roster(ids=[1, 2, 3]), 1)
        self.assertEqual(ctrl.poll_events(), [])

    def test_profile_update_rejects_wrong_match_duplicate_and_bad_deadline(self):
        ctrl = self.profile(lambda pid, timeout: player_data(pid))
        ctrl.start(roster(ids=[1]), 1)
        for data, kwargs in ((roster('other', [1]), {}), (roster(ids=[1, 1]), {}),
                             (roster(ids=[1]), {'deadline_seconds': float('nan')}),
                             (roster(ids=[1]), {'player_deadlines': {1: float('inf')}})):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ctrl.update_roster(data, 1, **kwargs)
        ctrl.close()
        with self.assertRaises(RuntimeError):
            ctrl.update_roster(roster(ids=[1]), 1)

    def test_late_history_player_reuses_running_shared_page(self):
        gate, entered, calls = self.release(), threading.Event(), []

        def fetch(mid, timeout):
            calls.append(mid)
            entered.set()
            gate.wait(.5)
            return match_data(mid, players=(1, 2))

        ctrl = self.battle(fetch)
        scan_id = ctrl.start([1], deadline=.7)
        ctrl.submit_history(1, [100])
        self.assertTrue(entered.wait(.5))
        self.assertEqual(ctrl.update_players([1, 2], deadline=.7), scan_id)
        ctrl.submit_history(2, [100])
        gate.set()
        es = collect(ctrl, lambda es: len([e for e in es if e['type'] == 'opening' and e['finished']]) == 2)
        self.assertEqual(calls, [100])
        self.assertTrue(all(e['scan_id'] == scan_id for e in es))
        self.assertEqual({e['player_id'] for e in es if e['type'] == 'opening' and e['finished']}, {1, 2})

    def test_finished_history_round_accepts_late_player_and_reuses_completed_page(self):
        calls = []
        ctrl = self.battle(lambda mid, timeout: (calls.append(mid), match_data(mid, players=(1, 5)))[1])
        scan_id = ctrl.start([1], deadline=.045)
        ctrl.submit_history(1, [100])
        collect(ctrl, lambda es: any(e['type'] == 'opening' and e['finished'] for e in es))
        eventually(lambda: ctrl._active.finished)
        self.assertEqual(ctrl.update_players([1, 5], deadline=.3), scan_id)
        ctrl.submit_history(5, [100])
        es = collect(ctrl, lambda es: any(e.get('player_id') == 5 and e['type'] == 'opening' and e['finished'] for e in es))
        self.assertEqual(calls, [100])
        self.assertTrue(all(e['player_id'] == 5 for e in es))
        final = next(e for e in es if e['type'] == 'opening' and e['finished'])
        self.assertEqual(final['data']['sample_count'], 1)
        self.assertEqual(set(final['windows']), {'20', '120'})

    def test_history_per_player_expiry_does_not_timeout_later_peer(self):
        gate = self.release()
        ctrl = self.battle(lambda mid, timeout: (gate.wait(.4), match_data(mid, players=(1, 2)))[1])
        ctrl.start([1], deadline=.05)
        original = ctrl._active.deadlines[1]
        ctrl.submit_history(1, [100])
        ctrl.update_players([1, 2], deadline=.4)
        ctrl.submit_history(2, [101])
        ctrl.update_players([1, 2], deadline=.4, player_deadlines={1: time.monotonic()+10})
        self.assertEqual(ctrl._active.deadlines[1], original)
        first = collect(ctrl, lambda es: any(e['type'] == 'opening' and e['finished'] and e['player_id'] == 1 for e in es))
        self.assertFalse(any(e['type'] == 'opening' and e['finished'] and e['player_id'] == 2 for e in first))
        gate.set()
        last = collect(ctrl, lambda es: any(e['type'] == 'opening' and e['finished'] and e['player_id'] == 2 for e in es))
        self.assertEqual(next(e for e in last if e['type'] == 'opening' and e['finished'])['status'], 'ok')

    def test_history_explicit_deadline_and_duplicate_update_do_not_extend(self):
        ctrl = self.battle(lambda mid, timeout: match_data(mid))
        ctrl.start([1], deadline=.5)
        end = time.monotonic() + .04
        ctrl.update_players([1, 2], deadline=.4, player_deadlines={2: end})
        ctrl.update_players([1, 2], deadline=.4, player_deadlines={2: end+20})
        self.assertEqual(ctrl._active.deadlines[2], end)
        es = collect(ctrl, lambda es: any(e['type'] == 'opening' and e['finished'] and e['player_id'] == 2 for e in es))
        self.assertEqual(next(e for e in es if e['type'] == 'opening' and e['finished'])['status'], 'timeout')

    def test_removed_history_inflight_has_no_public_event_or_cache_then_reuses_private_page(self):
        gate, entered, calls = self.release(), threading.Event(), []

        def fetch(mid, timeout):
            calls.append(mid)
            entered.set()
            gate.wait(.5)
            return match_data(mid, players=(1, 2))

        ctrl = self.battle(fetch)
        ctrl.start([1], deadline=.8)
        ctrl.submit_history(1, [100])
        self.assertTrue(entered.wait(.5))
        ctrl.update_players([], deadline=.8)
        gate.set()
        eventually(lambda: 100 in ctrl._active.results)
        self.assertEqual(ctrl.poll_events(), [])
        self.assertFalse((self.path/'match/100.json').exists())
        ctrl.update_players([2], deadline=.8)
        ctrl.submit_history(2, [100])
        es = collect(ctrl, lambda es: any(e['type'] == 'opening' and e['finished'] for e in es))
        self.assertTrue(all(e['player_id'] == 2 for e in es))
        self.assertEqual(calls, [100])

    def test_history_remove_readd_filters_old_events_and_preserves_original_deadline(self):
        calls = []
        ctrl = self.battle(lambda mid, timeout: (calls.append(mid), match_data(mid))[1])
        ctrl.start([1], deadline=.5)
        end = ctrl._active.deadlines[1]
        ctrl.submit_history(1, [100])
        eventually(lambda: 1 in ctrl._active.opening_terminal)
        ctrl.update_players([], deadline=.5)
        self.assertEqual(ctrl.poll_events(), [])
        ctrl.update_players([1], deadline=.5)
        es = collect(ctrl, lambda es: any(e['type'] == 'opening' and e['finished'] for e in es))
        self.assertEqual(calls, [100])
        self.assertEqual(ctrl._active.deadlines[1], end)
        self.assertEqual(next(e for e in es if e['type'] == 'opening' and e['finished'])['status'], 'ok')

    def test_history_cancelled_round_cannot_revive_and_bad_inputs_rejected(self):
        ctrl = self.battle(lambda mid, timeout: match_data(mid))
        ctrl.start([1])
        for ids, kwargs in (([False], {}), ([1], {'deadline': float('nan')}),
                            ([1], {'player_deadlines': {1: float('inf')}})):
            with self.subTest(ids=ids, kwargs=kwargs), self.assertRaises(ValueError):
                ctrl.update_players(ids, **kwargs)
        ctrl.cancel()
        with self.assertRaises(ValueError):
            ctrl.update_players([1, 2])
        ctrl.close()
        with self.assertRaises(RuntimeError):
            ctrl.update_players([1])


if __name__ == '__main__':
    unittest.main()
