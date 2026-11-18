"""Shared request pacing and HTTP 429 cooldown across lookup controllers."""

from __future__ import annotations

import math
from collections import deque
import threading
import time
from typing import Callable, TypeVar
from urllib.error import HTTPError


_Result = TypeVar("_Result")


class RequestCooldown(RuntimeError):
    """A previous HTTP 429 is still blocking admission to the shared gate."""


class RequestGate:
    """Space request starts while allowing admitted network calls to overlap.

    Calls accepted before a 429 may finish normally.  Later calls fail fast for
    at least 60 seconds, without invoking their callable. Name/profile lookups
    precede queued match pages, with FIFO order within each class. A 429 also rejects callers
    already waiting there. The gate never retries a request. A zero interval
    preserves the original immediate parallel-admission behavior.

    When pacing is enabled, a timeout keyword also bounds admission waiting;
    its elapsed wait is deducted before invoking the operation. Name callbacks
    with their own preflight deadline receive at most 20 seconds of admission
    waiting and must still check cancellation before issuing HTTP.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, *, min_interval: float = 0) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(min_interval, bool) or not isinstance(min_interval, (int, float)) or not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError('min_interval must be finite and nonnegative')
        self._clock = clock
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._cooldown_until = float("-inf")
        self.min_interval = float(min_interval)
        self._last_admitted = float('-inf')
        self._waiters = deque()

    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def _check_cooldown_locked(self, now: float) -> None:
        remaining = self._cooldown_until - now
        if remaining > 0:
            raise RequestCooldown(f"数据站请求限流，请等待约 {math.ceil(remaining)} 秒后重试")

    def _admit(self, kwargs: dict, background: bool) -> dict:
        if self.min_interval == 0:
            with self._lock:
                self._check_cooldown_locked(self._clock())
            return kwargs
        started = self._clock()
        timeout = kwargs.get('timeout', 20.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be positive and finite')
        ends = started + float(timeout)
        ticket = (background, object())
        with self._condition:
            self._check_cooldown_locked(self._clock())
            if background:
                self._waiters.append(ticket)
            else:
                first_background = next((i for i, queued in enumerate(self._waiters) if queued[0]), len(self._waiters))
                self._waiters.insert(first_background, ticket)
            try:
                while True:
                    now = self._clock()
                    self._check_cooldown_locked(now)
                    remaining = ends - now
                    if remaining <= 0:
                        raise TimeoutError('等待请求间隔时已到截止时间，未发出网络请求')
                    delay = self._last_admitted + self.min_interval - now
                    if self._waiters[0] is ticket and delay <= 0:
                        # Reserve only at actual admission, never one future
                        # slot per waiter. Expired/cancelled queued work cannot
                        # leave an unbounded chain of empty reservations.
                        self._last_admitted = now
                        adjusted = dict(kwargs)
                        if 'timeout' in adjusted:
                            adjusted['timeout'] = remaining
                        return adjusted
                    self._condition.wait(min(remaining, max(.001, delay) if self._waiters[0] is ticket else .1))
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    def run(self, operation: Callable[..., _Result], *args, background: bool = False, **kwargs) -> _Result:
        kwargs = self._admit(kwargs, background)
        # Check once more immediately before the callable: an in-flight HTTP
        # response may have set the shared cooldown during admission handoff.
        with self._lock:
            self._check_cooldown_locked(self._clock())
        # Deliberately outside the lock: core and recent-match calls retain
        # their own concurrency budgets and already-started calls can finish.
        try:
            return operation(*args, **kwargs)
        except HTTPError as exc:
            if exc.code == 429:
                with self._condition:
                    self._cooldown_until = max(self._cooldown_until, self._clock() + 60.0)
                    self._condition.notify_all()
            raise
