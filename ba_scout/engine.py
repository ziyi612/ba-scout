"""Bounded, cancellable player lookups and an application-local JSON cache.

No GUI code lives here.  ``poll_events`` is safe to call from a Tk ``after``
callback: it never waits for HTTP or disk I/O.  A fixed daemon worker pool is
shared by all scans, including cancelled ones, so a slow previous scan cannot
increase the number of simultaneous network requests.
"""

from __future__ import annotations

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
from .provider import profile_history, ranked_counts_available
from .settings import app_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("Invalid cache timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Cache timestamps must include a timezone")
    return stamp.timestamp()


def _fetched_profile(raw: Any, player_id: int) -> tuple[dict[str, Any], bool]:
    if (not isinstance(raw, dict) or type(raw.get('player_id')) is not int
            or raw['player_id'] != player_id):
        raise ValueError('战绩页面的玩家 ID 不匹配')
    incomplete = raw.get('core_ready') is False or raw.get('profile_incomplete') is True
    if incomplete and raw.get('profile_incomplete') is not True:
        raise ValueError('战绩页面仅取得身份信息，核心统计尚未完整取得')
    data = dict(raw)
    data['fetched_at'] = raw.get('fetched_at') or _utc_now()
    data['from_cache'] = False
    return data, incomplete


def _partial_message(data: dict, has_old: bool) -> str:
    missing = '双专精经验暂缺' if data.get('core_ready') else '累计战绩暂缺'
    return ('已取得部分资料，' + missing
            + ('；缺失字段可查看旧缓存及其原始时间' if has_old else ''))


def _preserve_history(data: dict, old: dict | None) -> None:
    if old is None or profile_history(data) is not None:
        return
    ids = profile_history(old)
    if ids is not None:
        data.update(recent_match_ids=ids, last_match_id=ids[0] if ids else None,
                    recent_history_available=True, history_from_cache=True,
                    history_fetched_at=old.get('history_fetched_at') or old.get('fetched_at'))


@dataclass
class _Scan:
    scan_id: int
    fight_id: str
    players: list[dict[str, Any]]
    self_team: str | None
    started: float
    ends: float
    force: bool
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    terminal: dict[int, str] = field(default_factory=dict)
    stale: dict[int, dict[str, Any]] = field(default_factory=dict)
    finished: bool = False
    members: set[int] = field(default_factory=set)
    deadlines: dict[int, float] = field(default_factory=dict)
    scheduled: set[int] = field(default_factory=set)
    responses: dict[int, dict[str, Any]] = field(default_factory=dict)
    progress: dict[int, dict[str, Any]] = field(default_factory=dict)
    wake: threading.Event = field(default_factory=threading.Event)
    revision: int = 0


@dataclass
class _PrefetchRecord:
    phase: str = 'queued'
    status: str | None = None
    data: dict[str, Any] | None = None
    stale: dict[str, Any] | None = None
    message: str = ''
    claimed_by: int | None = None
    progress: dict | None = None
    admitted: bool = False


@dataclass
class _PrefetchScope:
    token: int
    scope_id: str
    ends: float
    records: dict[int, _PrefetchRecord] = field(default_factory=dict)
    queued: list[int] = field(default_factory=list)
    cancelled: bool = False
    adopted: _Scan | None = None
    allowed_ids: set[int] = field(default_factory=set)


