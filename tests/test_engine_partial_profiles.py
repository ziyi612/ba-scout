"""Partial public profiles retain facts without becoming complete statistics."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from ba_scout.engine import ScanController
from tests.test_engine import TEMP_ROOT, player_data, roster, wait_finished
from tests.test_incremental_data import collect, eventually


def partial_profile(pid, **extra):
    return player_data(pid, core_ready=False, profile_incomplete=True,
                       rating=2193, recent_match_ids=['100', '99'], **extra)


class PartialProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='partial-profile-', dir=TEMP_ROOT)
        self.path = Path(self.temp.name)
        self.controllers = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for controller in self.controllers:
            controller.close()
        self.temp.cleanup()

    def controller(self, fetcher):
        ctrl = ScanController(str(self.path), fetcher, ttl=1)
        self.controllers.append(ctrl)
        return ctrl

    def stale_cache(self):
        stamp = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
        data = player_data(6, core_ready=True, fetched_at=stamp, rating=2001,
                           wins=100, recent_match_ids=['80'])
        path = self.path/'6.json'
        path.write_text(json.dumps({'schema': 1, 'player_id': 6, 'stored_at': stamp, 'data': data}), encoding='utf-8')
        return stamp, path.read_bytes()

    def test_fresh_partial_is_visible_but_never_cached_or_counted_complete(self):
        ctrl = self.controller(lambda pid, timeout: partial_profile(pid))
        ctrl.start(roster(ids=[6]), 1)
        events = wait_finished(ctrl)
        event = next(e for e in events if e['type'] == 'player')
        self.assertEqual(event['status'], 'partial')
        self.assertEqual(event['data'], event['partial_data'])
        self.assertEqual(event['data']['rating'], 2193)
        self.assertEqual(event['partial_data']['recent_match_ids'], ['100', '99'])
        self.assertFalse(event['data']['from_cache'])
        self.assertIn('累计战绩暂缺', event['message'])
        self.assertEqual(events[-1]['complete'], 0)
        self.assertFalse((self.path/'6.json').exists())

    def test_successful_refresh_with_missing_history_preserves_original_list_and_timestamp_on_disk(self):
        stamp, _ = self.stale_cache()
        ctrl = self.controller(lambda pid, timeout: player_data(pid, core_ready=True,
                               recent_match_ids=[], recent_history_available=False))
        ctrl.start(roster(ids=[6]), 1)
        events = wait_finished(ctrl)
        fresh = next(e['data'] for e in events if e.get('status') == 'ok')
        self.assertEqual(fresh['recent_match_ids'], ['80'])
        self.assertEqual(fresh['history_fetched_at'], stamp)
        self.assertTrue(fresh['history_from_cache'])
        stored = json.loads((self.path/'6.json').read_text('utf-8'))['data']
        self.assertEqual(stored['history_fetched_at'], stamp)
        self.assertEqual(stored['recent_match_ids'], ['80'])


    def test_partial_refresh_keeps_old_stats_and_new_basic_fields_separate(self):
        stamp, original = self.stale_cache()
        ctrl = self.controller(lambda pid, timeout: partial_profile(pid))
        ctrl.start(roster(ids=[6]), 1)
        events = wait_finished(ctrl)
        rows = [e for e in events if e['type'] == 'player']
        self.assertEqual([e['status'] for e in rows], ['stale', 'partial'])
        event = rows[-1]
        self.assertEqual(event['data']['rating'], 2001)
        self.assertEqual(event['data']['wins'], 100)
        self.assertEqual(event['data']['fetched_at'], stamp)
        self.assertTrue(event['data']['from_cache'])
        self.assertEqual(event['partial_data']['rating'], 2193)
        self.assertFalse(event['partial_data']['from_cache'])
        self.assertNotIn('wins', event['partial_data'])
        self.assertEqual((self.path/'6.json').read_bytes(), original)
        self.assertEqual(events[-1]['complete'], 0)

    def test_prefetched_partial_has_same_contract_with_or_without_old_cache(self):
        for old_cache in (False, True):
            with self.subTest(old_cache=old_cache):
                if old_cache:
                    stamp, original = self.stale_cache()
                calls = []
                ctrl = self.controller(lambda pid, timeout: (calls.append(pid), partial_profile(pid))[1])
                token = ctrl.begin_prefetch('100', time.monotonic()+1)
                ctrl.prefetch(token, 6)
                eventually(lambda: ctrl._prefetch.records[6].phase == 'completed')
                ctrl.start(roster(ids=[6]), 1, prefetch_token=token)
                events = wait_finished(ctrl)
                event = [e for e in events if e['type'] == 'player'][-1]
                self.assertEqual(event['status'], 'partial')
                self.assertEqual(event['partial_data']['recent_match_ids'], ['100', '99'])
                self.assertEqual(calls, [6])
                self.assertEqual(events[-1]['complete'], 0)
                if old_cache:
                    self.assertEqual(event['data']['fetched_at'], stamp)
                    self.assertTrue(event['data']['from_cache'])
                    self.assertEqual((self.path/'6.json').read_bytes(), original)
                else:
                    self.assertEqual(event['data'], event['partial_data'])
                    self.assertFalse((self.path/'6.json').exists())
                ctrl.close()

    def test_revoked_partial_retained_privately_then_reused_once(self):
        entered, release, calls = threading.Event(), threading.Event(), []
        self.releases.append(release)
        def fetch(pid, timeout):
            calls.append(pid)
            entered.set()
            release.wait(1)
            return partial_profile(pid)
        ctrl = self.controller(fetch)
        scan_id = ctrl.start(roster(ids=[6]), 1, deadline_seconds=1.5)
        self.assertTrue(entered.wait(.5))
        ctrl.update_roster(roster(ids=[]), 1, deadline_seconds=1.5)
        ctrl.poll_events()
        release.set()
        eventually(lambda: 6 in ctrl._active.responses)
        self.assertFalse(any(e.get('player_id') == 6 for e in ctrl.poll_events()))
        self.assertEqual(ctrl.update_roster(roster(ids=[6]), 1, deadline_seconds=1.5), scan_id)
        events = collect(ctrl, lambda es: any(e['type'] == 'finished' for e in es))
        event = next(e for e in events if e['type'] == 'player')
        self.assertEqual(event['status'], 'partial')
        self.assertEqual(event['partial_data']['player_id'], 6)
        self.assertEqual(calls, [6])
        self.assertFalse((self.path/'6.json').exists())

    def test_wrong_identity_never_produces_partial_event(self):
        for wrong in (999, True):
            with self.subTest(wrong=wrong):
                ctrl = self.controller(lambda pid, timeout: partial_profile(wrong))
                ctrl.start(roster(ids=[6]), 1)
                events = wait_finished(ctrl)
                event = next(e for e in events if e['type'] == 'player')
                self.assertEqual(event['status'], 'error')
                self.assertIsNone(event['data'])
                self.assertNotIn('partial_data', event)
                self.assertFalse((self.path/'6.json').exists())
                ctrl.close()


if __name__ == '__main__':
    unittest.main()
