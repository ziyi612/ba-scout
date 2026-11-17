"""Offline timing, deduplication, cache and cancellation checks for recon."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from ba_scout.recon_engine import RecentRecon


TEMP_ROOT = Path(__file__).resolve().parents[1] / 'work' / 'tests-temp'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def match_data(match_id, players=(1, 2, 3)):
    return {
        "match_id": match_id,
        "players": {str(player_id): {"available": True, "player_id": player_id, "units": ["测试单位"]} for player_id in players},
        "source_url": f"https://dash.batrace.top/match/{match_id}",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "match_end_time": "2026-10-25T00:00:00+00:00",
    }


def wait_events(controller, count, timeout=1.0):
    events = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        events.extend(controller.poll_events())
        if len(events) >= count:
            return events
        time.sleep(0.003)
    raise AssertionError(f"Expected {count} events, got {events}")


class ReconEngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="recon-engine-", dir=TEMP_ROOT)
        self.path = Path(self.directory.name)
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        self.directory.cleanup()

    def controller(self, fetcher, **kwargs):
        controller = RecentRecon(str(self.path), fetcher, **kwargs)
        self.controllers.append(controller)
        return controller

    def write_cache(self, match_id, age=0, players=(1, 2, 3)):
        stored_at = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        wrapper = {"schema": 1, "match_id": match_id, "stored_at": stored_at, "data": match_data(match_id, players)}
        (self.path / f"{match_id}.json").write_text(json.dumps(wrapper), encoding="utf-8")

    def test_shared_match_is_fetched_once_and_later_submit_reuses_result(self):
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            time.sleep(0.015)
            return match_data(match_id)

        controller = self.controller(fetch)
        scan_id = controller.start([1, 2, 3])
        controller.submit(1, 100)
        controller.submit(2, 100)
        controller.submit(1, 100)
        events = wait_events(controller, 2)
        controller.submit(3, 100)
        events.extend(wait_events(controller, 1))
        self.assertEqual(calls, [100])
        self.assertEqual({e["player_id"] for e in events}, {1, 2, 3})
        self.assertTrue(all(e["status"] == "ok" and e["scan_id"] == scan_id for e in events))
        self.assertTrue(all(e["source_url"].endswith("/100") for e in events))
        self.assertTrue(all(e["match_end_time"] and e["fetched_at"] for e in events))
        self.assertTrue((self.path / "100.json").exists())
        self.assertFalse(list(self.path.glob("*.tmp")))

    def test_missing_player_and_unavailable_summary_are_errors(self):
        raw = match_data(100, [1, 2])
        raw["players"]["2"] = {"available": False, "reason": "没有单位明细"}
        controller = self.controller(lambda match_id, timeout: raw)
        controller.start([1, 2, 3])
        for player_id in [1, 2, 3]:
            controller.submit(player_id, 100)
        events = {e["player_id"]: e for e in wait_events(controller, 3)}
        self.assertEqual(events[1]["status"], "ok")
        self.assertEqual(events[2]["status"], "error")
        self.assertEqual(events[3]["status"], "error")
        self.assertIsNone(events[2]["data"])
        self.assertIsNone(events[3]["data"])
        self.assertIn("没有单位明细", events[2]["message"])

    def test_missing_only_requested_player_does_not_write_cache(self):
        controller = self.controller(lambda match_id, timeout: match_data(match_id, [99]))
        controller.start([1])
        controller.submit(1, 100)
        self.assertEqual(wait_events(controller, 1)[0]["status"], "error")
        self.assertFalse(list(self.path.glob("*.json")))

    def test_one_day_cache_avoids_fetch_and_expired_cache_refreshes(self):
        self.write_cache(100, age=86300)
        self.write_cache(101, age=86500)
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            return match_data(match_id)

        controller = self.controller(fetch)
        controller.start([1, 2])
        controller.submit(1, 100)
        controller.submit(2, 101)
        events = {e["player_id"]: e for e in wait_events(controller, 2)}
        self.assertEqual(calls, [101])
        self.assertTrue(events[1]["from_cache"])
        self.assertFalse(events[2]["from_cache"])

    def test_deadline_is_from_start_and_late_response_never_writes(self):
        release = threading.Event()
        entered = threading.Event()
        timeouts = []

        def fetch(match_id, timeout):
            entered.set()
            timeouts.append(timeout)
            release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch)
        begin = time.monotonic()
        controller.start([1, 2], deadline=0.18)
        time.sleep(0.11)
        controller.submit(1, 100)
        self.assertTrue(entered.wait(0.05))
        events = wait_events(controller, 2)
        elapsed = time.monotonic() - begin
        self.assertGreaterEqual(elapsed, 0.16)
        self.assertLess(elapsed, 0.3)
        self.assertTrue(all(e["status"] == "timeout" for e in events))
        self.assertTrue(0 < timeouts[0] < 0.09)
        release.set()
        time.sleep(0.025)
        self.assertEqual(controller.poll_events(), [])
        self.assertFalse(list(self.path.glob("*.json")))

    def test_new_round_discards_old_events_and_late_old_cache_write(self):
        release = threading.Event()
        entered = threading.Event()

        def fetch(match_id, timeout):
            if match_id == 100:
                entered.set()
                release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch)
        old_scan = controller.start([1])
        controller.submit(1, 100)
        self.assertTrue(entered.wait(0.5))
        new_scan = controller.start([2])
        controller.submit(2, 101)
        events = wait_events(controller, 1)
        release.set()
        time.sleep(0.025)
        self.assertNotEqual(old_scan, new_scan)
        self.assertTrue(all(e["scan_id"] == new_scan for e in events))
        self.assertEqual(controller.poll_events(), [])
        self.assertFalse((self.path / "100.json").exists())
        self.assertTrue((self.path / "101.json").exists())

    def test_changed_assignment_does_not_receive_previous_match_response(self):
        release = threading.Event()
        entered = threading.Event()

        def fetch(match_id, timeout):
            if match_id == 100:
                entered.set()
                release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch)
        controller.start([1])
        controller.submit(1, 100)
        self.assertTrue(entered.wait(0.5))
        controller.submit(1, 101)
        event = wait_events(controller, 1)[0]
        release.set()
        time.sleep(0.025)
        self.assertEqual(event["match_id"], 101)
        self.assertEqual(controller.poll_events(), [])

    def test_429_cooldown_blocks_new_requests_but_cache_still_works(self):
        self.write_cache(102)
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            raise HTTPError("https://dash.batrace.top/match/100", 429, "Too Many Requests", {}, None)

        controller = self.controller(fetch, concurrency=1)
        controller.start([1, 2, 3])
        controller.submit(1, 100)
        controller.submit(2, 101)
        controller.submit(3, 102)
        events = {e["player_id"]: e for e in wait_events(controller, 3)}
        self.assertEqual(calls, [100])
        self.assertIn("限流", events[2]["message"])
        self.assertEqual(events[3]["status"], "ok")
        self.assertTrue(events[3]["from_cache"])
        self.assertGreater(controller._cooldown_until - time.monotonic(), 59)
        controller.start([1, 2])
        controller.submit(1, 103)
        controller.submit(2, 102)
        second = {e["player_id"]: e for e in wait_events(controller, 2)}
        self.assertEqual(calls, [100])
        self.assertEqual(second[1]["status"], "error")
        self.assertTrue(second[2]["from_cache"])

    def test_unpolled_old_assignment_event_is_discarded(self):
        controller = self.controller(lambda match_id, timeout: match_data(match_id))
        controller.start([1])
        controller.submit(1, 100)
        end = time.monotonic() + 0.5
        while 1 not in controller._active.terminal and time.monotonic() < end:
            time.sleep(0.003)
        self.assertIn(1, controller._active.terminal)
        controller.submit(1, 101)
        events = wait_events(controller, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["match_id"], 101)

    def test_fetch_error_and_wrong_match_are_not_cached(self):
        def fetch(match_id, timeout):
            if match_id == 100:
                raise OSError("offline")
            return match_data(999)

        controller = self.controller(fetch)
        controller.start([1, 2])
        controller.submit(1, 100)
        controller.submit(2, 101)
        events = wait_events(controller, 2)
        self.assertTrue(all(e["status"] == "error" for e in events))
        self.assertFalse(list(self.path.glob("*.json")))

    def test_fixed_worker_pool_caps_concurrency_across_rounds(self):
        release = threading.Event()
        entered = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def fetch(match_id, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    entered.set()
            release.wait(1)
            with lock:
                active -= 1
            return match_data(match_id)

        controller = self.controller(fetch)
        controller.start([1, 2])
        controller.submit(1, 100)
        controller.submit(2, 101)
        self.assertTrue(entered.wait(0.5))
        controller.start([1, 2], deadline=0.08)
        controller.submit(1, 102)
        controller.submit(2, 103)
        self.assertTrue(all(e["status"] == "timeout" for e in wait_events(controller, 2)))
        self.assertEqual(peak, 2)
        release.set()
        time.sleep(0.025)

    def test_cancel_and_close_are_prompt_and_drop_events(self):
        release = threading.Event()
        controller = self.controller(lambda match_id, timeout: (release.wait(1), match_data(match_id))[1])
        controller.start([1])
        controller.submit(1, 100)
        controller.cancel()
        self.assertEqual(controller.poll_events(), [])
        begin = time.monotonic()
        controller.close()
        self.assertLess(time.monotonic() - begin, 0.05)
        self.assertTrue(all(t.daemon for t in controller._workers))
        with self.assertRaises(RuntimeError):
            controller.start([1])
        release.set()

    def test_invalid_match_gives_error_and_unexpected_player_is_ignored(self):
        calls = []
        controller = self.controller(lambda match_id, timeout: calls.append(match_id))
        controller.start([1])
        controller.submit(99, 100)
        controller.submit(1, None)
        event = wait_events(controller, 1)[0]
        self.assertEqual(event["player_id"], 1)
        self.assertEqual(event["status"], "error")
        self.assertEqual(calls, [])

    def test_only_f_drive_absolute_cache_paths_and_at_most_two_workers(self):
        for path in [r"C:\must-not-create-recon", "relative-recon"]:
            with self.assertRaises(ValueError):
                RecentRecon(path, lambda *a, **kw: {})
        with self.assertRaises(ValueError):
            RecentRecon(str(self.path), lambda *a, **kw: {}, concurrency=3)


if __name__ == "__main__":
    unittest.main()
