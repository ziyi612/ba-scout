"""Global request-start pacing with real concurrent callers, no real HTTP."""
from __future__ import annotations

import threading
import time
import unittest
from urllib.error import HTTPError

from ba_scout.request_gate import RequestGate


def wait_until(predicate, timeout=1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError('condition not reached')


class RequestPacingTests(unittest.TestCase):
    def test_shared_name_profile_history_starts_are_spaced_while_http_overlaps(self):
        interval = .04
        gate = RequestGate(min_interval=interval)
        calls, results = [], []
        lock, release = threading.Lock(), threading.Event()

        def operation(kind, timeout):
            with lock:
                calls.append((kind, time.monotonic(), timeout))
            release.wait(1)
            return kind

        def invoke(kind):
            results.append(gate.run(operation, kind, timeout=1))

        workers = [threading.Thread(target=invoke, args=(kind,), daemon=True)
                   for kind in ('name1', 'name2', 'profile1', 'history1', 'profile2', 'history2')]
        try:
            for worker in workers:
                worker.start()
            wait_until(lambda: len(calls) == 6)
            self.assertEqual(results, [], 'HTTP bodies must overlap without serializing until completion')
            starts = sorted(stamp for _, stamp, _ in calls)
            self.assertTrue(all(b-a >= interval-.003 for a,b in zip(starts, starts[1:])))
            self.assertTrue(all(0 < timeout <= 1 for _,_,timeout in calls))
            self.assertLess(calls[-1][2], .85, 'Admission waiting must consume the request budget')
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
        self.assertCountEqual(results, [kind for kind,_,_ in calls])

    def test_fifo_queue_prevents_later_callable_overtaking_earlier_waiters(self):
        gate, calls = RequestGate(min_interval=.025), []
        gate.run(lambda: calls.append('primer'))
        workers=[]
        for index in range(4):
            worker=threading.Thread(target=lambda i=index: gate.run(lambda: calls.append(i)),daemon=True)
            workers.append(worker)
            worker.start()
            wait_until(lambda i=index: len(gate._waiters) >= i+1)
        for worker in workers:
            worker.join(1)
        self.assertEqual(calls,['primer',0,1,2,3])
        self.assertFalse(gate._waiters)

    def test_429_response_rejects_already_waiting_calls_without_invoking_them(self):
        gate = RequestGate(min_interval=.3)
        entered, release = threading.Event(), threading.Event()
        calls, errors = [], []

        def first():
            calls.append('first')
            entered.set()
            release.wait(1)
            raise HTTPError('https://fixture.invalid/',429,'fixture',{},None)

        def invoke(operation):
            try:
                gate.run(operation)
            except Exception as exc:
                errors.append(exc)

        first_worker=threading.Thread(target=invoke,args=(first,),daemon=True)
        first_worker.start()
        self.assertTrue(entered.wait(.5))
        waiting=[threading.Thread(target=invoke,args=(lambda: calls.append('forbidden'),),daemon=True) for _ in range(3)]
        try:
            for worker in waiting:
                worker.start()
            wait_until(lambda: len(gate._waiters)==3)
            before=time.monotonic()
            release.set()
            for worker in [first_worker,*waiting]:
                worker.join(.5)
            self.assertLess(time.monotonic()-before,.3)
            self.assertEqual(calls,['first'])
            self.assertEqual(len(errors),4)
            self.assertEqual(sum(isinstance(e,HTTPError) for e in errors),1)
            self.assertTrue(all(isinstance(e,(HTTPError,RuntimeError)) for e in errors))
            self.assertGreater(gate._cooldown_until-time.monotonic(),59)
            self.assertFalse(gate._waiters)
        finally:
            release.set()
            for worker in [first_worker,*waiting]:
                worker.join(.5)

    def test_expired_waiter_never_issues_http_or_reserves_a_future_empty_slot(self):
        gate = RequestGate(min_interval=.1)
        starts=[]
        gate.run(lambda: starts.append(time.monotonic()))
        with self.assertRaises(TimeoutError):
            gate.run(lambda timeout: self.fail('Expired request must not issue HTTP'),timeout=.02)
        self.assertFalse(gate._waiters)
        values=[]
        gate.run(lambda timeout: (starts.append(time.monotonic()),values.append(timeout)),timeout=.3)
        self.assertGreaterEqual(starts[1]-starts[0],.097)
        self.assertLess(starts[1]-starts[0],.18)
        self.assertGreater(values[0],.18)
        self.assertLess(values[0],.25)

    def test_timeout_without_wait_is_forwarded_with_only_admission_elapsed_deducted(self):
        gate = RequestGate(min_interval=.1)
        values=[]
        result=gate.run(lambda item, timeout: (values.append(timeout),item)[1],'payload',timeout=.5)
        self.assertEqual(result,'payload')
        self.assertTrue(.45 < values[0] <= .5)

    def test_no_timeout_callback_has_bounded_twenty_second_admission_wait(self):
        clock=[0.0]
        gate=RequestGate(clock=lambda: clock[0],min_interval=30)
        gate.run(lambda: None)
        errors=[]
        def invoke():
            try:
                gate.run(lambda: self.fail('Unbounded queued request was invoked'))
            except Exception as exc:
                errors.append(exc)
        worker=threading.Thread(target=invoke,daemon=True)
        worker.start()
        wait_until(lambda: len(gate._waiters)==1)
        with gate._condition:
            clock[0]=20.001
            gate._condition.notify_all()
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],TimeoutError)
        self.assertFalse(gate._waiters)

    def test_default_zero_interval_still_allows_parallel_requests_and_exact_timeout(self):
        gate=RequestGate()
        calls=[]
        release=threading.Event()
        def operation(timeout):
            calls.append(timeout)
            release.wait(.5)
        workers=[threading.Thread(target=lambda: gate.run(operation,timeout=8),daemon=True) for _ in range(3)]
        try:
            for worker in workers:
                worker.start()
            wait_until(lambda:len(calls)==3,.3)
            self.assertEqual(calls,[8,8,8])
        finally:
            release.set()
            for worker in workers:
                worker.join(.5)

    def test_non_429_error_does_not_retry_or_drop_spacing(self):
        gate=RequestGate(min_interval=.03)
        calls=[]
        def error():
            calls.append(time.monotonic())
            raise HTTPError('https://fixture.invalid/',503,'fixture',{},None)
        with self.assertRaises(HTTPError):
            gate.run(error)
        gate.run(lambda:calls.append(time.monotonic()))
        self.assertEqual(len(calls),2)
        self.assertGreaterEqual(calls[1]-calls[0],.027)
        self.assertEqual(gate._cooldown_until,float('-inf'))

    def test_invalid_intervals_and_paced_timeouts_rejected(self):
        for value in (-1,float('inf'),float('nan'),True,'1'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                RequestGate(min_interval=value)
        gate=RequestGate(min_interval=.01)
        for value in (-1,0,float('inf'),float('nan'),True,'1'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                gate.run(lambda timeout:self.fail('Invalid timeout'),timeout=value)


if __name__=='__main__':
    unittest.main()
