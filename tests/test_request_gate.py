"""Shared rate-limit checks without real HTTP or cooldown sleeps."""

import threading
import unittest
from urllib.error import HTTPError

from ba_scout.request_gate import RequestGate


def http_error(code):
    return HTTPError("https://dash.batrace.top/player/1", code, "test HTTP error", {}, None)


class RequestGateTests(unittest.TestCase):
    def test_run_forwards_arguments_and_return_value(self):
        gate = RequestGate()
        marker = object()
        received = []

        def operation(player_id, timeout):
            received.append((player_id, timeout))
            return marker

        self.assertIs(gate.run(operation, 1, timeout=8), marker)
        self.assertEqual(received, [(1, 8)])

    def test_429_blocks_other_callable_until_60_seconds(self):
        now = [0.0]
        gate = RequestGate(clock=lambda: now[0])
        calls = []

        def profile():
            calls.append("profile")
            raise http_error(429)

        def recent_match():
            calls.append("recent")
            return "ready"

        with self.assertRaises(HTTPError):
            gate.run(profile)
        with self.assertRaisesRegex(RuntimeError, "60 秒"):
            gate.run(recent_match)
        now[0] = 59.2
        with self.assertRaisesRegex(RuntimeError, "1 秒"):
            gate.run(recent_match)
        self.assertEqual(calls, ["profile"])
        now[0] = 60.0
        self.assertEqual(gate.run(recent_match), "ready")
        self.assertEqual(calls, ["profile", "recent"])

    def test_other_http_errors_do_not_start_cooldown(self):
        gate = RequestGate(clock=lambda: 0.0)

        def failure():
            raise http_error(503)

        with self.assertRaises(HTTPError):
            gate.run(failure)
        self.assertEqual(gate.run(lambda: "ok"), "ok")

    def test_regular_error_is_preserved_without_cooldown(self):
        gate = RequestGate()

        def failure():
            raise ValueError("bad input")

        with self.assertRaisesRegex(ValueError, "bad input"):
            gate.run(failure)
        self.assertEqual(gate.run(lambda: 1), 1)

    def test_network_calls_are_parallel_and_inflight_success_survives_429(self):
        gate = RequestGate(clock=lambda: 0.0)
        entered_count = [0]
        lock = threading.Lock()
        both_entered = threading.Event()
        release_429 = threading.Event()
        release_success = threading.Event()
        limited = threading.Event()
        results = []

        def operation(index):
            with lock:
                entered_count[0] += 1
                if entered_count[0] == 2:
                    both_entered.set()
            if index == 0:
                release_429.wait(1)
                raise http_error(429)
            release_success.wait(1)
            return "inflight success"

        def invoke(index):
            try:
                results.append(gate.run(operation, index))
            except HTTPError:
                limited.set()

        threads = [threading.Thread(target=invoke, args=(index,), daemon=True) for index in range(2)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(both_entered.wait(0.5), "The gate must not serialize HTTP calls")
            release_429.set()
            self.assertTrue(limited.wait(0.5))
            with self.assertRaisesRegex(RuntimeError, "限流"):
                gate.run(lambda: self.fail("A new network operation must not run"))
            release_success.set()
        finally:
            release_429.set()
            release_success.set()
            for thread in threads:
                thread.join(0.5)
        self.assertEqual(results, ["inflight success"])

    def test_later_inflight_429_extends_shared_cooldown(self):
        now = [0.0]
        gate = RequestGate(clock=lambda: now[0])
        entered = threading.Event()
        release = threading.Event()

        def delayed_error():
            entered.set()
            release.wait(1)
            raise http_error(429)

        def invoke():
            try:
                gate.run(delayed_error)
            except HTTPError:
                pass

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(0.5))

            def immediate_error():
                raise http_error(429)

            with self.assertRaises(HTTPError):
                gate.run(immediate_error)
            now[0] = 20.0
            release.set()
            worker.join(0.5)
            now[0] = 60.0
            with self.assertRaisesRegex(RuntimeError, "20 秒"):
                gate.run(lambda: "too early")
            now[0] = 80.0
            self.assertEqual(gate.run(lambda: "ok"), "ok")
        finally:
            release.set()
            worker.join(0.5)


if __name__ == "__main__":
    unittest.main()
