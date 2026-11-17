"""Offline behavioural tests.  All temporary data stays on F:."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

from ba_scout.engine import ScanController


TEMP_ROOT = Path(__file__).resolve().parents[1] / 'work' / 'tests-temp'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def roster(fight_id="100", ids=range(1, 11)):
    return SimpleNamespace(
        fight_id=fight_id,
        players=[
            {"id": p, "name": f"玩家{p}", "team": "Alpha" if p <= 5 else "Bravo"}
            for p in ids
        ],
        log_path="F:/fixture.log",
        detected_at="2026-10-25T00:00:00+00:00",
    )


def player_data(player_id, **extra):
    return {
        "player_id": player_id,
        "name": f"玩家{player_id}",
        "rating": 2000,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def wait_finished(controller, timeout=2.0):
    events = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        events.extend(controller.poll_events())
        if any(e["type"] == "finished" for e in events):
            return events
        time.sleep(0.005)
    raise AssertionError(f"No finished event: {events}")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="engine-", dir=TEMP_ROOT)
        self.path = Path(self.directory.name)
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        # Tests explicitly release any fake request before deleting its files.
        self.directory.cleanup()

    def controller(self, fetcher, **kwargs):
        controller = ScanController(str(self.path), fetcher, **kwargs)
        self.controllers.append(controller)
        return controller

    def write_cache(self, player_id, age=0, **extra):
        timestamp = datetime.now(timezone.utc) - timedelta(seconds=age)
        data = player_data(player_id, **extra)
        data["fetched_at"] = timestamp.isoformat()
        (self.path / f"{player_id}.json").write_text(
            json.dumps(
                {"schema": 1, "player_id": player_id, "stored_at": timestamp.isoformat(), "data": data}
            ),
            encoding="utf-8",
        )

    def test_enemy_priority_and_bounded_workers(self):
        entered = []
        active = 0
        peak = 0
        lock = threading.Lock()

        def fetch(player_id, timeout):
            nonlocal active, peak
            with lock:
                entered.append(player_id)
                active += 1
                peak = max(peak, active)
            time.sleep(0.025)
            with lock:
                active -= 1
            return player_data(player_id)

        controller = self.controller(fetch, concurrency=3)
        begin = time.monotonic()
        scan_id = controller.start(roster(), 1)
        self.assertLess(time.monotonic() - begin, 0.08)
        events = wait_finished(controller)
        # Worker invocation order within a concurrently dispatched group is
        # controlled by the OS scheduler; the queued priority is deterministic.
        self.assertEqual([p["id"] for p in events[0]["players"]], [6, 7, 8, 9, 10, 2, 3, 4, 5, 1])
        self.assertTrue(set(entered[:3]).issubset({6, 7, 8, 9, 10}))
        self.assertEqual(set(entered), set(range(1, 11)))
        self.assertLessEqual(peak, 3)
        self.assertTrue(all(e["scan_id"] == scan_id for e in events))
        self.assertEqual(events[0]["self_team"], "Alpha")
        self.assertEqual(events[-1]["complete"], 10)
        self.assertFalse(events[-1]["timed_out"])
        self.assertEqual(len(list(self.path.glob("*.json"))), 10)
        self.assertFalse(list(self.path.glob("*.tmp")))

    def test_fresh_cache_avoids_network(self):
        self.write_cache(6)
        calls = []

        def fetch(player_id, timeout):
            calls.append(player_id)
            return player_data(player_id)

        controller = self.controller(fetch)
        controller.start(roster(ids=[6]), 1)
        events = wait_finished(controller)
        result = next(e for e in events if e["type"] == "player")
        self.assertEqual(result["status"], "cached")
        self.assertTrue(result["data"]["from_cache"])
        self.assertEqual(calls, [])
        self.assertIsNone(events[0]["self_team"])

    def test_old_cache_visible_on_refresh_error(self):
        self.write_cache(6, age=900, rating=1800)

        def fetch(player_id, timeout):
            raise OSError("offline")

        controller = self.controller(fetch)
        controller.start(roster(ids=[6]), 1)
        events = wait_finished(controller)
        rows = [e for e in events if e["type"] == "player"]
        self.assertEqual([e["status"] for e in rows], ["stale", "error"])
        self.assertEqual(rows[-1]["data"]["rating"], 1800)
        self.assertTrue(rows[-1]["data"]["from_cache"])
        self.assertEqual(events[-1]["complete"], 0)
        self.assertFalse(events[-1]["timed_out"])

    def test_force_refreshes_fresh_cache(self):
        self.write_cache(6, rating=1800)
        controller = self.controller(lambda player_id, timeout: player_data(player_id, rating=2100))
        controller.start(roster(ids=[6]), 1, force=True)
        events = wait_finished(controller)
        rows = [e for e in events if e["type"] == "player"]
        self.assertEqual([e["status"] for e in rows], ["stale", "ok"])
        self.assertEqual(rows[-1]["data"]["rating"], 2100)

    def test_min_profile_version_preserves_old_cache_while_refreshing(self):
        self.write_cache(6, rating=1800)
        calls = []

        def fetch(player_id, timeout):
            calls.append(player_id)
            return player_data(player_id, rating=2100, profile_version=2)

        controller = self.controller(fetch, min_profile_version=2)
        controller.start(roster(ids=[6]), 1)
        events = wait_finished(controller)
        rows = [e for e in events if e["type"] == "player"]
        self.assertEqual([e["status"] for e in rows], ["stale", "ok"])
        self.assertEqual(rows[0]["data"]["rating"], 1800)
        self.assertTrue(rows[0]["data"]["from_cache"])
        self.assertEqual(rows[1]["data"]["profile_version"], 2)
        self.assertEqual(calls, [6])
        controller.start(roster("next", [6]), 1)
        second = wait_finished(controller)
        self.assertEqual(next(e for e in second if e["type"] == "player")["status"], "cached")
        self.assertEqual(calls, [6])

    def test_hard_deadline_does_not_wait_for_slow_fetch(self):
        release = threading.Event()
        supplied_timeout = []

        def fetch(player_id, timeout):
            supplied_timeout.append(timeout)
            release.wait(2)
            return player_data(player_id)

        controller = self.controller(fetch, deadline=0.18, concurrency=2)
        begin = time.monotonic()
        controller.start(roster(), 1)
        events = wait_finished(controller)
        elapsed = time.monotonic() - begin
        self.assertGreaterEqual(elapsed, 0.16)
        self.assertLess(elapsed, 0.5)
        rows = [e for e in events if e["type"] == "player"]
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(e["status"] == "timeout" for e in rows))
        self.assertTrue(events[-1]["timed_out"])
        self.assertEqual(events[-1]["complete"], 0)
        self.assertTrue(all(0 < value <= 0.18 for value in supplied_timeout))
        release.set()
        time.sleep(0.03)
        self.assertEqual(controller.poll_events(), [])
        self.assertEqual(list(self.path.glob("*.json")), [])

    def test_late_old_round_cannot_pollute_new_round_or_cache(self):
        started = threading.Event()
        release = threading.Event()

        def fetch(player_id, timeout):
            if player_id == 6:
                started.set()
                release.wait(2)
            return player_data(player_id)

        controller = self.controller(fetch, concurrency=2)
        old_id = controller.start(roster("old", [6]), 1)
        self.assertTrue(started.wait(0.5))
        new_id = controller.start(roster("new", [7]), 1)
        events = wait_finished(controller)
        release.set()
        time.sleep(0.03)
        events.extend(controller.poll_events())
        self.assertNotEqual(new_id, old_id)
        self.assertTrue(all(e["scan_id"] == new_id for e in events))
        self.assertTrue((self.path / "7.json").exists())
        self.assertFalse((self.path / "6.json").exists())

    def test_concurrency_stays_bounded_after_repeated_starts(self):
        release = threading.Event()
        entered = threading.Event()
        active = 0
        peak = 0
        lock = threading.Lock()

        def fetch(player_id, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    entered.set()
            release.wait(2)
            with lock:
                active -= 1
            return player_data(player_id)

        controller = self.controller(fetch, concurrency=2, deadline=0.25)
        controller.start(roster("first"), 1)
        self.assertTrue(entered.wait(0.5))
        for index in range(5):
            controller.start(roster(f"new-{index}"), 1)
        events = wait_finished(controller)
        self.assertEqual(peak, 2)
        self.assertTrue(events[-1]["timed_out"])
        release.set()
        time.sleep(0.03)

    def test_cancel_drops_events_and_close_is_prompt(self):
        release = threading.Event()
        controller = self.controller(
            lambda player_id, timeout: (release.wait(2), player_data(player_id))[1]
        )
        controller.start(roster(), 1)
        controller.cancel()
        self.assertEqual(controller.poll_events(), [])
        begin = time.monotonic()
        controller.close()
        self.assertLess(time.monotonic() - begin, 0.05)
        self.assertTrue(all(t.daemon for t in controller._workers))
        with self.assertRaises(RuntimeError):
            controller.start(roster(), 1)
        release.set()

    def test_corrupt_or_wrong_identity_cache_is_ignored(self):
        (self.path / "6.json").write_text("{broken", encoding="utf-8")
        self.write_cache(7)
        wrapper = json.loads((self.path / "7.json").read_text(encoding="utf-8"))
        wrapper["data"]["player_id"] = 99
        (self.path / "7.json").write_text(json.dumps(wrapper), encoding="utf-8")
        calls = []

        def fetch(player_id, timeout):
            calls.append(player_id)
            return player_data(player_id)

        controller = self.controller(fetch)
        controller.start(roster(ids=[6, 7]), 1)
        events = wait_finished(controller)
        self.assertEqual(set(calls), {6, 7})
        self.assertEqual(events[-1]["complete"], 2)

    def test_wrong_provider_identity_becomes_error_and_is_not_cached(self):
        controller = self.controller(lambda player_id, timeout: player_data(999))
        controller.start(roster(ids=[6]), 1)
        events = wait_finished(controller)
        row = next(e for e in events if e["type"] == "player")
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["data"])
        self.assertFalse(list(self.path.glob("*.json")))

    def test_429_stops_queued_requests_and_later_round_keeps_cache(self):
        self.write_cache(7, age=900, rating=1800)
        self.write_cache(8, rating=2200)
        calls = []

        def fetch(player_id, timeout):
            calls.append(player_id)
            raise HTTPError("https://dash.batrace.top/player/6", 429, "Too Many Requests", {}, None)

        controller = self.controller(fetch, concurrency=1)
        controller.start(roster(), 1)
        first = wait_finished(controller)
        self.assertEqual(calls, [6])
        self.assertGreater(controller._cooldown_until - time.monotonic(), 59)
        first_rows = {e["player_id"]: e for e in first if e["type"] == "player"}
        self.assertEqual(first_rows[8]["status"], "cached")
        self.assertEqual(first_rows[7]["status"], "error")
        self.assertTrue(first_rows[7]["data"]["from_cache"])
        self.assertEqual(first_rows[7]["data"]["rating"], 1800)
        self.assertIn("限流", first_rows[10]["message"])
        controller.start(roster("next", [6, 7, 8]), 1)
        second = wait_finished(controller)
        self.assertEqual(calls, [6])
        rows = {e["player_id"]: e for e in second if e["type"] == "player"}
        self.assertEqual(rows[8]["status"], "cached")
        self.assertEqual(rows[8]["data"]["rating"], 2200)
        self.assertIn("限流", rows[6]["message"])
        self.assertTrue(rows[7]["data"]["from_cache"])
        self.assertEqual(second[-1]["complete"], 1)

    def test_inflight_request_can_finish_after_another_worker_gets_429(self):
        both_started = threading.Barrier(2)
        allow_success = threading.Event()
        calls = []

        def fetch(player_id, timeout):
            calls.append(player_id)
            both_started.wait(timeout=1)
            if player_id == 6:
                raise HTTPError("https://dash.batrace.top/player/6", 429, "Too Many Requests", {}, None)
            allow_success.wait(1)
            return player_data(player_id)

        controller = self.controller(fetch, concurrency=2)
        controller.start(roster(ids=[6, 7, 8]), 1)
        end = time.monotonic() + 0.5
        while controller._cooldown_until == 0 and time.monotonic() < end:
            time.sleep(0.005)
        allow_success.set()
        events = wait_finished(controller)
        rows = {e["player_id"]: e for e in events if e["type"] == "player"}
        self.assertEqual(set(calls), {6, 7})
        self.assertEqual(rows[7]["status"], "ok")
        self.assertEqual(rows[8]["status"], "error")
        self.assertIn("限流", rows[8]["message"])

    def test_c_drive_and_relative_cache_paths_rejected_before_write(self):
        for path in [r"C:\must-not-create-ba-scout", "relative-cache"]:
            with self.assertRaises(ValueError):
                ScanController(path, lambda *a, **kw: {})


if __name__ == "__main__":
    unittest.main()
