"""Terminal profile failures must not hold an idle recon round to its deadline."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from ba_scout.scouting_engine import BattleRecon


def envelope(match_id):
    return {"match_id": match_id, "recon_version": 2, "match_start_time": 1000,
            "match_end_time": 1300, "source_url": f"offline:match/{match_id}",
            "players": {"1": {"player_id": 1, "available": True, "deployment_timing": {
                "available": True, "complete": True, "start_time": 1000, "end_time": 1300,
                "missing_spawn_times": 0, "before_start": 0, "after_end": 0, "missing_unit_ids": 0,
                "spawns": [{"id": 1, "name": "Synthetic unit", "type": 4, "offset_s": 5}]}}}}


class HistoryUnavailableTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "work"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="unavailable-", dir=base)
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        self.temp.cleanup()

    def controller(self, fetcher=None):
        def forbidden(*args, **kwargs):
            self.fail("No match request should be started for unavailable history")
        controller = BattleRecon(str(Path(self.temp.name) / str(len(self.controllers))), fetcher or forbidden)
        self.controllers.append(controller)
        return controller

    def collect(self, controller, predicate, timeout=.8):
        events = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            events.extend(controller.poll_events())
            if predicate(events):
                return events
            time.sleep(.002)
        self.fail(f"Timed out waiting for expected offline events: {events!r}")

    def test_terminal_profile_failure_finishes_both_windows_immediately(self):
        controller = self.controller()
        controller.start([1], deadline=5, history_limit=5)
        started = time.monotonic()
        self.assertTrue(controller.mark_unavailable(1, "资料请求失败"))
        events = controller.poll_events()
        self.assertLess(time.monotonic() - started, .2)
        recon = next(e for e in events if e["type"] == "recon")
        opening = next(e for e in events if e["type"] == "opening")
        self.assertEqual(recon["status"], "error")
        self.assertIsNone(recon["match_id"])
        self.assertIsNone(recon["data"])
        for state in opening["windows"].values():
            self.assertEqual(state["status"], "error")
            self.assertTrue(state["finished"])
            self.assertEqual(state["data"]["sample_count"], 0)
            self.assertEqual(state["data"]["requested_count"], 5)
            self.assertEqual(state["data"]["matches"], [])
            self.assertEqual(state["data"]["unusable_matches"], [])
            self.assertIn("资料请求失败", state["message"])
            self.assertNotIn("没有可查询", state["message"])

    def test_existing_pending_history_is_not_cancelled_or_shortened(self):
        gate = threading.Event()
        entered = threading.Event()
        calls = []
        def fetch(mid, timeout):
            calls.append(mid)
            entered.set()
            gate.wait(.5)
            return envelope(mid)
        controller = self.controller(fetch)
        controller.start([1, 2], deadline=2, history_limit=5)
        controller.submit_history(1, [100, 101, 102, 103, 104])
        self.assertTrue(entered.wait(.3))
        try:
            self.assertFalse(controller.mark_unavailable(1, "刷新失败但已有历史"))
            self.assertTrue(controller.mark_unavailable(2, "未取得资料"))
            events = controller.poll_events()
            self.assertFalse(any(e["type"] == "opening" and e["player_id"] == 1 and e["finished"] for e in events))
        finally:
            gate.set()
        events = self.collect(controller, lambda es: any(e["type"] == "opening" and e["player_id"] == 1 and e["finished"] for e in es))
        final = next(e for e in events if e["type"] == "opening" and e["player_id"] == 1 and e["finished"])
        self.assertEqual(final["data"]["sample_count"], 5)
        self.assertEqual(final["data"]["requested_count"], 5)
        self.assertCountEqual(calls, [100, 101, 102, 103, 104])

    def test_later_valid_history_supersedes_unpolled_unavailable_events(self):
        controller = self.controller(lambda mid, timeout: envelope(mid))
        controller.start([1], deadline=2)
        self.assertTrue(controller.mark_unavailable(1, "暂未取得资料"))
        controller.submit_history(1, [100])
        events = self.collect(controller, lambda es: any(e["type"] == "opening" and e["finished"] for e in es))
        self.assertFalse(any("暂未取得资料" in e.get("message", "") for e in events))
        final = next(e for e in events if e["type"] == "opening" and e["finished"])
        self.assertEqual(final["status"], "ok")
        self.assertEqual(final["data"]["sample_count"], 1)
        self.assertFalse(controller.mark_unavailable(1, "晚到的旧错误"))

    def test_duplicate_unknown_or_inactive_calls_are_noops(self):
        controller = self.controller()
        self.assertFalse(controller.mark_unavailable(1, "尚未开始"))
        controller.start([1], deadline=2)
        self.assertFalse(controller.mark_unavailable(9, "非本轮玩家"))
        self.assertFalse(controller.mark_unavailable(True, "非法ID"))
        self.assertTrue(controller.mark_unavailable(1, "首次失败"))
        controller.poll_events()
        self.assertFalse(controller.mark_unavailable(1, "重复失败"))
        self.assertEqual(controller.poll_events(), [])
        controller.cancel()
        self.assertFalse(controller.mark_unavailable(1, "取消后"))
        controller.close()
        self.assertFalse(controller.mark_unavailable(1, "关闭后"))

    def test_explicit_empty_history_is_not_relabelled_as_profile_failure(self):
        controller = self.controller()
        controller.start([1], deadline=2)
        controller.submit_history(1, [])
        self.assertFalse(controller.mark_unavailable(1, "晚到错误"))
        events = controller.poll_events()
        final = next(e for e in events if e["type"] == "opening")
        self.assertTrue(final["finished"])
        self.assertEqual(final["data"]["requested_count"], 0)
        self.assertNotIn("晚到错误", final["message"])

    def test_all_failed_profiles_finish_without_any_match_jobs(self):
        controller = self.controller()
        controller.start([1, 2, 3], deadline=5)
        for player in (1, 2, 3):
            self.assertTrue(controller.mark_unavailable(player, "资料未取得"))
        events = controller.poll_events()
        self.assertEqual(len([e for e in events if e["type"] == "opening" and e["finished"]]), 3)
        self.assertFalse(controller._active.pending)
        self.assertFalse(controller._active.running)

    def test_failure_reason_is_bounded_single_line_and_never_empty(self):
        controller = self.controller()
        controller.start([1, 2], deadline=2)
        controller.mark_unavailable(1, "HTTP\r\n错误" + "x" * 300)
        controller.mark_unavailable(2, "   ")
        events = controller.poll_events()
        messages = [e["message"] for e in events]
        self.assertTrue(all("\n" not in m and "\r" not in m and len(m) < 210 for m in messages))
        self.assertTrue(any("玩家资料未取得" in m for m in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
