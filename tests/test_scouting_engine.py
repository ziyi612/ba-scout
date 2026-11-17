"""Offline integration checks for the shared match/opening request pipeline."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from ba_scout.scouting_engine import BattleRecon


TEMP_ROOT = Path(__file__).resolve().parents[1] / 'work' / 'tests-temp'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def match_data(match_id, players=(1, 2, 3), offsets=(10.0,), duration=600.0):
    return {
        "recon_version": 2,
        "match_id": str(match_id),
        "players": {
            str(player): {
                "available": True,
                "player_id": player,
                "total_spawns": len(offsets),
                "all_units": [{"id": 10, "name": "测试步兵", "type": "Infantry", "count": len(offsets)}],
                "deployment_timing": {
                    "available": True, "complete": True,
                    "start_time": 1000.0, "end_time": 1000.0 + duration,
                    "missing_spawn_times": 0, "before_start": 0, "after_end": 0, "missing_unit_ids": 0,
                    "spawns": [{"id": 10, "name": "测试步兵", "type": "Infantry", "offset_s": offset} for offset in offsets],
                },
            }
            for player in players
        },
        "source_url": f"https://dash.batrace.top/match/{match_id}",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "match_start_time": 1000.0,
        "match_end_time": 1000.0 + duration,
    }


def wait_until(controller, predicate, timeout=1.0):
    events = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        events.extend(controller.poll_events())
        if predicate(events):
            return events
        time.sleep(0.003)
    raise AssertionError(f"Event condition was not reached: {events}")


def finished_events(controller, players=1, timeout=1.0):
    return wait_until(controller, lambda events: len([e for e in events if e["type"] == "opening" and e["finished"]]) >= players, timeout)


def final_openings(events):
    return {e["player_id"]: e for e in events if e["type"] == "opening" and e["finished"]}


class BattleReconTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="battle-recon-", dir=TEMP_ROOT)
        self.path = Path(self.directory.name)
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        self.directory.cleanup()

    def controller(self, fetcher, **kwargs):
        controller = BattleRecon(str(self.path), fetcher, **kwargs)
        self.controllers.append(controller)
        return controller

    def write_cache(self, match_id, age=0, version=2, schema=2, players=(1, 2, 3)):
        data = match_data(match_id, players)
        data["recon_version"] = version
        wrapper = {"schema": schema, "match_id": match_id, "stored_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(), "data": data}
        (self.path / f"{match_id}.json").write_text(json.dumps(wrapper), encoding="utf-8")

    def test_shared_history_page_fetched_once_for_both_views_and_later_player(self):
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            return match_data(match_id)

        controller = self.controller(fetch)
        scan_id = controller.start([1, 2, 3])
        controller.submit_history(1, [100, 101, 100, 102])
        controller.submit_history(2, [100, 102])
        events = finished_events(controller, players=2)
        controller.submit_history(3, [100, 101])
        events.extend(finished_events(controller))
        self.assertCountEqual(calls, [100, 101, 102])
        finals = final_openings(events)
        self.assertEqual([finals[p]["data"]["sample_count"] for p in [1, 2, 3]], [3, 2, 2])
        self.assertTrue(all(e["scan_id"] == scan_id for e in events))
        recon = [e for e in events if e["type"] == "recon"]
        self.assertEqual(len(recon), 3)
        self.assertTrue(all(e["match_id"] == 100 and e["status"] == "ok" for e in recon))
        self.assertTrue(all(e["source_url"].endswith("/100") and e["fetched_at"] and e["match_end_time"] for e in recon))
        self.assertEqual(len(list(self.path.glob("*.json"))), 3)
        self.assertFalse(list(self.path.glob("*.tmp")))

    def test_history_is_valid_unique_and_capped_at_five(self):
        calls = []
        controller = self.controller(lambda match_id, timeout: (calls.append(match_id), match_data(match_id))[1])
        controller.start([1])
        controller.submit_history(1, [None, False, -1, "oops", "100", 100, 101, 102, 103, 104, 105])
        final = final_openings(finished_events(controller))[1]
        self.assertCountEqual(calls, [100, 101, 102, 103, 104])
        self.assertEqual(final["data"]["requested_count"], 5)
        self.assertEqual(final["data"]["sample_count"], 5)
        controller.submit_history(1, [100, 101, 102, 103, 104])
        self.assertEqual(controller.poll_events(), [])

    def test_twenty_and_default_windows_share_one_fetch_and_exclude_exact_boundaries(self):
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            return match_data(match_id, offsets=(0.0, 19.0, 20.0, 119.0, 120.0))

        controller = self.controller(fetch)
        controller.start([1, 2])
        controller.submit(1, 100)
        controller.submit(2, 100)
        events = finished_events(controller, players=2)
        self.assertEqual(calls, [100])
        for final in final_openings(events).values():
            self.assertEqual(set(final["windows"]), {"20", "120"})
            early, regular = final["windows"]["20"], final["windows"]["120"]
            self.assertEqual(early["data"]["units"][0]["total_spawns"], 2)
            self.assertEqual(regular["data"]["units"][0]["total_spawns"], 4)
            self.assertEqual(early["data"]["sample_count"], 1)
            for key in ("status", "data", "finished", "message"):
                self.assertEqual(final[key], regular[key])
        pending = [event for event in events if event["type"] == "opening" and not event["finished"]]
        self.assertTrue(pending)
        self.assertTrue(all(set(event["windows"]) == {"20", "120"} for event in pending))

    def test_short_match_denominators_are_independent_for_each_window(self):
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            return match_data(match_id, duration=30.0 if match_id == 100 else 600.0)

        controller = self.controller(fetch)
        controller.start([1])
        controller.submit_history(1, [100, 101])
        final = final_openings(finished_events(controller))[1]
        early, regular = final["windows"]["20"], final["windows"]["120"]
        self.assertCountEqual(calls, [100, 101])
        self.assertEqual((early["data"]["sample_count"], regular["data"]["sample_count"]), (2, 1))
        self.assertEqual((early["status"], regular["status"]), ("ok", "partial"))
        self.assertEqual(early["data"]["units"][0]["matches_with_unit"], 2)
        self.assertEqual(regular["data"]["units"][0]["matches_with_unit"], 1)
        self.assertEqual(early["data"]["requested_count"], regular["data"]["requested_count"])
        self.assertIn("未覆盖完整开局观察窗口", str(regular["data"]["unusable_matches"]))

    def test_zero_early_deployments_is_valid_and_configured_twenty_is_deduplicated(self):
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            return match_data(match_id, offsets=(30.0,))

        controller = self.controller(fetch)
        controller.start([1])
        controller.submit(1, 100)
        first = final_openings(finished_events(controller))[1]
        self.assertEqual(first["windows"]["20"]["data"]["sample_count"], 1)
        self.assertEqual(first["windows"]["20"]["data"]["units"], [])
        self.assertEqual(len(first["windows"]["120"]["data"]["units"]), 1)
        controller.start([1], window_seconds=20)
        controller.submit(1, 100)
        second = final_openings(finished_events(controller))[1]
        self.assertEqual(set(second["windows"]), {"20"})
        self.assertEqual(second["data"]["window_seconds"], 20)
        self.assertEqual(calls, [100])  # Same timeline cache serves both rounds.
        controller.start([1], window_seconds=60)
        controller.submit(1, 100)
        third = final_openings(finished_events(controller))[1]
        self.assertEqual(set(third["windows"]), {"20", "60"})
        self.assertEqual(third["data"]["window_seconds"], 60)
        self.assertEqual(calls, [100])

    def test_progress_and_failed_match_never_enters_denominator(self):
        entered = threading.Event()
        release = threading.Event()

        def fetch(match_id, timeout):
            if match_id == 101:
                entered.set()
                release.wait(1)
                raise OSError("offline fixture")
            return match_data(match_id, offsets=(10.0, 20.0))

        controller = self.controller(fetch, concurrency=1)
        controller.start([1])
        controller.submit_history(1, [100, 101])
        self.assertTrue(entered.wait(0.5))
        events = controller.poll_events()
        progress = [e for e in events if e["type"] == "opening" and e["data"]["sample_count"] == 1]
        self.assertEqual(len(progress), 1)
        self.assertFalse(progress[0]["finished"])
        self.assertEqual(progress[0]["status"], "partial")
        release.set()
        final = final_openings(finished_events(controller))[1]
        self.assertTrue(final["finished"])
        self.assertEqual(final["status"], "partial")
        self.assertEqual(final["data"]["sample_count"], 1)
        self.assertEqual(final["data"]["requested_count"], 2)
        unit = final["data"]["units"][0]
        self.assertEqual(unit["matches_with_unit"], 1)
        self.assertEqual(unit["total_spawns"], 2)
        self.assertEqual(unit["mean_count"], 2)
        self.assertIn("offline fixture", str(final["data"]["unusable_matches"]))
        self.assertFalse((self.path / "101.json").exists())

    def test_other_players_latest_match_precedes_older_history_and_upgrades_shared_job(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            if match_id == 100:
                entered.set()
                release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch, concurrency=1)
        controller.start([1, 2])
        controller.submit_history(1, [100, 101, 102])
        self.assertTrue(entered.wait(0.5))
        controller.submit_history(2, [102, 200])
        release.set()
        finished_events(controller, players=2)
        self.assertEqual(calls, [100, 102, 101, 200])

    def test_missing_player_and_incomplete_timing_are_not_successful_opening_samples(self):
        raw = match_data(100, [1, 2])
        raw["players"]["1"]["deployment_timing"]["complete"] = False
        raw["players"]["1"]["deployment_timing"]["missing_spawn_times"] = 1
        raw["players"]["2"] = {"available": False, "reason": "没有单位明细"}
        controller = self.controller(lambda match_id, timeout: raw)
        controller.start([1, 2, 3])
        for player in [1, 2, 3]:
            controller.submit(player, 100)
        events = finished_events(controller, players=3)
        finals = final_openings(events)
        self.assertTrue(all(e["status"] == "error" and e["data"]["sample_count"] == 0 for e in finals.values()))
        recon = {e["player_id"]: e for e in events if e["type"] == "recon"}
        self.assertEqual(recon[1]["status"], "ok")
        self.assertEqual(recon[2]["status"], "error")
        self.assertEqual(recon[3]["status"], "error")

    def test_zero_units_in_window_is_a_real_sample(self):
        controller = self.controller(lambda match_id, timeout: match_data(match_id, offsets=(200.0,)))
        controller.start([1], window_seconds=120)
        controller.submit(1, 100)
        final = final_openings(finished_events(controller))[1]
        self.assertEqual(final["status"], "ok")
        self.assertEqual(final["data"]["sample_count"], 1)
        self.assertEqual(final["data"]["units"], [])

    def test_unavailable_player_with_residual_timing_still_cannot_enter_denominator(self):
        raw = match_data(100, [1])
        raw["players"]["1"]["available"] = False
        controller = self.controller(lambda match_id, timeout: raw)
        controller.start([1])
        controller.submit(1, 100)
        events = finished_events(controller)
        self.assertEqual(final_openings(events)[1]["data"]["sample_count"], 0)
        self.assertEqual([e for e in events if e["type"] == "recon"][0]["status"], "error")
        self.assertFalse(list(self.path.glob("*.json")))

    def test_version_two_day_cache_reuses_and_old_expired_or_broken_cache_refreshes(self):
        self.write_cache(100, age=86300)
        self.write_cache(101, age=86500)
        self.write_cache(102, version=1)
        self.write_cache(103, schema=1)
        (self.path / "104.json").write_text("broken", encoding="utf-8")
        calls = []
        controller = self.controller(lambda match_id, timeout: (calls.append(match_id), match_data(match_id))[1])
        controller.start([1])
        controller.submit_history(1, [100, 101, 102, 103, 104])
        events = finished_events(controller)
        self.assertCountEqual(calls, [101, 102, 103, 104])
        recon = [e for e in events if e["type"] == "recon"][0]
        self.assertTrue(recon["from_cache"])
        self.assertEqual(final_openings(events)[1]["data"]["sample_count"], 5)

    def test_old_fetch_result_can_show_latest_but_is_not_an_opening_or_cache_hit(self):
        raw = match_data(100)
        raw["recon_version"] = 1
        controller = self.controller(lambda match_id, timeout: raw)
        controller.start([1])
        controller.submit(1, 100)
        events = finished_events(controller)
        self.assertEqual([e for e in events if e["type"] == "recon"][0]["status"], "ok")
        self.assertEqual(final_openings(events)[1]["data"]["sample_count"], 0)
        self.assertFalse(list(self.path.glob("*.json")))

    def test_deadline_is_from_start_and_late_response_cannot_write_or_publish(self):
        entered = threading.Event()
        release = threading.Event()
        timeouts = []

        def fetch(match_id, timeout):
            entered.set()
            timeouts.append(timeout)
            release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch)
        began = time.monotonic()
        controller.start([1, 2], deadline=0.18)
        time.sleep(0.11)
        controller.submit_history(1, [100, 101])
        self.assertTrue(entered.wait(0.05))
        events = finished_events(controller, players=2)
        self.assertLess(time.monotonic() - began, 0.3)
        self.assertTrue(all(e["status"] == "timeout" for e in final_openings(events).values()))
        self.assertTrue(all(0 < seconds < 0.09 for seconds in timeouts))
        release.set()
        time.sleep(0.025)
        self.assertEqual(controller.poll_events(), [])
        self.assertFalse(list(self.path.glob("*.json")))

    def test_deadline_preserves_successful_partial_samples(self):
        release = threading.Event()

        def fetch(match_id, timeout):
            if match_id == 101:
                release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch, concurrency=1)
        controller.start([1], deadline=0.10)
        controller.submit_history(1, [100, 101])
        final = final_openings(finished_events(controller))[1]
        self.assertEqual(final["status"], "partial")
        self.assertEqual(final["data"]["sample_count"], 1)
        self.assertIn("截止", final["message"])
        release.set()
        time.sleep(0.025)
        self.assertFalse((self.path / "101.json").exists())

    def test_deadline_finishes_both_windows_without_replacing_their_denominators(self):
        release = threading.Event()

        def fetch(match_id, timeout):
            if match_id == 101:
                release.wait(1)
            return match_data(match_id, duration=30.0 if match_id == 100 else 600.0)

        controller = self.controller(fetch, concurrency=1)
        controller.start([1], deadline=0.10)
        controller.submit_history(1, [100, 101])
        final = final_openings(finished_events(controller))[1]
        early, regular = final["windows"]["20"], final["windows"]["120"]
        self.assertEqual((early["status"], regular["status"]), ("partial", "timeout"))
        self.assertEqual((early["data"]["sample_count"], regular["data"]["sample_count"]), (1, 0))
        self.assertTrue(early["finished"] and regular["finished"])
        self.assertIn("截止", early["message"])
        self.assertIn("截止", regular["message"])
        release.set()
        time.sleep(0.025)
        self.assertEqual(controller.poll_events(), [])
        self.assertFalse((self.path / "101.json").exists())

    def test_new_round_discards_late_old_result_and_keeps_fixed_worker_limit(self):
        release = threading.Event()
        entered = threading.Event()
        active = 0
        peak = 0
        lock = threading.Lock()

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
        first = controller.start([1])
        controller.submit_history(1, [100, 101])
        self.assertTrue(entered.wait(0.5))
        second = controller.start([2], deadline=0.08)
        controller.submit_history(2, [102, 103])
        events = finished_events(controller)
        self.assertNotEqual(first, second)
        self.assertTrue(all(e["scan_id"] == second for e in events))
        self.assertEqual(final_openings(events)[2]["status"], "timeout")
        self.assertTrue(all(window["status"] == "timeout" and window["finished"] for window in final_openings(events)[2]["windows"].values()))
        self.assertEqual(peak, 2)
        release.set()
        time.sleep(0.025)
        self.assertFalse(list(self.path.glob("*.json")))
        self.assertEqual(controller.poll_events(), [])

    def test_history_replacement_filters_unpolled_events_and_late_previous_history(self):
        entered = threading.Event()
        release = threading.Event()

        def fetch(match_id, timeout):
            if match_id == 101:
                entered.set()
                release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch)
        controller.start([1])
        controller.submit_history(1, [100, 101])
        self.assertTrue(entered.wait(0.5))
        controller.submit_history(1, [102])
        events = finished_events(controller)
        recon = [e for e in events if e["type"] == "recon"]
        self.assertEqual([e["match_id"] for e in recon], [102])
        self.assertEqual(final_openings(events)[1]["data"]["requested_count"], 1)
        for window in final_openings(events)[1]["windows"].values():
            self.assertEqual([m["match_id"] for m in window["data"]["matches"]], ["102"])
        release.set()
        time.sleep(0.025)
        self.assertEqual(controller.poll_events(), [])
        self.assertFalse((self.path / "101.json").exists())

    def test_429_stops_queued_requests_and_next_round_but_fresh_cache_still_works(self):
        self.write_cache(102)
        calls = []

        def fetch(match_id, timeout):
            calls.append(match_id)
            raise HTTPError("https://dash.batrace.top/match/100", 429, "Too Many Requests", {}, None)

        controller = self.controller(fetch, concurrency=1)
        controller.start([1])
        controller.submit_history(1, [100, 101, 102])
        final = final_openings(finished_events(controller))[1]
        self.assertEqual(calls, [100])
        self.assertEqual(final["data"]["sample_count"], 1)
        self.assertIn("限流", str(final["data"]["unusable_matches"]))
        self.assertGreater(controller._cooldown_until - time.monotonic(), 59)
        controller.start([1])
        controller.submit_history(1, [102, 103])
        events = finished_events(controller)
        self.assertEqual(calls, [100])
        self.assertEqual(final_openings(events)[1]["data"]["sample_count"], 1)
        self.assertTrue([e for e in events if e["type"] == "recon"][0]["from_cache"])

    def test_cancel_and_close_are_prompt_and_workers_are_daemons(self):
        release = threading.Event()
        entered = threading.Event()

        def fetch(match_id, timeout):
            entered.set()
            release.wait(1)
            return match_data(match_id)

        controller = self.controller(fetch)
        controller.start([1])
        controller.submit(1, 100)
        self.assertTrue(entered.wait(0.5))
        began = time.monotonic()
        controller.cancel()
        controller.close()
        self.assertLess(time.monotonic() - began, 0.05)
        self.assertEqual(controller.poll_events(), [])
        self.assertTrue(all(thread.daemon for thread in controller._workers))
        with self.assertRaises(RuntimeError):
            controller.start([1])
        release.set()

    def test_invalid_history_and_unknown_player_do_not_fetch(self):
        calls = []
        controller = self.controller(lambda match_id, timeout: calls.append(match_id))
        controller.start([1])
        controller.submit_history(99, [100])
        controller.submit_history(1, [False, None, "bad"])
        events = finished_events(controller)
        self.assertEqual(final_openings(events)[1]["status"], "error")
        self.assertEqual(calls, [])

    def test_missing_only_requested_player_or_wrong_match_never_writes_cache(self):
        controller = self.controller(lambda match_id, timeout: match_data(100 if match_id == 100 else 999, players=[99]))
        controller.start([1])
        controller.submit_history(1, [100, 101])
        final = final_openings(finished_events(controller))[1]
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["data"]["sample_count"], 0)
        self.assertFalse(list(self.path.glob("*.json")))

    def test_only_f_paths_and_bounded_concurrency_history_and_deadline(self):
        for path in [r"C:\must-not-create-battle", "relative-battle"]:
            with self.assertRaises(ValueError):
                BattleRecon(path, lambda *args, **kwargs: {})
        for concurrency in [0, 3, True]:
            with self.assertRaises(ValueError):
                BattleRecon(str(self.path), lambda *args, **kwargs: {}, concurrency=concurrency)
        controller = self.controller(lambda *args, **kwargs: {})
        for kwargs in [{"deadline": 0}, {"deadline": float("nan")}, {"window_seconds": 0}, {"history_limit": 6}, {"history_limit": True}]:
            with self.assertRaises(ValueError):
                controller.start([1], **kwargs)


if __name__ == "__main__":
    unittest.main()
