"""Preparation names arrive in separate real-time stages, with bounded HTTP.

Fixtures control frames and network completion independently. They exercise
the production coordinator and fixed worker pool, not a synchronous model.
"""

from concurrent.futures import Future
from collections import Counter
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ba_scout.player_search import parse_search_payload
from ba_scout.prep_engine import PrepController
from ba_scout.request_gate import RequestGate


def row(index, names):
    if isinstance(names, str):
        names = [names]
    return {"team": "Bravo", "index": index, "queryable": bool(names), "valid": True,
            "variants": [{"text": name, "queryable": True} for name in names]}


class ChangingOCR:
    def __init__(self, names=None):
        self.lock = threading.Lock()
        self.calls = 0
        self.set(names or {})

    def set(self, names):
        with self.lock:
            self.rows = [row(index, names.get(index, [])) for index in range(5)]

    def submit_capture_game(self, **kwargs):
        with self.lock:
            self.calls += 1
            result = {"ok": True, "geometry_valid": True, "rows": self.rows,
                      "placeholder_counts": {"Bravo": sum(not r["queryable"] for r in self.rows)}}
        future = Future()
        future.set_result(result)
        return future


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.controllers = []
        self.releases = []
        self.events = []
        self.calls = []
        self.call_lock = threading.Lock()

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        for release in self.releases:
            release.set()

    def controller(self, ocr, search=None, **options):
        options.setdefault("lookup_budget", .8)
        options.setdefault("total_budget", 1.0)
        options.setdefault("capture_interval", .01)
        gate = options.pop("request_gate", RequestGate())
        controller = PrepController(ocr, gate, searcher=search or self.search,
                                    extractor=lambda frame, regions: frame, **options)
        self.controllers.append(controller)
        controller.start("LIVE-TEST", {})
        return controller

    def search(self, name, timeout):
        with self.call_lock:
            self.calls.append((name, time.monotonic(), timeout))
        return [{"id": int(name.removeprefix("name")) + 100, "name": name}]

    def wait(self, controller, predicate, timeout=1.5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.events.extend(controller.poll_events())
            result = next((event for event in reversed(self.events) if predicate(event)), None)
            if result is not None:
                return result
            time.sleep(.003)
        self.fail(f"Expected event missing: {self.events}")

    def snapshot(self, controller, ids=None, final=None, timeout=1.5):
        return self.wait(controller, lambda event: event["type"] == "resolved"
                         and (ids is None or {p["id"] for p in event["players"]} == set(ids))
                         and (final is None or event["final"] == final), timeout)

    def barrier(self):
        value = threading.Event()
        self.releases.append(value)
        return value

    def test_first_player_then_late_fourth_fifth_continue_after_initial_deadline(self):
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr, total_budget=.18, lookup_budget=.12)
        first = self.snapshot(controller, {100}, False)
        self.assertTrue(first["progressive"])
        ocr.set({i: f"name{i}" for i in range(3)})
        three = self.snapshot(controller, {100, 101, 102}, False)
        self.assertEqual(three["player_deadlines"][100], first["player_deadlines"][100])
        self.wait(controller, lambda e: e["type"] == "resolved" and e.get("initial_report_ready"), .5)
        ocr.set({i: f"name{i}" for i in range(5)})
        complete = self.snapshot(controller, set(range(100, 105)), True, 2)
        self.assertEqual(complete["initial_deadline"], first["initial_deadline"])
        self.assertEqual(complete["started"], first["started"])
        self.assertGreater(complete["player_deadlines"][104], first["initial_deadline"])
        self.assertEqual(Counter(name for name, _, _ in self.calls), Counter(f"name{i}" for i in range(5)))
        self.assertGreater(complete["revision"], three["revision"])

    def test_three_rows_then_late_rows_do_not_wait_for_other_searches(self):
        slow = self.barrier()
        entered = self.barrier()
        def search(name, timeout):
            if name == "name2":
                entered.set()
                slow.wait(.7)
            return self.search(name, timeout)
        ocr = ChangingOCR({i: f"name{i}" for i in range(3)})
        controller = self.controller(ocr, search)
        self.assertTrue(entered.wait(.3))
        self.snapshot(controller, {100, 101}, False)
        ocr.set({i: f"name{i}" for i in range(5)})
        before_slow = self.snapshot(controller, {100, 101, 103, 104}, False)
        self.assertFalse(slow.is_set())
        slow.set()
        full = self.snapshot(controller, set(range(100, 105)), True)
        self.assertGreater(full["revision"], before_slow["revision"])

    def test_eleven_variants_share_five_workers_and_wait_for_every_variant(self):
        release = self.barrier()
        entered_three = self.barrier()
        active, peak = 0, 0
        lock = threading.Lock()
        def search(name, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                self.calls.append(name)
                if active >= 3:
                    entered_three.set()
            release.wait(.6)
            with lock:
                active -= 1
            return [{"id": 123, "name": name}] if name == "variant0" else []
        ocr = ChangingOCR({0: [f"variant{i}" for i in range(11)]})
        controller = self.controller(ocr, search)
        self.assertTrue(entered_three.wait(.3), "Variants must use idle pool workers in parallel")
        self.events.extend(controller.poll_events())
        self.assertFalse(any(e["type"] == "resolved" and e["players"] for e in self.events))
        release.set()
        result = self.snapshot(controller, {123}, False)
        self.assertEqual(result["name_requests"], 11)
        self.assertEqual(len(set(self.calls)), 11)
        self.assertLessEqual(peak, 5)
        self.assertEqual(len(result["rows"][0]["resolution"]["queries"]), 11)

    def test_latest_match_sized_twenty_one_variants_can_complete_all_slots(self):
        sizes = [3, 3, 2, 2, 11]
        names = {i: [f"r{i}v{j}" for j in range(size)] for i, size in enumerate(sizes)}
        def search(name, timeout):
            with self.call_lock:
                self.calls.append(name)
            time.sleep(.015)
            return [{"id": 100 + int(name[1]), "name": name}] if name.endswith("v0") else []
        ocr = ChangingOCR({i: names[i] for i in (2, 3, 4)})
        controller = self.controller(ocr, search)
        self.snapshot(controller, {102, 103, 104}, False)
        ocr.set(names)
        result = self.snapshot(controller, set(range(100, 105)), True)
        self.assertEqual(result["name_requests"], 21)
        self.assertEqual(len(self.calls), len(set(self.calls)))

    def test_stable_changed_name_revokes_previous_identity_before_new_http_returns(self):
        blocked = self.barrier()
        entered = self.barrier()
        def search(name, timeout):
            if name == "name1":
                entered.set()
                blocked.wait(.7)
            return self.search(name, timeout)
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr, search)
        first = self.snapshot(controller, {100}, False)
        ocr.set({0: "name1"})
        self.assertTrue(entered.wait(.4))
        revoked = self.snapshot(controller, set(), False)
        self.assertGreater(revoked["revision"], first["revision"])
        blocked.set()
        self.snapshot(controller, {101}, False)

    def test_stable_variant_subset_keeps_resolved_identity_and_deadlines(self):
        def search(name, timeout):
            self.calls.append(name)
            return [{"id": 810004, "name": "示例玩家甲号"}] if name == "示例玩家甲号" else []
        initial = ["示 例 玩 家 甲 号", "示 例 玩 家 申 号", "示例玩家甲号", "示例玩家申号"]
        ocr = ChangingOCR({2: initial})
        controller = self.controller(ocr, search)
        first = self.snapshot(controller, {810004}, False)
        remaining = [name for name in initial if name != "示 例 玩 家 申 号"]
        ocr.set({2: remaining})
        updated = self.wait(controller, lambda e: e["type"] == "resolved" and e["revision"] > first["revision"])
        self.assertEqual({p["id"] for p in updated["players"]}, {810004})
        self.assertEqual(updated["player_deadlines"], first["player_deadlines"])
        self.assertEqual(updated["initial_deadline"], first["initial_deadline"])
        self.assertEqual(Counter(self.calls), Counter(initial))
        self.assertFalse(any(e["type"] == "resolved" and e["revision"] > first["revision"]
                             and not e["players"] for e in self.events))

    def test_added_variant_still_revokes_resolved_identity_before_lookup(self):
        blocked, entered = self.barrier(), self.barrier()
        def search(name, timeout):
            self.calls.append(name)
            if name == "extra":
                entered.set()
                blocked.wait(.7)
                return [{"id": 101, "name": name}]
            return [{"id": 100, "name": name}]
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr, search)
        first = self.snapshot(controller, {100}, False)
        ocr.set({0: ["name0", "extra"]})
        self.assertTrue(entered.wait(.4))
        revoked = self.snapshot(controller, set(), False)
        self.assertGreater(revoked["revision"], first["revision"])
        blocked.set()
        self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "ambiguous")

    def test_subset_without_exact_case_preserved_name_does_not_keep_identity(self):
        def search(name, timeout):
            self.calls.append(name)
            return [{"id": 100, "name": "Player"}] if name == "Player" else []
        ocr = ChangingOCR({0: ["Player", "player"]})
        controller = self.controller(ocr, search)
        first = self.snapshot(controller, {100}, False)
        ocr.set({0: "player"})
        result = self.wait(controller, lambda e: e["type"] == "resolved" and e["revision"] > first["revision"]
                           and e["rows"][0]["status"] == "unmatched")
        self.assertEqual(result["players"], [])
        self.assertEqual(Counter(self.calls), Counter(["Player", "player"]))

    def test_old_inflight_name_cannot_reappear_after_stable_replacement(self):
        blocked = self.barrier()
        entered = self.barrier()
        def search(name, timeout):
            if name == "name0":
                entered.set()
                blocked.wait(.6)
            return self.search(name, timeout)
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr, search)
        self.assertTrue(entered.wait(.3))
        ocr.set({0: "name1"})
        self.snapshot(controller, {101}, False)
        blocked.set()
        time.sleep(.04)
        self.events.extend(controller.poll_events())
        self.assertFalse(any(any(p["id"] == 100 for p in event.get("players", [])) for event in self.events))

    def test_placeholders_preserve_valid_identity_and_do_not_repeat_http(self):
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr)
        first = self.snapshot(controller, {100}, False)
        ocr.set({})
        self.wait(controller, lambda e: e.get("status") == "names_unavailable")
        time.sleep(.035)
        self.events.extend(controller.poll_events())
        self.assertFalse(any(e["type"] == "resolved" and e["revision"] > first["revision"] and not e["players"] for e in self.events))
        ocr.set({0: "name0", 1: "name1"})
        result = self.snapshot(controller, {100, 101}, False)
        self.assertEqual(result["player_deadlines"][100], first["player_deadlines"][100])
        self.assertEqual(Counter(n for n, _, _ in self.calls), Counter(["name0", "name1"]))

    def test_duplicate_id_in_late_row_revokes_every_occurrence_and_reuses_query(self):
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr)
        first = self.snapshot(controller, {100}, False)
        ocr.set({0: "name0", 1: "name0"})
        revoked = self.wait(controller, lambda e: e["type"] == "resolved" and e["revision"] > first["revision"]
                            and sum(r["status"] == "conflict" for r in e["rows"]) == 2)
        self.assertEqual(revoked["players"], [])
        self.assertEqual(len(self.calls), 1)

    def test_successful_query_is_single_flight_across_concurrent_rows(self):
        release = self.barrier()
        def search(name, timeout):
            result = self.search(name, timeout)
            release.wait(.4)
            return result
        ocr = ChangingOCR({0: "name0", 1: "name0"})
        controller = self.controller(ocr, search)
        time.sleep(.04)
        self.assertEqual(len(self.calls), 1)
        release.set()
        result = self.wait(controller, lambda e: e["type"] == "resolved"
                           and sum(r["status"] == "conflict" for r in e["rows"]) == 2)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["name_requests"], 1)

    def test_transient_error_retries_once_after_cooloff_then_stops_on_unchanged_frames(self):
        def search(name, timeout):
            with self.call_lock:
                self.calls.append(time.monotonic())
            raise OSError("temporary fixture error")
        with patch("ba_scout.prep_engine._STREAM_RETRY_DELAY", .04):
            controller = self.controller(ChangingOCR({0: "broken"}), search)
            result = self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "error")
        self.assertEqual(result["players"], [])
        self.assertEqual(len(self.calls), 2)
        self.assertGreaterEqual(self.calls[1] - self.calls[0], .035)
        time.sleep(.06)
        self.assertEqual(len(self.calls), 2)

    def test_transient_failure_recovers_without_manual_restart(self):
        def search(name, timeout):
            result = self.search(name, timeout)
            if len(self.calls) == 1:
                raise OSError("temporary")
            return result
        with patch("ba_scout.prep_engine._STREAM_RETRY_DELAY", .025):
            controller = self.controller(ChangingOCR({0: "name0"}), search)
            result = self.snapshot(controller, {100}, False)
        self.assertEqual(result["name_requests"], 2)
        self.assertEqual(len(self.calls), 2)

    def test_changed_row_does_not_reset_retry_count_for_real_failed_http(self):
        blocked, entered = self.barrier(), self.barrier()
        counts = Counter()
        def search(name, timeout):
            with self.call_lock:
                counts[name] += 1
                ordinal = counts[name]
            if name == "shared":
                if ordinal == 1:
                    entered.set()
                    blocked.wait(.5)
                raise OSError("real failed HTTP fixture")
            return []
        ocr = ChangingOCR({0: ["shared", "other0"]})
        with patch("ba_scout.prep_engine._STREAM_RETRY_DELAY", .025):
            controller = self.controller(ocr, search)
            self.assertTrue(entered.wait(.3))
            ocr.set({0: ["shared", "other1"]})
            # A different variant set replaces the row while the shared
            # actual request is in flight; its retry count still belongs to
            # the generation, not to the old row object.
            until = time.monotonic() + .3
            while time.monotonic() < until and counts["other1"] == 0:
                time.sleep(.003)
            self.assertEqual(counts["other1"], 1)
            blocked.set()
            self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "error")
        self.assertEqual(counts["shared"], 2)

    def test_failed_required_variant_never_releases_other_exact_hit(self):
        def search(name, timeout):
            self.calls.append(name)
            if name == "variant-error":
                raise ValueError("malformed result")
            return [{"id": 123, "name": name}]
        controller = self.controller(ChangingOCR({0: ["exact", "variant-error"]}), search)
        result = self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "error")
        self.assertEqual(result["players"], [])
        self.assertFalse(any(e.get("players") for e in self.events))

    def test_full_fifty_candidates_remain_incomplete_and_are_not_repeated(self):
        def search(name, timeout):
            self.calls.append(name)
            values = [{"id": 123, "name": name}] + [{"id": 1000+i, "name": f"other{i}"} for i in range(49)]
            return parse_search_payload({"players": values, "count": 50}, requested_limit=50, query=name)
        controller = self.controller(ChangingOCR({0: "exact"}), search)
        result = self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "incomplete")
        self.assertEqual(result["players"], [])
        time.sleep(.045)
        self.assertEqual(self.calls, ["exact"])

    def test_429_cooldown_blocks_new_names_without_repeating_network(self):
        def search(name, timeout):
            self.calls.append(name)
            raise HTTPError("https://fixture.invalid/", 429, "rate limit", {}, None)
        ocr = ChangingOCR({0: "first"})
        controller = self.controller(ocr, search, concurrency=1)
        self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "rate_limited")
        ocr.set({0: "first", 1: "second"})
        self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][1]["status"] == "rate_limited")
        self.assertEqual(self.calls, ["first"])

    def test_unchanged_names_resume_when_shared_cooldown_ends(self):
        gate_time = [0.0]
        gate = RequestGate(clock=lambda: gate_time[0])
        calls = []

        def search(name, timeout):
            calls.append(name)
            if len(calls) == 1:
                raise HTTPError("https://fixture.invalid/", 429, "rate limit", {}, None)
            return [{"id": 101, "name": name}]

        controller = self.controller(ChangingOCR({0: "name1"}), search, request_gate=gate)
        limited = self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "rate_limited")
        self.assertGreater(limited["rows"][0]["resolution"]["retry_at"] - time.monotonic(), 59)
        gate_time[0] = 59.0
        time.sleep(.035)
        self.assertEqual(calls, ["name1"])
        gate_time[0] = 60.0
        self.snapshot(controller, {101}, False)
        self.assertEqual(calls, ["name1", "name1"])

    def test_five_staggered_names_recover_one_unchanged_timed_out_row(self):
        ocr = ChangingOCR({0: "name0"})
        entered, release = self.barrier(), self.barrier()
        attempts = Counter()

        def search(name, timeout):
            with self.call_lock:
                attempts[name] += 1
                first_slow_attempt = name == "name1" and attempts[name] == 1
            if first_slow_attempt:
                entered.set()
                release.wait(2)
                raise TimeoutError("first request timed out")
            if name == "other-spelling":
                return []
            return [{"id": 100 + int(name[-1]), "name": name}]

        names = {0: "name0"}
        with patch("ba_scout.prep_engine._STREAM_RETRY_DELAY", .03):
            controller = self.controller(ocr, search, lookup_budget=.35, total_budget=.55)
            first = self.snapshot(controller, {100}, False)
            names[1] = ["name1", "other-spelling"]
            ocr.set(names)
            self.assertTrue(entered.wait(.35))
            names[2] = "name2"
            ocr.set(names)
            self.snapshot(controller, {100, 102}, False)
            self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][1]["status"] == "timeout")
            names[3] = "name3"
            ocr.set(names)
            self.snapshot(controller, {100, 102, 103}, False)
            self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][1]["status"] == "retrying")
            release.set()
            recovered = self.snapshot(controller, {100, 101, 102, 103}, False)
            self.assertGreater(recovered["player_deadlines"][101], first["initial_deadline"])
            self.assertGreater(recovered["player_deadlines"][101], time.monotonic())
            names[4] = "name4"
            ocr.set(names)
            full = self.snapshot(controller, set(range(100, 105)), True)

        self.assertEqual(full["initial_deadline"], first["initial_deadline"])
        self.assertEqual(attempts, Counter({"name0": 1, "name1": 2, "other-spelling": 1,
                                            "name2": 1, "name3": 1, "name4": 1}))

    def test_recovered_variant_with_second_exact_id_stays_ambiguous(self):
        calls = Counter()
        def search(name, timeout):
            with self.call_lock:
                calls[name] += 1
                first = calls[name] == 1
            if name == "second" and first:
                raise TimeoutError("temporary")
            return [{"id": 100 if name == "first" else 101, "name": name}]

        with patch("ba_scout.prep_engine._STREAM_RETRY_DELAY", .03):
            controller = self.controller(ChangingOCR({0: ["first", "second"]}), search)
            self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "ambiguous")
        self.assertFalse(any(e.get("players") for e in self.events))
        self.assertEqual(calls, Counter({"first": 1, "second": 2}))

    def test_more_than_twelve_variants_blocks_entire_row_without_partial_search(self):
        def search(name, timeout):
            self.fail("Over-budget row must not query a subset")
        controller = self.controller(ChangingOCR({0: [f"v{i}" for i in range(13)]}), search)
        result = self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "incomplete")
        self.assertIn("12", result["rows"][0]["resolution"]["reason"])
        self.assertEqual(result["name_requests"], 0)

    def test_global_thirty_request_cap_cannot_be_reset_by_later_capture(self):
        def search(name, timeout):
            with self.call_lock:
                self.calls.append(name)
            return []
        names = {i: [f"row{i}v{j}" for j in range(12)] for i in range(3)}
        ocr = ChangingOCR(names)
        controller = self.controller(ocr, search)
        result = self.wait(controller, lambda e: e["type"] == "resolved" and e["request_limit_reached"])
        self.assertEqual(result["name_request_limit"], 30)
        self.assertEqual(len(self.calls), 30)
        ocr.set({**names, 4: "new-late-name"})
        self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][4]["status"] == "incomplete")
        self.assertEqual(len(self.calls), 30)

    def test_lifecycle_cancel_drops_late_workers_and_all_old_events(self):
        blocked, entered = self.barrier(), self.barrier()
        def search(name, timeout):
            if name == "name0":
                entered.set()
                blocked.wait(.5)
            return self.search(name, timeout)
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr, search)
        self.assertTrue(entered.wait(.3))
        controller.cancel()  # UI loading/game-room/new-match boundary.
        ocr.set({0: "name1"})
        generation = controller.start("NEXT-MATCH", {})
        blocked.set()
        result = self.snapshot(controller, {101}, False)
        self.assertEqual(result["generation"], generation)
        self.assertTrue(all(e["fight_id"] == "NEXT-MATCH" for e in self.events))

    def test_timed_out_row_does_not_prevent_late_row_own_lookup_window(self):
        blocked, entered = self.barrier(), self.barrier()
        def search(name, timeout):
            if name == "name0":
                entered.set()
                blocked.wait(.6)
            return self.search(name, timeout)
        ocr = ChangingOCR({0: "name0"})
        controller = self.controller(ocr, search, lookup_budget=.12, total_budget=.3)
        self.assertTrue(entered.wait(.3))
        self.wait(controller, lambda e: e["type"] == "resolved" and e["rows"][0]["status"] == "timeout")
        ocr.set({0: "name0", 1: "name1"})
        result = self.snapshot(controller, {101}, False)
        blocked.set()
        time.sleep(.03)
        self.events.extend(controller.poll_events())
        self.assertFalse(any(100 in {p["id"] for p in e.get("players", [])} for e in self.events))
        self.assertGreater(result["player_deadlines"][101], result["initial_deadline"])


if __name__ == "__main__":
    unittest.main()
