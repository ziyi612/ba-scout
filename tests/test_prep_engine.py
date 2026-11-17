"""Offline tests for bounded, partial preparation identity lookup."""

from concurrent.futures import Future
import threading
import time
import unittest
from urllib.error import HTTPError

from ba_scout.prep_engine import PrepController
from ba_scout.player_search import parse_search_payload
from ba_scout.request_gate import RequestGate


def row(index, name=None, team="Bravo", **extra):
    name = name or f"player{index}"
    return {"team": team, "index": index, "queryable": True, "valid": True,
            "variants": [{"text": name, "queryable": True}], **extra}


def frame(rows=None, geometry_valid=True, **extra):
    return {"ok": True, "status": "ok", "rows": rows if rows is not None else [row(i) for i in range(5)],
            "geometry_valid": geometry_valid, **extra}


def manual(count=5, prefix="player", team="Bravo"):
    return [{"team": team, "index": i, "variants": [f"{prefix}{i}"]} for i in range(count)]


class FakeOCR:
    def __init__(self, *frames):
        self.frames = list(frames) or [frame()]
        self.calls = 0
        self.timeouts = []

    def submit_capture_game(self, *, timeout, regions=None):
        self.timeouts.append(timeout)
        value = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        if isinstance(value, Future):
            return value
        result = Future()
        if isinstance(value, Exception):
            result.set_exception(value)
        else:
            result.set_result(value)
        return result


def wait_resolved(controller, timeout=1.5):
    events = []
    end = time.monotonic() + timeout
    changed = time.monotonic()
    while time.monotonic() < end:
        incoming = controller.poll_events()
        if incoming:
            changed = time.monotonic()
            events.extend(incoming)
        matches = [event for event in events if event["type"] == "resolved"]
        # Legacy tests inspect the settled available roster. Streaming tests
        # separately assert first-result timing and each intermediate revision.
        if matches and (matches[-1].get("final", True) or time.monotonic() - changed >= .04):
            return matches[-1], events
        time.sleep(0.003)
    raise AssertionError(f"No resolved event: {events}")


