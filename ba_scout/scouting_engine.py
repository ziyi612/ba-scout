"""One bounded match-page pipeline for latest-match and opening summaries.

Profiles submit up to five history IDs per opponent.  A fixed daemon pool
shares each match page across all players and prioritises newer histories.
Each player's deadline starts at admission and is never extended by its later
profile/history updates. Newly identified preparation rows can join the round.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from .settings import app_path

from .opening import aggregate_openings


_CACHE_SECONDS = 86400.0
_MAX_CACHE_BYTES = 8_388_608


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Timezone required")
    return stamp.timestamp()


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            number = int(value)
            return number if number > 0 else None
        except ValueError:
            pass
    return None


@dataclass
class _Round:
    scan_id: int
    player_ids: list[int]
    ends: float
    window_seconds: float
    history_limit: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    histories: dict[int, tuple[int, ...]] = field(default_factory=dict)
    unavailable_histories: dict[int, str] = field(default_factory=dict)
    revisions: dict[int, int] = field(default_factory=dict)
    pending: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    running: set[int] = field(default_factory=set)
    results: dict[int, tuple[dict[str, Any], bool]] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    recon_terminal: set[int] = field(default_factory=set)
    opening_terminal: set[int] = field(default_factory=set)
    finished: bool = False
    deadlines: dict[int, float] = field(default_factory=dict)
    expired: set[int] = field(default_factory=set)


class BattleRecon:
    """Fetch each match once, publishing both old ``recon`` and ``opening``.

    Repeating a history is a no-op.  Replacing it filters queued events for
    that player's previous history, while already fetched shared pages remain
    reusable in the round.  No network or disk operation runs in ``submit``
    or ``poll_events``.  ``close`` never waits for outstanding HTTP calls.
    """

    def __init__(self, cache_dir: str, fetcher: Callable[..., dict[str, Any]], concurrency: int = 2) -> None:
        resolved = app_path(cache_dir)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 2:
            raise ValueError("对局查询并发数必须为 1 或 2")
        resolved.mkdir(parents=True, exist_ok=True)
        self.cache_dir = resolved
        self.fetcher = fetcher
        self.concurrency = concurrency
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._jobs: list[tuple[int, int, int, int, int]] = []
        self._active: _Round | None = None
        self._counter = 0
        self._sequence = 0
        self._closed = False
        self._cooldown_until = 0.0
        self._workers = []
        for index in range(concurrency):
            worker = threading.Thread(target=self._work, name=f"ba-scout-battle-{index + 1}", daemon=True)
            worker.start()
            self._workers.append(worker)

    def start(self, player_ids: list[int], deadline: float = 19, window_seconds: float = 120, history_limit: int = 5) -> int:
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("deadline must be positive and finite")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be positive and finite")
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= 5:
            raise ValueError("history_limit must be between 1 and 5")
        expected = []
        for value in player_ids:
            player_id = _positive_id(value)
            if player_id is None:
                raise ValueError("玩家 ID 必须是正整数")
            if player_id not in expected:
                expected.append(player_id)
        with self._condition:
            if self._closed:
                raise RuntimeError("BattleRecon has been closed")
            if self._active is not None:
                self._active.cancelled.set()
            self._jobs.clear()
            self._counter += 1
            scan = _Round(self._counter, expected, time.monotonic() + float(deadline), float(window_seconds), history_limit)
            scan.deadlines = {player_id: scan.ends for player_id in expected}
            self._active = scan
            self._condition.notify_all()
            threading.Thread(target=self._watch_deadline, args=(scan,), name=f"ba-scout-battle-deadline-{scan.scan_id}", daemon=True).start()
            return scan.scan_id

    def update_players(self, player_ids: list[int], deadline: float = 19, *,
                       player_deadlines: dict[int, float] | None = None) -> int:
        """Apply preparation membership while preserving shared page work.

        Existing IDs keep their original absolute budget. Only newly discovered
        IDs get a fresh bounded deadline, optionally shortened by the caller's
        identity deadline. Removed IDs cannot publish late history events.
        """
        if isinstance(deadline, bool) or not math.isfinite(deadline) or not 0 < deadline <= 20:
            raise ValueError('deadline must be positive and at most 20 seconds')
        expected = []
        for value in player_ids:
            player = _positive_id(value)
            if player is None:
                raise ValueError('玩家 ID 必须是正整数')
            if player not in expected:
                expected.append(player)
        if player_deadlines is not None and (not isinstance(player_deadlines, dict) or any(
                type(pid) is not int or type(end) not in (int, float) or not math.isfinite(end)
                for pid, end in player_deadlines.items())):
            raise ValueError('玩家截止时间必须为 ID 到有限时间的映射')
        with self._condition:
            scan = self._active
            if self._closed:
                raise RuntimeError('BattleRecon has been closed')
            if scan is None or scan.cancelled.is_set():
                raise ValueError('增量名单必须属于当前未取消的对局')
            previous, current = set(scan.player_ids), set(expected)
            added, removed = current - previous, previous - current
            now = time.monotonic()
            for player in added:
                if player not in scan.deadlines:
                    end = now + float(deadline)
                    if player_deadlines is not None and player in player_deadlines:
                        end = min(end, player_deadlines[player])
                    scan.deadlines[player] = end
            scan.player_ids = expected
            scan.ends = max((scan.deadlines[pid] for pid in expected), default=now)
            if added or removed:
                scan.finished = False
                for player in removed:
                    scan.revisions[player] = scan.revisions.get(player, 0) + 1
                for player in added:
                    # Re-added IDs reuse their already fetched pages without
                    # extending their deadline or replaying stale queued events.
                    history = scan.histories.pop(player, None)
                    scan.recon_terminal.discard(player)
                    scan.opening_terminal.discard(player)
                    scan.expired.discard(player)
                    scan.revisions[player] = scan.revisions.get(player, 0) + 1
                    if history is not None and now < scan.deadlines[player]:
                        self.submit_history(player, list(history))
                self._condition.notify_all()
            return scan.scan_id

    def submit(self, player_id: int, match_id: int) -> None:
        """Compatibility helper for a single most recent match."""
        self.submit_history(player_id, [match_id])

    def submit_history(self, player_id: int, match_ids: list[int]) -> None:
        player = _positive_id(player_id)
        with self._condition:
            scan = self._active
            if scan is None or not self._player_live_locked(scan, player):
                return
            history = []
            if isinstance(match_ids, (list, tuple)):
                for value in match_ids:
                    match = _positive_id(value)
                    if match is not None and match not in history:
                        history.append(match)
                    if len(history) == scan.history_limit:
                        break
            ids = tuple(history)
            if scan.histories.get(player) == ids:
                return
            scan.unavailable_histories.pop(player, None)
            scan.histories[player] = ids
            scan.revisions[player] = scan.revisions.get(player, 0) + 1
            scan.recon_terminal.discard(player)
            scan.opening_terminal.discard(player)
            if not ids:
                self._emit_recon_locked(scan, player, None, "error", None, None, False, "没有可查询的最近对局编号")
                self._publish_opening_locked(scan, player)
                return
            first = ids[0]
            if first in scan.results:
                data, from_cache = scan.results[first]
                self._publish_recon_locked(scan, player, first, data, from_cache)
            elif first in scan.errors:
                self._emit_recon_locked(scan, player, first, "error", None, None, False, scan.errors[first])
            self._publish_opening_locked(scan, player)
            player_order = scan.player_ids.index(player)
            for depth, match_id in enumerate(ids):
                if match_id in scan.results or match_id in scan.errors or match_id in scan.running:
                    continue
                priority = (depth, player_order)
                previous = scan.pending.get(match_id)
                if previous is not None and previous[:2] <= priority:
                    continue
                self._sequence += 1
                ticket = (*priority, self._sequence)
                scan.pending[match_id] = ticket
                heapq.heappush(self._jobs, (*ticket, scan.scan_id, match_id))
            self._condition.notify_all()

    def mark_unavailable(self, player_id: int, reason: str) -> bool:
        """Finish an unknown history after its profile source has failed.

        Call only when the upstream profile is terminal, not while it is still
        loading. Existing submitted histories (including pending requests and
        explicit empty histories) are never replaced. A later valid history
        may still be submitted during this round and supersedes these events.
        No absent match ID or sample is invented; requested_count remains the
        configured history limit because the actual history is unknown.
        """
        player = _positive_id(player_id)
        message = reason if isinstance(reason, str) else "玩家资料未取得"
        message = message.replace("\r", " ").replace("\n", " ").strip()[:180] or "玩家资料未取得"
        with self._condition:
            scan = self._active
            if (scan is None or not self._player_live_locked(scan, player)
                    or player in scan.histories
                    or player in scan.unavailable_histories):
                return False
            scan.unavailable_histories[player] = message
            scan.revisions[player] = scan.revisions.get(player, 0) + 1
            self._emit_recon_locked(scan, player, None, "error", None, None, False,
                                    "未取得近期对局编号：" + message)
            self._publish_opening_locked(scan, player)
            return True

    def poll_events(self) -> list[dict[str, Any]]:
        result = []
        with self._lock:
            scan = self._active
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                revision = event.pop("_history_revision")
                if (not self._closed and scan is not None and event["scan_id"] == scan.scan_id
                        and event['player_id'] in scan.player_ids
                        and revision == scan.revisions.get(event["player_id"], 0)):
                    result.append(event)
        return result

    def cancel(self) -> None:
        with self._condition:
            if self._active is not None:
                self._active.cancelled.set()
                self._active = None
            self._jobs.clear()
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self.cancel()
            self._closed = True
            self._condition.notify_all()

    def _is_active_locked(self, scan: _Round) -> bool:
        return not self._closed and self._active is scan and not scan.cancelled.is_set() and not scan.finished

    def _player_live_locked(self, scan: _Round, player: int) -> bool:
        return (self._is_active_locked(scan) and player in scan.player_ids
                and time.monotonic() < scan.deadlines.get(player, scan.ends))

    def _match_live_locked(self, scan: _Round, match_id: int) -> bool:
        return self._is_active_locked(scan) and bool(self._subscribers(scan, match_id))

    @staticmethod
    def _subscribers(scan: _Round, match_id: int) -> list[int]:
        now = time.monotonic()
        return [player for player, history in scan.histories.items()
                if player in scan.player_ids and now < scan.deadlines.get(player, scan.ends) and match_id in history]

    def _next_job(self) -> tuple[_Round, int] | None:
        with self._condition:
            while not self._closed:
                while self._jobs:
                    depth, order, sequence, scan_id, match_id = heapq.heappop(self._jobs)
                    scan = self._active
                    if scan is None or scan.scan_id != scan_id or not self._is_active_locked(scan):
                        continue
                    if scan.pending.get(match_id) != (depth, order, sequence):
                        continue
                    del scan.pending[match_id]
                    if not self._subscribers(scan, match_id):
                        continue
                    scan.running.add(match_id)
                    return scan, match_id
                self._condition.wait()
            return None

    def _work(self) -> None:
        while True:
            job = self._next_job()
            if job is None:
                return
            scan, match_id = job
            cached = self._read_cache(match_id)
            if cached is not None:
                with self._lock:
                    if self._match_live_locked(scan, match_id):
                        self._publish_match_locked(scan, match_id, cached, True)
                    else:
                        scan.running.discard(match_id)
                continue
            with self._lock:
                subscribers = self._subscribers(scan, match_id)
                if not self._is_active_locked(scan) or not subscribers:
                    scan.running.discard(match_id)
                    continue
                remaining = max(scan.deadlines[player] for player in subscribers) - time.monotonic()
                if remaining <= 0:
                    scan.running.discard(match_id)
                    continue
                cooldown = self._cooldown_until - time.monotonic()
                if cooldown > 0:
                    self._publish_error_locked(scan, match_id, f"数据站请求限流，约 {math.ceil(cooldown)} 秒后可重试")
                    continue
            try:
                raw = self.fetcher(match_id, timeout=min(8.0, remaining))
                data = self._validate_result(raw, match_id)
                with self._lock:
                    if not self._match_live_locked(scan, match_id):
                        if self._active is scan and not self._closed and not scan.cancelled.is_set():
                            # ID-validated page may be reused privately by a
                            # later admitted player; detached rows see no event
                            # and the page is not written to disk here.
                            scan.results[match_id] = (data, False)
                        continue
                    useful = any(self._available_summary(data, player) is not None for player in self._subscribers(scan, match_id))
                if useful and type(data.get("recon_version")) is int and data["recon_version"] == 2:
                    self._write_cache(match_id, data, scan)
                with self._lock:
                    if self._match_live_locked(scan, match_id):
                        self._publish_match_locked(scan, match_id, data, False)
            except Exception as exc:
                message = str(exc).replace("\n", " ").replace("\r", " ")[:180] or type(exc).__name__
                with self._lock:
                    if isinstance(exc, HTTPError) and exc.code == 429:
                        self._cooldown_until = max(self._cooldown_until, time.monotonic() + 60.0)
                        message = "数据站请求限流，已暂停新查询至少 60 秒"
                    if self._match_live_locked(scan, match_id):
                        self._publish_error_locked(scan, match_id, "对局查询失败：" + message)
            finally:
                with self._condition:
                    scan.running.discard(match_id)
                    self._condition.notify_all()

    @staticmethod
    def _validate_result(raw: Any, match_id: int) -> dict[str, Any]:
        if not isinstance(raw, dict) or _positive_id(raw.get("match_id")) != match_id:
            raise ValueError("对局编号与请求不匹配")
        if not isinstance(raw.get("players"), dict):
            raise ValueError("对局摘要缺少玩家数据")
        data = dict(raw)
        data["match_id"] = match_id
        if not data.get("fetched_at"):
            data["fetched_at"] = _utc_now()
        else:
            _timestamp(data["fetched_at"])
        return data

    @staticmethod
    def _available_summary(data: dict[str, Any], player_id: int) -> dict[str, Any] | None:
        summary = data["players"].get(str(player_id))
        if not isinstance(summary, dict) or not summary or summary.get("available") is False:
            return None
        return summary

    def _publish_match_locked(self, scan: _Round, match_id: int, data: dict[str, Any], from_cache: bool) -> None:
        scan.running.discard(match_id)
        scan.results[match_id] = (data, from_cache)
        for player in self._subscribers(scan, match_id):
            if scan.histories[player][0] == match_id:
                self._publish_recon_locked(scan, player, match_id, data, from_cache)
            self._publish_opening_locked(scan, player)

    def _publish_error_locked(self, scan: _Round, match_id: int, message: str) -> None:
        scan.running.discard(match_id)
        scan.errors[match_id] = message
        for player in self._subscribers(scan, match_id):
            if scan.histories[player][0] == match_id:
                self._emit_recon_locked(scan, player, match_id, "error", None, None, False, message)
            self._publish_opening_locked(scan, player)

    def _publish_recon_locked(self, scan: _Round, player: int, match_id: int, data: dict[str, Any], from_cache: bool) -> None:
        if player in scan.recon_terminal:
            return
        summary = self._available_summary(data, player)
        if summary is not None:
            self._emit_recon_locked(scan, player, match_id, "ok", dict(summary), data, from_cache, "使用近期对局缓存" if from_cache else "近期对局摘要已取得")
        else:
            raw_summary = data["players"].get(str(player))
            reason = raw_summary.get("reason") or raw_summary.get("message") if isinstance(raw_summary, dict) else None
            message = "该对局未提供此玩家的有效摘要"
            if isinstance(reason, str) and reason.strip():
                message += "：" + reason[:150]
            self._emit_recon_locked(scan, player, match_id, "error", None, data, from_cache, message)

    def _emit_recon_locked(self, scan: _Round, player: int, match_id: int | None, status: str, summary: dict[str, Any] | None, envelope: dict[str, Any] | None, from_cache: bool, message: str) -> None:
        if player in scan.recon_terminal:
            return
        envelope = envelope or {}
        scan.recon_terminal.add(player)
        self._emit_locked(scan, player, {
            "type": "recon", "match_id": match_id, "status": status, "data": summary,
            "source_url": envelope.get("source_url"), "match_end_time": envelope.get("match_end_time"),
            "fetched_at": envelope.get("fetched_at"), "from_cache": from_cache, "message": message,
        })

    def _publish_opening_locked(self, scan: _Round, player: int, *, deadline: bool = False) -> None:
        if player in scan.opening_terminal:
            return
        history = scan.histories.get(player)
        unavailable = scan.unavailable_histories.get(player)
        requested = len(history) if history is not None else scan.history_limit
        ids = history or ()
        envelopes = []
        failures = []
        for match_id in ids:
            if match_id in scan.results:
                data, from_cache = scan.results[match_id]
                if self._available_summary(data, player) is None:
                    failures.append({"match_id": match_id, "reason": "该对局未提供此玩家的有效摘要", "source_url": data.get("source_url")})
                elif type(data.get("recon_version")) is int and data["recon_version"] == 2:
                    envelopes.append({**data, "from_cache": from_cache})
                else:
                    failures.append({"match_id": match_id, "reason": "旧版对局摘要未保留部署时间", "source_url": data.get("source_url")})
            elif match_id in scan.errors:
                failures.append({"match_id": match_id, "reason": scan.errors[match_id]})
            elif deadline:
                failures.append({"match_id": match_id, "reason": "本轮时间预算已截止"})
        completed = sum(mid in scan.results or mid in scan.errors for mid in ids)
        finished = deadline or unavailable is not None or (history is not None and completed == len(ids))
        windows = {}
        primary = None
        # Reuse these exact pages for both windows.  Completeness is assessed
        # separately, so a 30-second match can enter 20s but not 120s samples.
        for window in dict.fromkeys((20.0, scan.window_seconds)):
            aggregate = aggregate_openings(player, envelopes, requested_match_ids=ids, window_seconds=window)
            if history is None:
                aggregate["requested_count"] = requested
            by_match = {_positive_id(item.get("match_id")): item for item in aggregate.get("unusable_matches", [])}
            for failure in failures:
                by_match[_positive_id(failure["match_id"])] = dict(failure)
            aggregate["unusable_matches"] = list(by_match.values())
            count = aggregate["sample_count"]
            if finished and requested > 0 and count == requested:
                status = "ok"
            elif count > 0:
                status = "partial"
            elif finished:
                status = "timeout" if deadline else "error"
            else:
                status = "pending"
            message = f"已取得 {count}/{requested} 场有效开局样本"
            if deadline:
                message += "；本轮时间预算已截止"
            elif not finished:
                message += "；仍在查询"
            elif count < requested:
                message += "；缺失或无有效部署时间的场次未计入统计"
            if history == ():
                message = "没有可查询的最近对局编号"
            elif unavailable is not None:
                message = "未取得近期对局编号：" + unavailable
            state = {"status": status, "data": aggregate, "finished": finished, "message": message}
            key = str(int(window)) if window.is_integer() else str(window)
            windows[key] = state
            if window == scan.window_seconds:
                primary = state
        if finished:
            scan.opening_terminal.add(player)
        self._emit_locked(scan, player, {"type": "opening", **primary, "windows": windows})

    def _emit_locked(self, scan: _Round, player: int, event: dict[str, Any]) -> None:
        if self._closed or self._active is not scan or player not in scan.player_ids:
            return
        self._events.put({**event, "scan_id": scan.scan_id, "player_id": player, "_history_revision": scan.revisions.get(player, 0)})

    def _watch_deadline(self, scan: _Round) -> None:
        with self._condition:
            while not self._closed and self._active is scan and not scan.cancelled.is_set():
                now = time.monotonic()
                for player in set(scan.player_ids) - scan.expired:
                    if now < scan.deadlines[player]:
                        continue
                    if player not in scan.recon_terminal:
                        ids = scan.histories.get(player, ())
                        match_id = ids[0] if ids else None
                        self._emit_recon_locked(scan, player, match_id, 'timeout', None, None, False,
                                                '本轮已截止，暂无近期对局摘要')
                    self._publish_opening_locked(scan, player, deadline=True)
                    scan.expired.add(player)
                live = set(scan.player_ids) - scan.expired
                scan.finished = not live
                delay = max(0.0, min(scan.deadlines[pid] for pid in live) - now) if live else None
                self._condition.wait(delay)

    def _read_cache(self, match_id: int) -> dict[str, Any] | None:
        path = self.cache_dir / f"{match_id}.json"
        try:
            if path.stat().st_size > _MAX_CACHE_BYTES:
                return None
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(wrapper, dict) or wrapper.get("schema") != 2 or _positive_id(wrapper.get("match_id")) != match_id:
                return None
            age = time.time() - _timestamp(wrapper.get("stored_at"))
            if age < -300 or age > _CACHE_SECONDS:
                return None
            data = self._validate_result(wrapper.get("data"), match_id)
            return data if type(data.get("recon_version")) is int and data["recon_version"] == 2 else None
        except (OSError, ValueError, TypeError, OverflowError):
            return None

    def _write_cache(self, match_id: int, data: dict[str, Any], scan: _Round) -> None:
        target = self.cache_dir / f"{match_id}.json"
        temporary = self.cache_dir / f".{match_id}.{uuid.uuid4().hex}.tmp"
        try:
            wrapper = {"schema": 2, "match_id": match_id, "stored_at": _utc_now(), "data": data}
            serialized = json.dumps(wrapper, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > _MAX_CACHE_BYTES:
                return
            temporary.write_text(serialized, encoding="utf-8")
            with self._lock:
                if self._match_live_locked(scan, match_id):
                    os.replace(temporary, target)
        except (OSError, ValueError, TypeError, OverflowError):
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
