"""Private preparation prefetch: no network, GUI, or non-F scratch writes."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError

from ba_scout.engine import ScanController
from tests.test_engine import player_data, roster, wait_finished

TEMP_ROOT = Path(__file__).resolve().parents[1] / 'work' / 'tests-temp'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def until(predicate, timeout=1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError('Prefetch condition did not become true')


class PrefetchTests(unittest.TestCase):
    def test_inflight_prefetch_progress_released_only_after_adoption_and_never_after_cancel(self):
        first, next_chunk, done = threading.Event(), threading.Event(), threading.Event()
        self.releases.extend([next_chunk, done])
        def fetch(pid, timeout, on_progress):
            on_progress(player_data(pid, recent_match_ids=['88'], core_ready=False))
            first.set()
            next_chunk.wait(2)
            on_progress(player_data(pid, recent_match_ids=['89', '88'], core_ready=False))
            done.set()
            return player_data(pid)
        ctrl = self.controller(fetch, stream_progress=True)
        token = ctrl.begin_prefetch('100', time.monotonic() + 2)
        ctrl.prefetch(token, 6)
        self.assertTrue(first.wait(1))
        self.assertEqual(ctrl.poll_events(), [])
        scan = ctrl.start(roster(ids=[6]), 1, prefetch_token=token)
        events = []
        def adopted():
            events.extend(ctrl.poll_events())
            return any(e['type'] == 'player_progress' for e in events)
        until(adopted)
        progress = next(e for e in events if e['type'] == 'player_progress')
        self.assertEqual((progress['scan_id'], progress['status'], progress['partial_data']['recent_match_ids']),
                         (scan, 'loading', ['88']))
        self.assertFalse(any(e['type'] == 'finished' for e in events))
        ctrl.cancel()
        next_chunk.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(ctrl.poll_events(), [])

    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='profile-prefetch-', dir=TEMP_ROOT)
        self.path = Path(self.temp.name)
        self.controllers, self.releases = [], []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        for release in self.releases:
            release.set()
        self.temp.cleanup()

    def controller(self, fetcher, **kwargs):
        controller = ScanController(str(self.path), fetcher, **kwargs)
        self.controllers.append(controller)
        return controller

    def completed(self, controller, player_id):
        with controller._lock:
            scope = controller._prefetch
            return scope is not None and scope.records[player_id].phase == 'completed'

    def test_completed_prefetch_is_private_then_final_scan_uses_it_once(self):
        calls = []
        controller = self.controller(lambda pid, timeout: (calls.append(pid), player_data(pid))[1])
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        self.assertTrue(controller.prefetch(token, 6))
        self.assertTrue(controller.prefetch(token, 6))
        until(lambda: self.completed(controller, 6))
        self.assertEqual(controller.poll_events(), [])
        self.assertFalse(list(self.path.iterdir()))
        controller.start(roster(ids=[6]), 1, prefetch_token=token)
        events = wait_finished(controller)
        rows = [event for event in events if event['type'] == 'player']
        self.assertEqual([(row['player_id'], row['status']) for row in rows], [(6, 'ok')])
        self.assertEqual(calls, [6])
        self.assertTrue((self.path / '6.json').is_file())

    def test_final_scan_joins_inflight_prefetch_without_duplicate_http(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def fetch(pid, timeout):
            calls.append(pid)
            entered.set()
            release.wait(1)
            return player_data(pid)
        controller = self.controller(fetch)
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(token, 6)
        self.assertTrue(entered.wait(.3))
        controller.start(roster(ids=[6]), 1, prefetch_token=token)
        time.sleep(.02)
        self.assertEqual(calls, [6])
        release.set()
        events = wait_finished(controller)
        self.assertEqual(calls, [6])
        self.assertEqual(sum(e['type'] == 'player' for e in events), 1)
        self.assertEqual(events[-1]['complete'], 1)

    def test_two_prefetch_slots_promote_to_same_five_worker_pool(self):
        release = threading.Event()
        self.releases.append(release)
        lock = threading.Lock()
        calls, active = [], {'current': 0, 'peak': 0}
        def fetch(pid, timeout):
            with lock:
                calls.append(pid)
                active['current'] += 1
                active['peak'] = max(active['peak'], active['current'])
            release.wait(1)
            with lock:
                active['current'] -= 1
            return player_data(pid)
        controller = self.controller(fetch)
        pool = tuple(controller._workers)
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        for pid in range(6, 11):
            self.assertTrue(controller.prefetch(token, pid))
        self.assertFalse(controller.prefetch(token, 11))
        until(lambda: len(calls) == 2)
        time.sleep(.02)
        self.assertEqual(active['peak'], 2)
        controller.start(roster(ids=range(6, 11)), 1, prefetch_token=token)
        until(lambda: len(calls) == 5)
        self.assertEqual(active['peak'], 5)
        self.assertEqual(tuple(controller._workers), pool)
        self.assertTrue(all(worker.daemon for worker in pool))
        release.set()
        events = wait_finished(controller)
        self.assertEqual(Counter(calls), Counter(range(6, 11)))
        self.assertEqual(events[-1]['complete'], 5)

    def test_final_roster_excludes_conflicted_prefetched_id_from_events_and_cache(self):
        calls = []
        controller = self.controller(lambda pid, timeout: (calls.append(pid), player_data(pid))[1])
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(token, 6)
        until(lambda: self.completed(controller, 6))
        controller.start(roster(ids=[7]), 1, prefetch_token=token)
        events = wait_finished(controller)
        self.assertEqual([e['player_id'] for e in events if e['type'] == 'player'], [7])
        self.assertFalse((self.path / '6.json').exists())
        self.assertTrue((self.path / '7.json').exists())

    def test_prefetch_failure_is_final_error_without_automatic_retry(self):
        calls = []
        def fetch(pid, timeout):
            calls.append(pid)
            raise TimeoutError('fake profile timeout')
        controller = self.controller(fetch)
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(token, 6)
        until(lambda: self.completed(controller, 6))
        controller.start(roster(ids=[6]), 1, prefetch_token=token)
        events = wait_finished(controller)
        self.assertEqual(calls, [6])
        row = next(e for e in events if e['type'] == 'player')
        self.assertEqual(row['status'], 'error')
        self.assertIn('fake profile timeout', row['message'])
        self.assertFalse(list(self.path.iterdir()))

    def test_fresh_cache_prevents_prefetch_http_and_stale_failure_preserves_old_data(self):
        calls = []
        def fetch(pid, timeout):
            calls.append(pid)
            raise OSError('fixture failure')
        for pid, age in ((6, 0), (7, 900)):
            stamp = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
            data = player_data(pid, fetched_at=stamp, rating=1800)
            (self.path / f'{pid}.json').write_text(json.dumps(dict(schema=1, player_id=pid, stored_at=stamp, data=data)), encoding='utf-8')
        controller = self.controller(fetch)
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(token, 6)
        controller.prefetch(token, 7)
        until(lambda: self.completed(controller, 6) and self.completed(controller, 7))
        controller.start(roster(ids=[6, 7]), 1, prefetch_token=token)
        rows = {e['player_id']: e for e in wait_finished(controller) if e['type'] == 'player'}
        self.assertEqual(calls, [7])
        self.assertEqual(rows[6]['status'], 'cached')
        self.assertEqual(rows[7]['status'], 'error')
        self.assertEqual(rows[7]['data']['rating'], 1800)
        self.assertTrue(rows[7]['data']['from_cache'])

    def test_absolute_deadline_is_not_reset_on_promotion_and_late_result_cannot_write(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        timeouts = []
        def fetch(pid, timeout):
            timeouts.append(timeout)
            entered.set()
            release.wait(1)
            return player_data(pid)
        controller = self.controller(fetch)
        end = time.monotonic() + .12
        token = controller.begin_prefetch('100', end)
        controller.prefetch(token, 6)
        self.assertTrue(entered.wait(.3))
        time.sleep(.04)
        controller.start(roster(ids=[6]), 1, prefetch_token=token)
        events = wait_finished(controller)
        self.assertLess(time.monotonic() - end, .09)
        self.assertTrue(0 < timeouts[0] <= .12)
        self.assertEqual(next(e for e in events if e['type'] == 'player')['status'], 'timeout')
        release.set()
        until(lambda: controller._prefetch_slots == 0)
        self.assertEqual(controller.poll_events(), [])
        self.assertFalse(list(self.path.iterdir()))

    def test_prefetch_error_racing_cache_prepare_still_emits_stale_before_error(self):
        entered, fail_fetch, cache_reading, release_cache = (threading.Event() for _ in range(4))
        self.releases.extend([fail_fetch, release_cache])
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        data = player_data(6, fetched_at=stamp, recent_match_ids=['123'])
        (self.path / '6.json').write_text(json.dumps(dict(schema=1, player_id=6, stored_at=stamp, data=data)), encoding='utf-8')
        def fetch(pid, timeout):
            entered.set()
            fail_fetch.wait(1)
            raise OSError('fake failure')
        controller = self.controller(fetch)
        original = controller._read_cache
        def delayed_read(pid):
            if threading.current_thread().name.startswith('ba-scout-cache-'):
                cache_reading.set()
                release_cache.wait(1)
            return original(pid)
        controller._read_cache = delayed_read
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(token, 6)
        self.assertTrue(entered.wait(.3))
        controller.start(roster(ids=[6]), 1, prefetch_token=token)
        self.assertTrue(cache_reading.wait(.3))
        fail_fetch.set()
        events = wait_finished(controller)
        release_cache.set()
        rows = [event for event in events if event['type'] == 'player']
        self.assertEqual([row['status'] for row in rows], ['stale', 'error'])
        self.assertEqual(rows[0]['data']['recent_match_ids'], ['123'])
        self.assertTrue(rows[1]['data']['from_cache'])

    def test_two_prefetch_slots_are_shared_across_cancelled_generations(self):
        release = threading.Event()
        self.releases.append(release)
        calls, lock = [], threading.Lock()
        def fetch(pid, timeout):
            with lock:
                calls.append(pid)
            if pid in (6, 7):
                release.wait(1)
            return player_data(pid)
        controller = self.controller(fetch)
        old = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(old, 6)
        controller.prefetch(old, 7)
        until(lambda: len(calls) == 2)
        controller.cancel()
        new = controller.begin_prefetch('101', time.monotonic() + 1)
        controller.prefetch(new, 8)
        controller.prefetch(new, 9)
        time.sleep(.02)
        self.assertEqual(set(calls), {6, 7})
        release.set()
        until(lambda: self.completed(controller, 8) and self.completed(controller, 9))
        self.assertEqual(Counter(calls), Counter([6, 7, 8, 9]))
        self.assertFalse(list(self.path.iterdir()))

    def test_already_expired_matching_token_cannot_start_a_fresh_scan_budget(self):
        calls = []
        controller = self.controller(lambda pid, timeout: (calls.append(pid), player_data(pid))[1])
        token = controller.begin_prefetch('100', time.monotonic() + .02)
        time.sleep(.03)
        self.assertFalse(controller.prefetch(token, 6))
        controller.start(roster(ids=[6]), 1, prefetch_token=token)
        events = wait_finished(controller)
        self.assertEqual(calls, [])
        self.assertTrue(events[-1]['timed_out'])

    def test_cancelled_old_prefetch_cannot_join_new_generation_or_write_cache(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def fetch(pid, timeout):
            calls.append(pid)
            if len(calls) == 1:
                entered.set()
                release.wait(1)
                return player_data(pid, rating=1)
            return player_data(pid, rating=2)
        controller = self.controller(fetch)
        old = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(old, 6)
        self.assertTrue(entered.wait(.3))
        controller.cancel()
        new = controller.begin_prefetch('101', time.monotonic() + 1)
        self.assertFalse(controller.prefetch(old, 6))
        controller.prefetch(new, 6)
        until(lambda: self.completed(controller, 6))
        controller.start(roster('101', [6]), 1, prefetch_token=new)
        events = wait_finished(controller)
        release.set()
        until(lambda: controller._prefetch_slots == 0)
        self.assertEqual(next(e['data']['rating'] for e in events if e['type'] == 'player'), 2)
        self.assertEqual(json.loads((self.path / '6.json').read_text('utf-8'))['data']['rating'], 2)
        self.assertEqual(controller.poll_events(), [])

    def test_prefetch_429_cools_shared_controller_and_is_not_retried_at_commit(self):
        calls = []
        def fetch(pid, timeout):
            calls.append(pid)
            raise HTTPError('https://example.invalid/profile', 429, 'limited', {}, None)
        controller = self.controller(fetch, concurrency=1)
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(token, 6)
        controller.prefetch(token, 7)
        until(lambda: self.completed(controller, 6) and self.completed(controller, 7))
        controller.start(roster(ids=[6, 7, 8]), 1, prefetch_token=token)
        events = wait_finished(controller)
        self.assertEqual(calls, [6])
        self.assertGreater(controller._cooldown_until - time.monotonic(), 59)
        self.assertTrue(all('限流' in e['message'] for e in events if e['type'] == 'player'))
        self.assertFalse(list(self.path.iterdir()))

    def test_close_is_prompt_and_inflight_prefetch_never_writes_unconfirmed_cache(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def fetch(pid, timeout):
            entered.set()
            release.wait(1)
            return player_data(pid)
        controller = self.controller(fetch)
        token = controller.begin_prefetch('100', time.monotonic() + 1)
        controller.prefetch(token, 6)
        self.assertTrue(entered.wait(.3))
        started = time.monotonic()
        controller.close()
        self.assertLess(time.monotonic() - started, .1)
        release.set()
        until(lambda: controller._prefetch_slots == 0)
        self.assertFalse(list(self.path.iterdir()))
        self.assertEqual(controller.poll_events(), [])


if __name__ == '__main__':
    unittest.main()