class ScanController:
    """Emit ``started``, ``player``, and ``finished`` dictionaries per scan.

    ``complete`` in the finished event counts successful fresh or cached
    lookups, not stale fallbacks.  A failed or timed-out row may still carry
    its old cache in ``data``; ``data['from_cache']`` remains true in that case.
    ``partial`` carries confirmed basic data without counting as complete;
    ``partial_data`` holds its fresh fields separately from any old full cache.
    ``cancel`` discards this scan's queued events.  ``close`` returns promptly
    even when a fetcher is slow; outstanding workers are daemon threads.
    """

    def __init__(
        self,
        cache_dir: str,
        fetcher: Callable[..., dict[str, Any]],
        deadline: float = 20.0,
        concurrency: int = 5,
        ttl: float = 600.0,
        min_profile_version: int = 1,
        *,
        stream_progress: bool = False,
    ) -> None:
        resolved = app_path(cache_dir)
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("deadline must be positive and finite")
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 10:
            raise ValueError("concurrency must be between 1 and 10")
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("ttl must be nonnegative and finite")
        if not isinstance(min_profile_version, int) or min_profile_version < 1:
            raise ValueError("min_profile_version must be a positive integer")
        resolved.mkdir(parents=True, exist_ok=True)
        self.cache_dir = resolved
        self.fetcher = fetcher
        self.stream_progress = stream_progress
        self.deadline = float(deadline)
        self.concurrency = concurrency
        self.ttl = float(ttl)
        self.min_profile_version = min_profile_version
        self._lock = threading.RLock()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._jobs: queue.Queue[tuple[_Scan, int] | None] = queue.Queue()
        self._active: _Scan | None = None
        self._counter = 0
        self._prefetch_counter = 0
        self._prefetch: _PrefetchScope | None = None
        self._prefetch_slots = 0
        self._closed = False
        self._cooldown_until = 0.0
        self._workers: list[threading.Thread] = []
        for index in range(concurrency):
            worker = threading.Thread(
                target=self._work,
                name=f"ba-scout-http-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def begin_prefetch(self, scope_id: str, deadline_at: float) -> int:
        """Begin a private, absolute-deadline scope for one fight ID.

        At most two prefetch jobs share the existing HTTP pool. No public
        player event or disk-cache write happens until start() adopts the
        token and a duplicate-checked roster includes that player ID.
        """
        now = time.monotonic()
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError('预取必须绑定本轮对局 ID')
        if isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float)) or not math.isfinite(deadline_at) or not 0 < deadline_at - now <= 20.0:
            raise ValueError('预取截止时间必须在未来 20 秒内')
        with self._lock:
            if self._closed:
                raise RuntimeError('ScanController has been closed')
            self._cancel_prefetch_locked()
            self._prefetch_counter += 1
            self._prefetch = _PrefetchScope(self._prefetch_counter, scope_id.strip(), float(deadline_at))
            return self._prefetch_counter

    def prefetch(self, token: int, player_id: int) -> bool:
        """Queue one already-resolved row, without claiming final identity."""
        if type(player_id) is not int or player_id <= 0:
            raise ValueError('预取玩家 ID 必须是正整数')
        with self._lock:
            scope = self._prefetch
            if scope is None or type(token) is not int or scope.token != token or not self._prefetch_live_locked(scope) or scope.adopted is not None:
                return False
            if player_id in scope.records:
                return True
            if len(scope.records) >= 5:
                return False
            scope.records[player_id] = _PrefetchRecord()
            scope.queued.append(player_id)
            self._schedule_prefetch_locked()
            return True

    def start(self, roster: Any, self_id: int, force: bool = False, *, deadline_seconds: float | None = None,
              prefetch_token: int | None = None) -> int:
        """Start a scan without waiting for network or cache reads."""
        budget = self.deadline if deadline_seconds is None else float(deadline_seconds)
        if not math.isfinite(budget) or budget <= 0 or budget > self.deadline:
            raise ValueError("本轮预算必须大于0且不能超过配置预算")
        players = []
        seen: set[int] = set()
        for item in roster.players:
            player_id = int(item["id"])
            if player_id <= 0 or player_id in seen:
                raise ValueError("玩家名单必须包含唯一的正整数 ID")
            seen.add(player_id)
            players.append(
                {"id": player_id, "name": str(item["name"]), "team": str(item["team"])}
            )
        if not players:
            raise ValueError("玩家名单为空")
        self_team = next((p["team"] for p in players if p["id"] == self_id), getattr(roster, 'self_team', None))
        if self_team is not None:
            players.sort(
                key=lambda p: (p["team"] == self_team, p["id"] == self_id)
            )
        with self._lock:
            if self._closed:
                raise RuntimeError("ScanController has been closed")
            if self._active is not None:
                self._active.cancelled.set()
                self._active.done.set()
                self._active.wake.set()
            self._counter += 1
            started = time.monotonic()
            scope = self._prefetch
            adopt = (not force and scope is not None and type(prefetch_token) is int
                     and scope.token == prefetch_token and scope.scope_id == str(roster.fight_id)
                     and scope.adopted is None and not scope.cancelled)
            if not adopt:
                self._cancel_prefetch_locked()
            scan = _Scan(
                scan_id=self._counter,
                fight_id=str(roster.fight_id),
                players=players,
                self_team=self_team,
                started=started,
                ends=min(started + budget, scope.ends) if adopt else started + budget,
                force=bool(force),
            )
            self._active = scan
            scan.members = seen
            scan.deadlines = {player_id: scan.ends for player_id in seen}
            if adopt:
                scope.adopted = scan
                scope.ends = scan.ends
                scope.allowed_ids = seen
                # Jobs not yet dispatched have never issued HTTP. Let the
                # normal scan schedule them at its usual concurrency instead.
                for player_id in scope.queued:
                    scope.records.pop(player_id, None)
                scope.queued.clear()
            self._emit_locked(
                scan,
                {
                    "type": "started",
                    "fight_id": scan.fight_id,
                    "players": [dict(p) for p in players],
                    "self_team": self_team,
                    "started_at": _utc_now(),
                },
            )
            # The deadline thread is separate from local cache I/O.
            threading.Thread(
                target=self._watch_deadline,
                args=(scan,),
                name=f"ba-scout-deadline-{scan.scan_id}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._prepare,
                args=(scan,),
                name=f"ba-scout-cache-{scan.scan_id}",
                daemon=True,
            ).start()
            return scan.scan_id

    def update_roster(self, roster: Any, self_id: int, *, deadline_seconds: float = 19,
                      player_deadlines: dict[int, float] | None = None) -> int:
        """Apply a complete preparation roster without restarting retained IDs.

        Only newly admitted IDs receive a budget.  Repeated snapshots never
        extend an existing player's deadline or issue its profile request twice.
        Removing an ID filters queued events and prevents late cache writes.
        A finished round remains available for late names until cancel/start.
        """
        budget = float(deadline_seconds)
        if isinstance(deadline_seconds, bool) or not math.isfinite(budget) or not 0 < budget <= self.deadline:
            raise ValueError('本轮预算必须大于0且不能超过配置预算')
        players, seen = [], set()
        for item in roster.players:
            if isinstance(item.get('id'), bool):
                raise ValueError('玩家名单必须包含唯一的正整数 ID')
            player_id = int(item['id'])
            if player_id <= 0 or player_id in seen:
                raise ValueError('玩家名单必须包含唯一的正整数 ID')
            seen.add(player_id)
            players.append({'id': player_id, 'name': str(item['name']), 'team': str(item['team'])})
        if player_deadlines is not None and (not isinstance(player_deadlines, dict) or any(
                type(pid) is not int or type(end) not in (int, float) or not math.isfinite(end)
                for pid, end in player_deadlines.items())):
            raise ValueError('玩家截止时间必须为 ID 到有限时间的映射')
        self_team = next((p['team'] for p in players if p['id'] == self_id), getattr(roster, 'self_team', None))
        if self_team is not None:
            players.sort(key=lambda p: (p['team'] == self_team, p['id'] == self_id))
        with self._lock:
            scan = self._active
            if self._closed:
                raise RuntimeError('ScanController has been closed')
            if scan is None or scan.cancelled.is_set() or str(roster.fight_id) != scan.fight_id:
                raise ValueError('增量名单必须属于当前未取消的对局')
            previous = scan.members
            added = seen - previous
            changed = seen != previous
            now = time.monotonic()
            new = added - scan.deadlines.keys()
            for player_id in new:
                end = now + budget
                if player_deadlines is not None and player_id in player_deadlines:
                    end = min(end, player_deadlines[player_id])
                scan.deadlines[player_id] = end
            scan.players, scan.members, scan.self_team = players, seen, self_team
            if changed:
                scan.revision += 1
                scan.finished = False
                scan.done.clear()
                scan.ends = max((scan.deadlines[pid] for pid in seen), default=now)
                for player_id in added:
                    if player_id in scan.responses:
                        self._emit_locked(scan, scan.responses[player_id])
                self._emit_locked(scan, {'type': 'updated', 'fight_id': scan.fight_id,
                                        'players': [dict(p) for p in players], 'revision': scan.revision})
                scan.wake.set()
            if added:
                threading.Thread(target=self._prepare, args=(scan, [p for p in players if p['id'] in added]),
                                 name=f'ba-scout-cache-{scan.scan_id}-update', daemon=True).start()
            return scan.scan_id

    def poll_events(self) -> list[dict[str, Any]]:
        """Return current-scan events, dropping late/cancelled generations."""
        result = []
        with self._lock:
            current_id = None if self._active is None else self._active.scan_id
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                if (not self._closed and event["scan_id"] == current_id
                        and (event.get('player_id') is None or event['player_id'] in self._active.members)
                        and (event['type'] != 'finished' or event.get('revision', 0) == self._active.revision)):
                    result.append(event)
        return result

    def cancel(self) -> None:
        with self._lock:
            self._cancel_prefetch_locked()
            if self._active is not None:
                self._active.cancelled.set()
                self._active.done.set()
                self._active.wake.set()
                self._active = None

    def _cancel_prefetch_locked(self) -> None:
        if self._prefetch is not None:
            self._prefetch.cancelled = True
            self._prefetch.queued.clear()
            self._prefetch = None

    def _prefetch_live_locked(self, scope: _PrefetchScope) -> bool:
        return (not self._closed and self._prefetch is scope and not scope.cancelled
                and time.monotonic() < scope.ends
                and (scope.adopted is None or self._is_active_locked(scope.adopted)))

    def _schedule_prefetch_locked(self) -> None:
        scope = self._prefetch
        if scope is None or not self._prefetch_live_locked(scope) or scope.adopted is not None:
            return
        while scope.queued and self._prefetch_slots < min(2, self.concurrency):
            player_id = scope.queued.pop(0)
            scope.records[player_id].phase = 'dispatched'
            self._prefetch_slots += 1
            self._jobs.put((scope, player_id))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.cancel()
            self._closed = True
            for _ in self._workers:
                self._jobs.put(None)

    def _is_active_locked(self, scan: _Scan) -> bool:
        return (
            not self._closed
            and self._active is scan
            and not scan.cancelled.is_set()
            and not scan.finished
        )

    def _emit_locked(self, scan: _Scan, event: dict[str, Any]) -> None:
        if (not self._closed and self._active is scan and
                (event.get('player_id') is None or event['player_id'] in scan.members)):
            self._events.put({**event, "scan_id": scan.scan_id})

    def _player_live_locked(self, scan: _Scan, player_id: int) -> bool:
        return (self._is_active_locked(scan) and player_id in scan.members
                and player_id not in scan.terminal
                and time.monotonic() < scan.deadlines.get(player_id, scan.ends))

    def _retain_detached_locked(self, scan: _Scan, player_id: int, status: str,
                                data: dict | None, message: str,
                                *, partial_data: dict | None = None) -> None:
        # A temporary roster revocation must not leak this result to UI/disk.
        # Keep it privately for a possible same-ID re-admission, without a
        # second request or a renewed deadline. A cancelled generation loses it.
        if (self._closed or self._active is not scan or scan.cancelled.is_set()
                or player_id in scan.members or player_id in scan.terminal
                or time.monotonic() >= scan.deadlines.get(player_id, 0)):
            return
        scan.terminal[player_id] = status
        scan.responses[player_id] = {'type': 'player', 'player_id': player_id,
                                    'status': status, 'data': data, 'message': message}
        if partial_data is not None:
            scan.responses[player_id]['partial_data'] = partial_data

    def _prepare(self, scan: _Scan, players: list[dict] | None = None) -> None:
        for player in list(scan.players) if players is None else players:
            player_id = player["id"]
            with self._lock:
                if not self._player_live_locked(scan, player_id) or player_id in scan.scheduled:
                    continue
                scan.scheduled.add(player_id)
            cached = self._read_cache(player_id)
            delivery = None
            with self._lock:
                if not self._player_live_locked(scan, player_id):
                    scan.scheduled.discard(player_id)
                    continue
                if cached is not None:
                    data, age = cached
                    if age <= self.ttl and not scan.force:
                        self._player_locked(scan, player_id, "cached", data, "使用有效缓存")
                        continue
                    scan.stale[player_id] = data
                    self._emit_locked(
                        scan,
                        {
                            "type": "player",
                            "player_id": player_id,
                            "status": "stale",
                            "data": data,
                            "message": "显示历史缓存，正在刷新",
                        },
                    )
                scope = self._prefetch
                if (scope is not None and scope.adopted is scan and player_id in scope.records
                        and player_id in scope.allowed_ids and self._prefetch_live_locked(scope)):
                    # A completed result is consumed once; an in-flight one
                    # publishes to this scan when done. Neither issues HTTP twice.
                    record = scope.records[player_id]
                    record.admitted = True
                    if record.progress is not None:
                        self._profile_progress_locked(scan, player_id, record.progress)
                    delivery = self._claim_prefetch_locked(scope, player_id)
                else:
                    self._jobs.put((scan, player_id))
            if delivery is not None:
                self._deliver_prefetched(*delivery)

    def _claim_prefetch_locked(self, scope: _PrefetchScope, player_id: int):
        record = scope.records.get(player_id)
        if (not self._prefetch_live_locked(scope) or scope.adopted is None
                or player_id not in scope.allowed_ids or record is None
                or record.phase != 'completed' or record.claimed_by is not None):
            return None
        record.claimed_by = scope.adopted.scan_id
        return scope.adopted, player_id, record

    def _deliver_prefetched(self, scan: _Scan, player_id: int, record: _PrefetchRecord) -> None:
        if record.status == 'ok':
            self._write_cache(player_id, record.data, scan)
        with self._lock:
            if self._player_live_locked(scan, player_id):
                if (record.status in {'error', 'partial'} and record.stale is not None
                        and player_id not in scan.stale and player_id not in scan.terminal):
                    # Completion can race _prepare's cache read. Preserve its
                    # stale-first contract so history IDs remain available to
                    # the match pipeline even when the refresh has failed.
                    scan.stale[player_id] = record.stale
                    self._emit_locked(scan, {'type': 'player', 'player_id': player_id,
                                             'status': 'stale', 'data': record.stale,
                                             'message': '显示历史缓存，预取刷新未成功'})
                if record.status == 'partial':
                    old = scan.stale.get(player_id, record.stale)
                    self._player_locked(scan, player_id, 'partial', old or record.data,
                                        _partial_message(record.data, old is not None), partial_data=record.data)
                else:
                    data = record.data if record.status in {'ok', 'cached'} else scan.stale.get(player_id, record.stale)
                    self._player_locked(scan, player_id, record.status, data, record.message)

    def _profile_progress_locked(self, scan: _Scan, player_id: int, data: dict) -> None:
        if self._player_live_locked(scan, player_id):
            scan.progress[player_id] = data
            self._emit_locked(scan, {'type': 'player_progress', 'player_id': player_id,
                                     'status': 'loading', 'data': scan.stale.get(player_id) or data,
                                     'partial_data': data, 'message': '已取得部分字段，正在补充本次玩家资料'})

    def _fetch_profile(self, owner: _Scan | _PrefetchScope, player_id: int, remaining: float):
        timeout = min(12.0, remaining)
        if not self.stream_progress:
            return self.fetcher(player_id, timeout=timeout)

        def publish(data):
            if not isinstance(data, dict) or type(data.get('player_id')) is not int or data['player_id'] != player_id:
                raise ValueError('流式玩家资料 ID 不匹配')
            with self._lock:
                if isinstance(owner, _PrefetchScope):
                    if not self._prefetch_live_locked(owner) or player_id not in owner.records:
                        return
                    record = owner.records[player_id]
                    record.progress = data
                    if not record.admitted:
                        return
                    self._profile_progress_locked(owner.adopted, player_id, data)
                else:
                    self._profile_progress_locked(owner, player_id, data)

        return self.fetcher(player_id, timeout=timeout, on_progress=publish)

    def _work_prefetch(self, scope: _PrefetchScope, player_id: int) -> None:
        """Borrow one existing worker; uncommitted results stay only in RAM."""
        try:
            with self._lock:
                if not self._prefetch_live_locked(scope):
                    return
                if scope.adopted is not None and player_id not in scope.allowed_ids:
                    return
                record = scope.records[player_id]
                record.phase = 'running'
            status, data, message = 'error', None, ''
            try:
                cached = self._read_cache(player_id)
                if cached is not None:
                    old, age = cached
                    if age <= self.ttl:
                        status, data, message = 'cached', old, '使用有效缓存'
                    else:
                        record.stale = old
                if status != 'cached':
                    with self._lock:
                        if not self._prefetch_live_locked(scope):
                            return
                        remaining = scope.ends - time.monotonic()
                        cooldown = self._cooldown_until - time.monotonic()
                        if cooldown > 0:
                            raise RuntimeError(f'数据站请求限流，约 {math.ceil(cooldown)} 秒后可重试')
                    raw = self._fetch_profile(scope, player_id, remaining)
                    data, incomplete = _fetched_profile(raw, player_id)
                    _preserve_history(data, record.stale)
                    status, message = (('partial', _partial_message(data, record.stale is not None)) if incomplete
                                       else ('ok', '查询成功（已复用准备阶段预取）'))
            except Exception as exc:
                error = str(exc).replace('\r', ' ').replace('\n', ' ')[:180] or type(exc).__name__
                with self._lock:
                    if isinstance(exc, HTTPError) and exc.code == 429:
                        self._cooldown_until = max(self._cooldown_until, time.monotonic() + 60.0)
                        error = '数据站请求限流，已暂停新查询至少 60 秒'
                status = 'error'
                data = None
                message = ('刷新失败，保留历史缓存：' if record.stale else '查询失败：') + error
            with self._lock:
                if not self._prefetch_live_locked(scope):
                    return
                record.phase, record.status, record.data, record.message = 'completed', status, data, message
                delivery = self._claim_prefetch_locked(scope, player_id)
            if delivery is not None:
                self._deliver_prefetched(*delivery)
        finally:
            with self._lock:
                self._prefetch_slots -= 1
                self._schedule_prefetch_locked()

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            scan, player_id = job
            if isinstance(scan, _PrefetchScope):
                self._work_prefetch(scan, player_id)
                continue
            with self._lock:
                if not self._player_live_locked(scan, player_id):
                    scan.scheduled.discard(player_id)
                    continue
                remaining = scan.deadlines[player_id] - time.monotonic()
                if remaining <= 0:
                    continue
                cooldown = self._cooldown_until - time.monotonic()
                if cooldown > 0:
                    old = scan.stale.get(player_id)
                    message = f"数据站请求限流，约 {math.ceil(cooldown)} 秒后可重试"
                    if old:
                        message += "；保留历史缓存"
                    self._player_locked(scan, player_id, "error", old, message)
                    continue
            try:
                raw = self._fetch_profile(scan, player_id, remaining)
                data, incomplete = _fetched_profile(raw, player_id)
                _preserve_history(data, scan.stale.get(player_id))
                if incomplete:
                    with self._lock:
                        old = scan.stale.get(player_id)
                        message = _partial_message(data, old is not None)
                        if self._player_live_locked(scan, player_id):
                            self._player_locked(scan, player_id, 'partial', old or data,
                                                message, partial_data=data)
                        else:
                            self._retain_detached_locked(scan, player_id, 'partial', old or data,
                                                         message, partial_data=data)
                    continue
                with self._lock:
                    if not self._player_live_locked(scan, player_id):
                        self._retain_detached_locked(scan, player_id, 'ok', data, '查询成功')
                        continue
                self._write_cache(player_id, data, scan)
                with self._lock:
                    if self._player_live_locked(scan, player_id):
                        self._player_locked(scan, player_id, "ok", data, "查询成功")
                    else:
                        self._retain_detached_locked(scan, player_id, 'ok', data, '查询成功')
            except Exception as exc:
                message = str(exc).replace("\r", " ").replace("\n", " ")[:180]
                if not message:
                    message = type(exc).__name__
                with self._lock:
                    if isinstance(exc, HTTPError) and exc.code == 429:
                        # An in-flight response from an older round still
                        # applies to the shared service.  Do not dispatch
                        # queued requests or later scans for at least 60 s.
                        self._cooldown_until = max(
                            self._cooldown_until, time.monotonic() + 60.0
                        )
                        message = "数据站请求限流，已暂停新查询至少 60 秒"
                    if self._player_live_locked(scan, player_id):
                        old = scan.stale.get(player_id)
                        prefix = "刷新失败，保留历史缓存：" if old else "查询失败："
                        self._player_locked(scan, player_id, "error", old, prefix + message)
                    else:
                        self._retain_detached_locked(scan, player_id, 'error', scan.stale.get(player_id), '查询失败：' + message)

    def _player_locked(
        self,
        scan: _Scan,
        player_id: int,
        status: str,
        data: dict[str, Any] | None,
        message: str,
        *,
        partial_data: dict[str, Any] | None = None,
    ) -> None:
        if player_id in scan.terminal or player_id not in scan.members:
            return
        scan.terminal[player_id] = status
        progress = scan.progress.pop(player_id, None)
        if status in {'error', 'timeout'} and progress is not None:
            partial_data = progress
            data = data or progress
        response = {
                "type": "player",
                "player_id": player_id,
                "status": status,
                "data": data,
                "message": message,
            }
        if partial_data is not None:
            response['partial_data'] = partial_data
        scan.responses[player_id] = response
        self._emit_locked(scan, response)
        scan.wake.set()
        if scan.members <= scan.terminal.keys():
            scan.done.set()

    def _watch_deadline(self, scan: _Scan) -> None:
        while True:
            with self._lock:
                if self._closed or self._active is not scan or scan.cancelled.is_set():
                    return
                now = time.monotonic()
                for player_id in scan.members - scan.terminal.keys():
                    if now >= scan.deadlines[player_id]:
                        old = scan.stale.get(player_id)
                        self._player_locked(scan, player_id, 'timeout', old,
                                            '本轮已截止，显示历史缓存' if old else '本轮已截止，暂无查询结果')
                pending = scan.members - scan.terminal.keys()
                if not pending and not scan.finished:
                    scan.finished = True
                    scan.done.set()
                    self._emit_locked(scan, {'type': 'finished', 'revision': scan.revision,
                        'elapsed': now - scan.started, 'total': len(scan.members),
                        'complete': sum(scan.terminal.get(pid) in {'ok', 'cached'} for pid in scan.members),
                        'timed_out': any(scan.terminal.get(pid) == 'timeout' for pid in scan.members)})
                delay = max(0.0, min(scan.deadlines[pid] for pid in pending) - now) if pending else None
                scan.wake.clear()
            scan.wake.wait(delay)

    def _read_cache(self, player_id: int) -> tuple[dict[str, Any], float] | None:
        path = self.cache_dir / f"{player_id}.json"
        try:
            if path.stat().st_size > 1_048_576:
                return None
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(wrapper, dict) or wrapper.get("schema") != 1:
                return None
            if wrapper.get("player_id") != player_id:
                return None
            data = wrapper.get("data")
            if not isinstance(data, dict) or data.get("player_id") != player_id:
                return None
            if data.get('core_ready') is False:
                return None
            stored_at = _timestamp(wrapper.get("stored_at"))
            if stored_at > time.time() + 300:
                return None
            data = dict(data)
            _timestamp(data.get("fetched_at"))
            data["from_cache"] = True
            age = max(0.0, time.time() - stored_at)
            profile_version = data.get("profile_version", 1)
            if self.min_profile_version >= 4 and profile_version == 3:
                # Old profiles could claim "ready" with only wins. Empty
                # lists also conflated missing fields with a genuine zero.
                data['core_ready'] = ranked_counts_available(data.get('wins'), data.get('ranked_games'))
                data['specs_available'] = isinstance(data.get('specs'), list) and bool(data['specs'])
                if data['core_ready'] and data['specs_available'] and profile_history(data) is not None:
                    profile_version = 4
            if not isinstance(profile_version, int) or profile_version < self.min_profile_version:
                age = max(age, self.ttl + 1.0)
            return data, age
        except (OSError, ValueError, TypeError, OverflowError):
            return None

    def _write_cache(self, player_id: int, data: dict[str, Any], scan: _Scan) -> None:
        if data.get('core_ready') is False:
            return
        path = self.cache_dir / f"{player_id}.json"
        temporary = self.cache_dir / f".{player_id}.{uuid.uuid4().hex}.tmp"
        try:
            wrapper = {
                "schema": 1,
                "player_id": player_id,
                "stored_at": _utc_now(),
                "data": {k: v for k, v in data.items() if k != "from_cache"},
            }
            serialized = json.dumps(wrapper, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > 1_048_576:
                return
            temporary.write_text(serialized, encoding="utf-8")
            with self._lock:
                if self._player_live_locked(scan, player_id):
                    os.replace(temporary, path)
        except (OSError, ValueError, TypeError, OverflowError):
            # Cache failure must not turn a successful lookup into a failure.
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