def wait_until(predicate, timeout=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("Condition did not become true")


class PrepTests(unittest.TestCase):
    def setUp(self):
        self.controllers = []
        self.release = []
        self.calls = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        for event in self.release:
            event.set()

    def search(self, name, timeout):
        self.calls.append((name, timeout))
        return [{"id": 100 + int(name[-1]), "name": name}]

    def controller(self, ocr=None, **kwargs):
        kwargs.setdefault("searcher", self.search)
        kwargs.setdefault("extractor", lambda captured, regions: captured)
        kwargs.setdefault("capture_interval", 0.01)
        kwargs.setdefault("lookup_budget", 0.6)
        kwargs.setdefault("total_budget", 1.0)
        request_gate = kwargs.pop("request_gate", RequestGate())
        controller = PrepController(ocr or FakeOCR(), request_gate, **kwargs)
        self.controllers.append(controller)
        return controller

    def test_per_variant_completeness_survives_merge_for_identity_and_prefetch_event(self):
        def search(name, timeout):
            self.calls.append(name)
            rows = [{"id": 1000 + i, "name": f"other-{name}-{i}"} for i in range(30)]
            if name == "Laser":
                rows[0] = {"id": 810003, "name": "Laser"}
            return parse_search_payload({"players": rows, "count": len(rows)},
                                        requested_limit=50, query=name)
        controller = self.controller(searcher=search)
        controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 0,
                                                   "variants": ["Laser", "Laser-"]}])
        result, events = wait_resolved(controller)
        self.assertEqual(self.calls, ["Laser", "Laser-"])
        self.assertEqual([player["id"] for player in result["players"]], [810003])
        self.assertEqual(result["name_requests"], 2)
        queries = result["rows"][0]["resolution"]["queries"]
        self.assertEqual([query["returned_count"] for query in queries], [30, 30])
        self.assertTrue(all(query["complete"] for query in queries))
        ready = [event for event in events if event["type"] == "identity_ready"]
        self.assertEqual(len(ready), 1)

    def test_full_page_does_not_emit_resolved_player_or_identity_ready(self):
        def search(name, timeout):
            self.calls.append(name)
            rows = [{"id": 1, "name": name}] + [
                {"id": i + 2, "name": f"other{i}"} for i in range(49)]
            return parse_search_payload({"players": rows, "count": 50}, requested_limit=50, query=name)
        controller = self.controller(searcher=search)
        controller.start("100", None, manual_rows=manual(1))
        result, events = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"][0]["status"], "incomplete")
        self.assertEqual(result["name_requests"], 1)
        self.assertFalse(any(event["type"] == "identity_ready" for event in events))

    def test_failed_second_variant_does_not_adopt_complete_first_response(self):
        def search(name, timeout):
            self.calls.append(name)
            if name.endswith("-"):
                raise TimeoutError("offline second-variant failure")
            rows = [{"id": 810003, "name": name}]
            return parse_search_payload({"players": rows, "count": 1}, requested_limit=50, query=name)
        controller = self.controller(searcher=search)
        controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 0,
                                                   "variants": ["Laser", "Laser-"]}])
        result, events = wait_resolved(controller)
        self.assertEqual(self.calls, ["Laser", "Laser-"])
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"][0]["status"], "error")
        self.assertEqual(result["name_requests"], 2)
        self.assertFalse(any(event["type"] == "identity_ready" for event in events))

    def test_wrong_query_metadata_is_not_trusted_by_controller(self):
        def search(name, timeout):
            rows = [{"id": 810003, "name": name}]
            return parse_search_payload({"players": rows, "count": 1}, requested_limit=50, query="unqueried")
        controller = self.controller(searcher=search)
        controller.start("100", None, manual_rows=manual(1))
        result, events = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"][0]["status"], "invalid")
        self.assertFalse(any(event["type"] == "identity_ready" for event in events))

    def test_three_stable_bravo_rows_allow_partial_without_alpha(self):
        rows = [row(i, queryable=i < 3) for i in range(5)]
        ocr = FakeOCR(frame(rows))
        controller = self.controller(ocr)
        generation = controller.start("100", {"fixture": True})
        result, events = wait_resolved(controller)
        self.assertGreaterEqual(ocr.calls, 2)
        self.assertFalse(result["final"])
        self.assertEqual({player["id"] for player in result["players"]}, {100, 101, 102})
        self.assertEqual([item["status"] for item in result["rows"]], ["resolved"] * 3 + ["missing"] * 2)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual((result["generation"], result["fight_id"], result["source"]), (generation, "100", "screen"))
        self.assertEqual(result["identity_status"], "screen_name_match_pending_log_id")
        self.assertAlmostEqual(result["deadline"] - result["started"], 1.0)
        self.assertTrue(any(event.get("status") == "stabilizing" and event["started"] is not None for event in events))
        self.assertTrue(all(player["identity_source"] == "screen-exact" for player in result["players"]))

    def test_no_lookup_before_second_stable_frame_and_clock_begins_on_first(self):
        second = Future()
        ocr = FakeOCR(frame(), second)
        controller = self.controller(ocr)
        controller.start("100", {})
        wait_until(lambda: ocr.calls == 2)
        events = controller.poll_events()
        self.assertEqual(self.calls, [])
        first = next(event for event in events if event.get("status") == "stabilizing")
        self.assertIsNotNone(first["started"])
        second.set_result(frame())
        result, _ = wait_resolved(controller)
        self.assertEqual(result["started"], first["started"])
        self.assertGreaterEqual(result["lookup_elapsed"], 0.01)

    def test_two_bravo_rows_query_independently_and_keep_observing_missing_rows(self):
        rows = [row(i, queryable=i < 2) for i in range(5)]
        rows += [row(i, team="Alpha") for i in range(5)]
        ocr = FakeOCR(frame(rows))
        controller = self.controller(ocr, lookup_budget=0.3)
        controller.start("100", {})
        wait_until(lambda: ocr.calls >= 8)
        events = controller.poll_events()
        self.assertEqual({name for name, _ in self.calls}, {"player0", "player1"})
        result = [event for event in events if event["type"] == "resolved"][-1]
        self.assertEqual({player["id"] for player in result["players"]}, {100, 101})
        self.assertFalse(result["final"])
        self.assertIsNotNone(result["started"])

    def test_first_valid_frame_capture_delay_is_included_in_total_budget(self):
        first = Future()
        ocr = FakeOCR(first, frame())
        controller = self.controller(ocr)
        before = time.monotonic()
        controller.start("100", {})
        wait_until(lambda: ocr.calls == 1)
        time.sleep(0.06)
        released = time.monotonic()
        first.set_result(frame())
        result, events = wait_resolved(controller)
        self.assertGreaterEqual(result["started"], before)
        self.assertLessEqual(result["started"], released - 0.05)
        self.assertGreaterEqual(result["lookup_elapsed"], 0.06)
        self.assertAlmostEqual(result["deadline"] - result["started"], 1.0)
        self.assertTrue(any(event.get("status") == "stabilizing" and event["elapsed"] >= 0.06 for event in events))

    def test_real_extractor_partial_cross_row_frame_integrates_without_network(self):
        regions = {"Alpha": [.1, .1, .3, .5], "Bravo": [.6, .1, .3, .5]}
        lines = [{"text": f"Bravo{i}", "words": [{"text": f"Bravo{i}", "x": 620,
                  "y": 190 if i == 0 else 120 + i * 100, "width": 90, "height": 20}]} for i in range(5)]
        captured = {"ok": True, "status": "ok", "image_width": 1000, "image_height": 1000,
                    "results": [{"language": "en-GB", "lines": lines}]}
        controller = self.controller(FakeOCR(captured), extractor=None)
        controller.start("100", regions)
        result, _ = wait_resolved(controller)
        self.assertEqual({player["id"] for player in result["players"]}, {102, 103, 104})
        self.assertEqual(len(result["rows"]), 10)
        self.assertEqual([item["status"] for item in result["rows"][:5]], ["missing", "missing", "resolved", "resolved", "resolved"])
        self.assertTrue(result["rows"][0]["issues"])
        self.assertTrue(result["rows"][1]["observed_variants"])
        self.assertEqual(len(self.calls), 3)

    def test_invalid_roi_geometry_never_searches_despite_readable_names(self):
        ocr = FakeOCR(frame(geometry_valid=False))
        controller = self.controller(ocr)
        controller.start("100", {})
        wait_until(lambda: ocr.calls >= 3)
        events = controller.poll_events()
        self.assertEqual(self.calls, [])
        self.assertTrue(any(event.get("capture_status") == "invalid_geometry" for event in events))
        self.assertTrue(all(event["started"] is None for event in events))

    def test_capture_failure_emits_status_without_network(self):
        ocr = FakeOCR({"ok": False, "status": "not_foreground", "error": "game is not foreground"})
        controller = self.controller(ocr)
        controller.start("100", {})
        wait_until(lambda: ocr.calls >= 2)
        events = controller.poll_events()
        self.assertTrue(any(event.get("status") == "capture_error" and event.get("capture_status") == "not_foreground" for event in events))
        self.assertEqual(self.calls, [])

    def test_changed_frame_cannot_count_as_stable_and_budget_finishes_empty(self):
        class AlternatingOCR(FakeOCR):
            def submit_capture_game(self, *, timeout, regions=None):
                self.calls += 1
                result = Future()
                result.set_result(frame([row(i, f"{self.calls % 2}-player{i}") for i in range(5)]))
                return result
        ocr = AlternatingOCR()
        controller = self.controller(ocr, lookup_budget=0.08)
        controller.start("100", {})
        result, _ = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(self.calls, [])
        self.assertGreaterEqual(result["lookup_elapsed"], 0.075)

    def test_capture_error_resets_consecutive_frame_stability(self):
        ocr = FakeOCR(frame(), {"ok": False, "status": "black_frame"}, frame(), frame())
        controller = self.controller(ocr)
        controller.start("100", {})
        result, _ = wait_resolved(controller)
        self.assertEqual(ocr.calls, 4)
        self.assertEqual(len(result["players"]), 5)

    def test_manual_one_row_has_no_capture_or_three_row_requirement(self):
        ocr = FakeOCR()
        controller = self.controller(ocr)
        controller.start("100", None, manual_rows=manual(1))
        result, _ = wait_resolved(controller)
        self.assertEqual(ocr.calls, 0)
        self.assertEqual(result["source"], "manual")
        self.assertEqual(len(result["players"]), 1)
        self.assertEqual(result["players"][0]["identity_source"], "manual-exact")

    def test_manual_confirmed_id_skips_search_and_is_explicitly_marked(self):
        controller = self.controller()
        controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 2, "player_id": "810001", "name": "样例"}])
        result, _ = wait_resolved(controller)
        self.assertEqual(self.calls, [])
        self.assertEqual(result["rows"][0]["status"], "manual-confirmed")
        self.assertEqual(result["players"][0]["id"], 810001)
        self.assertEqual(result["players"][0]["identity_source"], "manual-confirmed")

    def test_invalid_confirmed_id_does_not_fall_back_to_name_search(self):
        controller = self.controller()
        controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 0, "player_id": True, "name": "player0", "variants": ["player0"]}])
        result, _ = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"][0]["status"], "invalid")
        self.assertEqual(self.calls, [])

    def test_one_failed_required_variant_invalidates_whole_row_but_keeps_hints(self):
        def search(name, timeout):
            self.calls.append(name)
            if name == "alternative":
                raise TimeoutError("variant timed out")
            return [{"id": 123, "name": name}]
        controller = self.controller(searcher=search)
        controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 0, "variants": ["exact", "alternative"]}])
        result, events = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"][0]["status"], "error")
        self.assertEqual(result["rows"][0]["candidates"], [{"id": 123, "name": "exact"}])
        self.assertEqual(self.calls, ["exact", "alternative"])
        self.assertFalse(any(event['type'] == 'identity_ready' for event in events))

    def test_identity_ready_waits_for_complete_variants_but_not_slower_other_rows(self):
        first_done, release_variant, release_other = (threading.Event() for _ in range(3))
        self.release.extend([release_variant, release_other])
        def search(name, timeout):
            if name == 'exact':
                first_done.set()
            elif name == 'alternate':
                release_variant.wait(1)
            else:
                release_other.wait(1)
            return [{'id': 123 if name in ('exact', 'alternate') else 456, 'name': name}]
        controller = self.controller(searcher=search)
        controller.start('100', None, manual_rows=[{'team': 'Bravo', 'index': 0, 'variants': ['exact', 'alternate']},
                                                 {'team': 'Bravo', 'index': 1, 'variants': ['slow-other']}])
        self.assertTrue(first_done.wait(.3))
        time.sleep(.015)
        self.assertFalse(any(event['type'] == 'identity_ready' for event in controller.poll_events()))
        release_variant.set()
        early = []
        def collect():
            early.extend(controller.poll_events())
            return any(event['type'] == 'identity_ready' for event in early)
        wait_until(collect)
        self.assertFalse(any(event['type'] == 'resolved' for event in early))
        ready = next(event for event in early if event['type'] == 'identity_ready')
        self.assertEqual((ready['player_id'], ready['team'], ready['index']), (123, 'Bravo', 0))
        self.assertFalse(ready['identity_final'])
        release_other.set()
        result, _ = wait_resolved(controller)
        self.assertEqual({p['id'] for p in result['players']}, {123, 456})
        self.assertEqual(ready['deadline'], result['deadline'])

    def test_early_identity_is_not_final_and_duplicate_rows_still_invalidate_roster(self):
        release = threading.Event()
        self.release.append(release)
        def search(name, timeout):
            if name == 'player1':
                release.wait(1)
            return [{'id': 123, 'name': name}]
        controller = self.controller(searcher=search)
        controller.start('100', None, manual_rows=manual(2))
        early = []
        def collect():
            early.extend(controller.poll_events())
            return any(event['type'] == 'identity_ready' for event in early)
        wait_until(collect)
        self.assertFalse(next(e for e in early if e['type'] == 'identity_ready')['identity_final'])
        release.set()
        result, _ = wait_resolved(controller)
        self.assertEqual(result['players'], [])
        self.assertTrue(all(row['status'] == 'conflict' for row in result['rows']))

    def test_multiple_successful_variants_matching_different_ids_stay_ambiguous(self):
        controller = self.controller()
        controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 0, "variants": ["player0", "player1"]}])
        result, _ = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"][0]["status"], "ambiguous")
        self.assertEqual(len(result["rows"][0]["candidates"]), 2)

    def test_same_id_in_multiple_rows_invalidates_every_conflicting_row(self):
        controller = self.controller(searcher=lambda name, timeout: [{"id": 1, "name": name}])
        controller.start("100", None, manual_rows=manual(2) + [{"team": "Alpha", "index": 0, "player_id": 1, "name": "confirmed"}])
        result, _ = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertTrue(all(item["status"] == "conflict" for item in result["rows"]))
        self.assertTrue(all(item["resolution"]["player_id"] is None for item in result["rows"]))

    def test_all_names_failing_still_emits_empty_resolved_once(self):
        def fail(name, timeout):
            raise OSError("offline")
        controller = self.controller(searcher=fail)
        controller.start("100", None, manual_rows=manual())
        result, _ = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(len(result["rows"]), 5)
        self.assertTrue(all(item["status"] == "error" for item in result["rows"]))
        time.sleep(0.03)
        self.assertEqual(controller.poll_events(), [])

    def test_deadline_publishes_partial_and_ignores_late_result(self):
        release = threading.Event()
        self.release.append(release)
        def search(name, timeout):
            if name == "player4":
                release.wait(1)
            return [{"id": 100 + int(name[-1]), "name": name}]
        controller = self.controller(searcher=search, lookup_budget=0.12)
        controller.start("100", None, manual_rows=manual())
        result, _ = wait_resolved(controller)
        self.assertEqual(len(result["players"]), 4)
        self.assertEqual(result["rows"][4]["status"], "timeout")
        self.assertLess(result["lookup_elapsed"], 0.3)
        self.assertAlmostEqual(result["deadline"] - result["started"], 1)
        release.set()
        time.sleep(0.03)
        self.assertEqual(controller.poll_events(), [])

    def test_fixed_pool_does_not_double_concurrency_after_cancel_restart(self):
        release = threading.Event()
        self.release.append(release)
        lock = threading.Lock()
        counts = {"active": 0, "peak": 0, "entered": 0}
        def search(name, timeout):
            with lock:
                counts["active"] += 1
                counts["entered"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
            if name.startswith("old"):
                release.wait(1)
            with lock:
                counts["active"] -= 1
            return [{"id": 100 + int(name[-1]), "name": name}]
        controller = self.controller(searcher=search)
        old = controller.start("100", None, manual_rows=manual(prefix="old") + manual(prefix="ally", team="Alpha"))
        wait_until(lambda: counts["entered"] == 5)
        controller.cancel()
        new = controller.start("101", None, manual_rows=manual(prefix="new"))
        time.sleep(0.02)
        self.assertEqual(counts["entered"], 5)
        release.set()
        result, events = wait_resolved(controller)
        self.assertNotEqual(old, new)
        self.assertEqual(result["generation"], new)
        self.assertEqual(result["fight_id"], "101")
        self.assertEqual(len(result["players"]), 5)
        self.assertEqual(counts["peak"], 5)
        self.assertTrue(all(event["generation"] == new for event in events))
        self.assertEqual(len(controller._workers), 5)
        self.assertTrue(all(worker.daemon for worker in controller._workers))

    def test_bravo_queries_are_queued_before_alpha(self):
        entered = []
        def search(name, timeout):
            entered.append(name)
            return [{"id": (100 if name.startswith("enemy") else 200) + int(name[-1]), "name": name}]
        controller = self.controller(searcher=search, concurrency=1)
        controller.start("100", None, manual_rows=manual(prefix="ally", team="Alpha") + manual(prefix="enemy"))
        result, _ = wait_resolved(controller)
        self.assertEqual(entered[:5], [f"enemy{i}" for i in range(5)])
        self.assertEqual(len(result["players"]), 8)
        self.assertEqual(len(entered), 8)
        self.assertTrue(all(row["status"] == "incomplete" for row in result["rows"][-2:]))

    def test_default_timing_remains_twelve_seconds_and_point_three_five_interval(self):
        controller = PrepController(FakeOCR(), RequestGate(), searcher=self.search)
        self.controllers.append(controller)
        self.assertEqual(controller.lookup_budget, 12.0)
        self.assertEqual(controller.capture_interval, 0.35)
        self.assertEqual(controller.total_budget, 20.0)

    def test_nine_required_variants_stop_at_eight_and_never_resolve_partial_exact_hit(self):
        def search(name, timeout):
            self.calls.append(name)
            return [{"id": 123, "name": name}]
        controller = self.controller(searcher=search)
        controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 0,
                         "variants": [f"variant{i}" for i in range(9)]}])
        result, events = wait_resolved(controller)
        self.assertEqual(self.calls, [f"variant{i}" for i in range(8)])
        self.assertEqual(result["name_requests"], 8)
        self.assertEqual(result["name_request_limit"], 8)
        self.assertTrue(result["request_limit_reached"])
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"][0]["status"], "incomplete")
        self.assertIsNone(result["rows"][0]["resolution"]["player_id"])
        self.assertEqual(len(result["rows"][0]["candidates"]), 8)
        self.assertIn("未完整查询", result["rows"][0]["resolution"]["reason"])
        self.assertIn("本轮姓名搜索上限", result["message"])
        self.assertTrue(any(event.get("status") == "request_limit" for event in events))
        self.assertFalse(any(event['type'] == 'identity_ready' for event in events))
        self.assertLess(result["lookup_elapsed"], 0.3)

    def test_failed_calls_still_consume_cap_without_retries_or_waiting_for_unused_rows(self):
        def search(name, timeout):
            self.calls.append(name)
            raise OSError("fixture failure")
        controller = self.controller(searcher=search, concurrency=1)
        controller.start("100", None, manual_rows=manual(prefix="enemy") + manual(prefix="ally", team="Alpha"))
        result, _ = wait_resolved(controller)
        self.assertEqual(self.calls, [f"enemy{i}" for i in range(5)] + [f"ally{i}" for i in range(3)])
        self.assertEqual(result["name_requests"], 8)
        self.assertEqual([row["status"] for row in result["rows"]], ["error"] * 8 + ["incomplete"] * 2)
        self.assertLess(result["lookup_elapsed"], 0.3)

    def test_five_actual_inflight_enemies_remain_priority_and_finish_without_ally_http(self):
        fast_release, slow_release, all_entered = threading.Event(), threading.Event(), threading.Event()
        self.release.extend([fast_release, slow_release])
        lock = threading.Lock()
        counts = {"active": 0, "peak": 0, "enemy_entered": 0, "enemy_finished": 0}
        calls, ally_observed_finished = [], []
        def search(name, timeout):
            with lock:
                calls.append(name)
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
                if name.startswith("enemy"):
                    counts["enemy_entered"] += 1
                    if counts["enemy_entered"] == 5:
                        all_entered.set()
                else:
                    ally_observed_finished.append(counts["enemy_finished"])
            if name.startswith("enemy"):
                (slow_release if name == "enemy4" else fast_release).wait(1)
            with lock:
                counts["active"] -= 1
                if name.startswith("enemy"):
                    counts["enemy_finished"] += 1
            return [{"id": (100 if name.startswith("enemy") else 200) + int(name[-1]), "name": name}]
        automatic = [row(i, f"ally{i}", "Alpha") for i in range(5)] + [row(i, f"enemy{i}") for i in range(5)]
        controller = self.controller(FakeOCR(frame(automatic)), searcher=search)
        controller.start("100", {})
        self.assertTrue(all_entered.wait(.4))
        fast_release.set()
        wait_until(lambda: counts["enemy_finished"] == 4)
        time.sleep(.025)
        self.assertEqual(len(calls), 5)
        self.assertEqual(ally_observed_finished, [])
        slow_release.set()
        result, _ = wait_resolved(controller)
        self.assertEqual(counts["peak"], 5)
        self.assertEqual(ally_observed_finished, [])
        self.assertEqual(len(calls), 5)
        self.assertEqual(result["name_requests"], 5)
        self.assertEqual(sum(p["team"] == "Bravo" for p in result["players"]), 5)
        self.assertEqual(sum(p["team"] == "Alpha" for p in result["players"]), 0)
        self.assertTrue(result["final"])

    def test_enemy_deadline_skips_allies_and_does_not_extend_round(self):
        release = threading.Event()
        self.release.append(release)
        def search(name, timeout):
            self.calls.append(name)
            release.wait(1)
            return [{"id": 100, "name": name}]
        controller = self.controller(searcher=search, lookup_budget=.08)
        controller.start("100", None, manual_rows=manual(1, prefix="enemy") + manual(prefix="ally", team="Alpha"))
        result, _ = wait_resolved(controller)
        self.assertEqual(self.calls, ["enemy0"])
        self.assertTrue(all(row["status"] == "timeout" for row in result["rows"]))
        self.assertTrue(all("未启动" in row["resolution"]["reason"] for row in result["rows"] if row["team"] == "Alpha"))
        self.assertLess(result["lookup_elapsed"], .25)
        self.assertAlmostEqual(result["deadline"] - result["started"], 1.0)

    def test_completed_round_request_budget_resets_without_changing_deadlines(self):
        def search(name, timeout):
            self.calls.append(name)
            return []
        controller = self.controller(searcher=search)
        generations = []
        for fight in ("100", "101"):
            generations.append(controller.start(fight, None, manual_rows=[{"team": "Bravo", "index": 0,
                               "variants": [f"{fight}-variant{i}" for i in range(9)]}]))
            result, _ = wait_resolved(controller)
            self.assertEqual(result["name_requests"], 8)
            self.assertTrue(result["request_limit_reached"])
            self.assertAlmostEqual(result["deadline"] - result["started"], 1.0)
        self.assertNotEqual(*generations)
        self.assertEqual(len(self.calls), 16)

    def test_cancelled_inflight_request_cannot_consume_or_publish_into_new_round_cap(self):
        release, entered = threading.Event(), threading.Event()
        self.release.append(release)
        lock = threading.Lock()
        def search(name, timeout):
            with lock:
                self.calls.append(name)
            if name == "old":
                entered.set()
                release.wait(1)
            return [{"id": 123, "name": name}]
        controller = self.controller(searcher=search)
        old = controller.start("100", None, manual_rows=[{"team": "Bravo", "index": 0, "variants": ["old", "old-second"]}])
        self.assertTrue(entered.wait(.3))
        controller.cancel()
        current = controller.start("101", None, manual_rows=[{"team": "Bravo", "index": 0,
                                 "variants": [f"new{i}" for i in range(9)]}])
        result, events = wait_resolved(controller)
        self.assertNotEqual(old, current)
        self.assertEqual(result["name_requests"], 8)
        self.assertTrue(all(event["generation"] == current for event in events))
        release.set()
        time.sleep(.025)
        self.assertNotIn("old-second", self.calls)
        self.assertEqual(len([name for name in self.calls if name.startswith("new")]), 8)
        self.assertEqual(controller.poll_events(), [])

    def test_repeated_automatic_start_same_fight_does_not_repeat_search(self):
        ocr = FakeOCR(frame())
        controller = self.controller(ocr)
        first = controller.start("100", {})
        wait_resolved(controller)
        count = len(self.calls)
        self.assertEqual(controller.start("100", {}), first)
        time.sleep(0.025)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(ocr.calls, 2)
        controller.cancel()
        self.assertNotEqual(controller.start("100", {}), first)
        wait_resolved(controller)
        self.assertEqual(len(self.calls), count + 5)

    def test_429_shared_gate_prevents_further_search_and_profile_calls(self):
        gate = RequestGate(clock=lambda: 0)
        def search(name, timeout):
            self.calls.append(name)
            raise HTTPError("https://dash.batrace.top/api/players/search", 429, "limited", {}, None)
        controller = self.controller(searcher=search, concurrency=1, request_gate=gate)
        controller.start("100", None, manual_rows=manual())
        result, _ = wait_resolved(controller)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["players"], [])
        with self.assertRaisesRegex(RuntimeError, "限流"):
            gate.run(lambda: self.fail("profile must not start after 429"))

    def test_close_returns_promptly_without_waiting_for_inflight_http(self):
        release = threading.Event()
        entered = threading.Event()
        self.release.append(release)
        def search(name, timeout):
            entered.set()
            release.wait(1)
            return [{"id": 1, "name": name}]
        controller = self.controller(searcher=search)
        controller.start("100", None, manual_rows=manual(1))
        self.assertTrue(entered.wait(0.5))
        started = time.monotonic()
        controller.close()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(controller.poll_events(), [])
        with self.assertRaises(RuntimeError):
            controller.start("101", None, manual_rows=manual(1))

    def test_cancelled_inflight_capture_is_not_replaced_with_another_concurrent_capture(self):
        pending = Future()
        pending.set_running_or_notify_cancel()
        ocr = FakeOCR(pending, frame(), frame())
        controller = self.controller(ocr)
        old = controller.start("100", {})
        wait_until(lambda: ocr.calls == 1)
        controller.cancel()
        new = controller.start("101", {})
        time.sleep(0.04)
        self.assertEqual(ocr.calls, 1)
        pending.set_result(frame())
        result, events = wait_resolved(controller)
        self.assertEqual(result["generation"], new)
        self.assertNotEqual(old, new)
        self.assertEqual(ocr.calls, 3)
        self.assertTrue(all(event["fight_id"] == "101" for event in events))

    def test_duplicate_row_geometry_rejected_before_any_query(self):
        controller = self.controller()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            controller.start("100", None, manual_rows=manual(1) + manual(1))
        self.assertEqual(self.calls, [])

    def test_manual_empty_input_emits_empty_result_not_fake_ids(self):
        controller = self.controller()
        controller.start("100", None, manual_rows=[])
        result, _ = wait_resolved(controller)
        self.assertEqual(result["players"], [])
        self.assertEqual(result["rows"], [])
        self.assertEqual(self.calls, [])

    def test_request_timeout_never_exceeds_remaining_identity_budget(self):
        controller = self.controller(lookup_budget=0.1, search_timeout=4)
        controller.start("100", None, manual_rows=manual(1))
        wait_resolved(controller)
        self.assertTrue(self.calls)
        self.assertTrue(0 < self.calls[0][1] <= 0.1)

    @staticmethod
    def placeholder_capture(placeholder=True, count=5):
        lines = [{"text": "Player name" if placeholder and i < count else f"Bravo{i}",
                  "words": [{"text": "Player name" if placeholder and i < count else f"Bravo{i}",
                             "x": 620, "y": 120 + i * 100, "width": 120, "height": 20}]} for i in range(5)]
        return {"ok": True, "status": "ok", "image_width": 1000, "image_height": 1000,
                "results": [{"language": "en-GB", "lines": lines}]}

    def test_placeholder_frames_do_not_start_clock_or_search_even_with_language_misreads(self):
        captured = self.placeholder_capture()
        captured["results"].append({"language": "ru", "lines": [
            {"text": f"Misread{i}", "words": [{"text": f"Misread{i}", "x": 620,
             "y": 120 + i * 100, "width": 120, "height": 20}]} for i in range(5)]})
        ocr = FakeOCR(captured)
        controller = self.controller(ocr, extractor=None, lookup_budget=0.04)
        controller.start("100", {"Alpha": [.1, .1, .3, .5], "Bravo": [.6, .1, .3, .5]})
        wait_until(lambda: ocr.calls >= 8)
        events = controller.poll_events()
        status = [event for event in events if event.get("status") == "names_unavailable"]
        self.assertTrue(status)
        self.assertEqual(status[0]["placeholder_count"], 5)
        self.assertTrue(all(event["started"] is None and event["deadline"] is None for event in events))
        self.assertFalse(any(event["type"] == "resolved" for event in events))
        self.assertEqual(self.calls, [])

    def test_placeholder_to_real_names_recovers_in_same_generation_with_one_budget(self):
        placeholder = self.placeholder_capture()
        real = self.placeholder_capture(False)
        second_real = Future()
        class SwitchingOCR(FakeOCR):
            ready = False
            real_calls = 0
            first_real_started = None
            def submit_capture_game(inner, *, timeout, regions=None):
                inner.calls += 1
                inner.timeouts.append(timeout)
                if inner.ready:
                    inner.real_calls += 1
                    if inner.real_calls == 1:
                        inner.first_real_started = time.monotonic()
                        result = Future()
                        result.set_result(real)
                        return result
                    return second_real
                result = Future()
                result.set_result(placeholder)
                return result
        ocr = SwitchingOCR()
        controller = self.controller(ocr, extractor=None, lookup_budget=0.6, total_budget=20)
        generation = controller.start("100", {"Alpha": [.1, .1, .3, .5], "Bravo": [.6, .1, .3, .5]})
        wait_until(lambda: ocr.calls >= 6)
        placeholder_events = controller.poll_events()
        self.assertEqual(self.calls, [])
        self.assertTrue(all(event["started"] is None for event in placeholder_events))
        ocr.ready = True
        wait_until(lambda: ocr.real_calls == 2)
        first_events = controller.poll_events()
        stable = next(event for event in first_events if event.get("status") == "stabilizing")
        self.assertEqual(self.calls, [])
        # Time spent obtaining the second frame must remain in the same budget.
        time.sleep(0.035)
        second_real.set_result(real)
        result, events = wait_resolved(controller)
        self.assertEqual(result["generation"], generation)
        self.assertEqual(result["started"], stable["started"])
        self.assertAlmostEqual(result["started"], ocr.first_real_started, delta=0.02)
        self.assertAlmostEqual(result["deadline"] - result["started"], 20)
        self.assertGreaterEqual(result["lookup_elapsed"], 0.035)
        self.assertEqual({p["id"] for p in result["players"]}, set(range(100, 105)))
        self.assertEqual(ocr.real_calls, 2)
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all(event["started"] == stable["started"] for event in events))
        self.assertTrue(all(timeout < 0.6 for _, timeout in self.calls))

    def test_placeholder_interrupts_frame_stability_without_resetting_started_budget(self):
        real = self.placeholder_capture(False)
        ocr = FakeOCR(real, self.placeholder_capture(), real, real)
        controller = self.controller(ocr, extractor=None, total_budget=20)
        controller.start("100", {"Alpha": [.1, .1, .3, .5], "Bravo": [.6, .1, .3, .5]})
        result, events = wait_resolved(controller)
        self.assertEqual(ocr.calls, 4)
        first = next(event for event in events if event.get("status") == "stabilizing")
        placeholder = next(event for event in events if event.get("status") == "names_unavailable")
        self.assertEqual(result["started"], first["started"])
        self.assertEqual(placeholder["started"], first["started"])
        self.assertEqual(result["initial_deadline"], placeholder["initial_deadline"])
        self.assertAlmostEqual(result["initial_deadline"] - result["started"], 20)
        self.assertGreaterEqual(result["deadline"], result["initial_deadline"])
        self.assertEqual(len(result["players"]), 5)

    def test_three_placeholders_do_not_block_two_real_opponents_and_missing_rows_keep_waiting(self):
        regions = {"Alpha": [.1, .1, .3, .5], "Bravo": [.6, .1, .3, .5]}
        blocked_ocr = FakeOCR(self.placeholder_capture(count=3))
        blocked = self.controller(blocked_ocr, extractor=None)
        blocked.start("100", regions)
        wait_until(lambda: blocked_ocr.calls >= 5)
        events = blocked.poll_events()
        results = [event for event in events if event["type"] == "resolved"]
        self.assertEqual({p["id"] for p in results[-1]["players"]}, {103, 104})
        self.assertFalse(results[-1]["final"])
        self.assertEqual({name for name, _ in self.calls}, {"Bravo3", "Bravo4"})
        blocked.close()
        self.calls.clear()
        partial = self.controller(FakeOCR(self.placeholder_capture(count=2)), extractor=None)
        partial.start("101", regions)
        result, _ = wait_resolved(partial)
        self.assertEqual({player["id"] for player in result["players"]}, {102, 103, 104})
        self.assertEqual({name for name, _ in self.calls}, {"Bravo2", "Bravo3", "Bravo4"})


if __name__ == "__main__":
    unittest.main()
