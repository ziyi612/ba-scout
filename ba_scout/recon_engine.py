"""Best-effort recent-match summaries, independently bounded from profiles.

All cache I/O stays inside the application directory.  Match
pages are deduplicated within a round and cached for one day.  Workers are a
fixed daemon pool, so closing a window never waits for a network response.
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
from .settings import app_path


_CACHE_SECONDS = 86400.0
_MAX_CACHE_BYTES = 2_097_152


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
    cancelled: threading.Event = field(default_factory=threading.Event)
    assignments: dict[int, int] = field(default_factory=dict)
    queued: set[int] = field(default_factory=set)
    results: dict[int, tuple[dict[str, Any], bool]] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    terminal: dict[int, str] = field(default_factory=dict)
    finished: bool = False


class RecentRecon:
    """Emit one ``recon`` event per submitted player, or a deadline timeout.

    ``start`` sets a single total deadline.  ``submit`` never moves that
    deadline and never waits for network or disk.  Re-submitting the same
    player/match is a no-op; a different match within the active round updates
    that player's assignment.  Late responses only apply to still-matching
    assignments.  Unknown player IDs are ignored.
    """

    def __init__(self, cache_dir: str, fetcher: Callable[..., dict[str, Any]], concurrency: int = 2) -> None:
        resolved = app_path(cache_dir)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 2:
            raise ValueError("近期对局查询并发数必须为 1 或 2")
        resolved.mkdir(parents=True, exist_ok=True)
        self.cache_dir = resolved
        self.fetcher = fetcher
        self.concurrency = concurrency
        self._lock = threading.RLock()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._jobs: queue.Queue[tuple[_Round, int] | None] = queue.Queue()
        self._active: _Round | None = None
        self._counter = 0
        self._closed = False
        self._cooldown_until = 0.0
        self._workers: list[threading.Thread] = []
        for index in range(concurrency):
            worker = threading.Thread(target=self._work, name=f"ba-scout-recon-{index + 1}", daemon=True)
            worker.start()
            self._workers.append(worker)

    def start(self, player_ids: list[int], deadline: float = 19) -> int:
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("deadline must be positive and finite")
        expected = []
        for value in player_ids:
            player_id = _positive_id(value)
            if player_id is None:
                raise ValueError("玩家 ID 必须是正整数")
            if player_id not in expected:
                expected.append(player_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("RecentRecon has been closed")
            if self._active is not None:
                self._active.cancelled.set()
            self._counter += 1
            scan = _Round(self._counter, expected, time.monotonic() + float(deadline))
            self._active = scan
            threading.Thread(
                target=self._watch_deadline,
                args=(scan,),
                name=f"ba-scout-recon-deadline-{scan.scan_id}",
                daemon=True,
            ).start()
            return scan.scan_id

    def submit(self, player_id: int, match_id: int) -> None:
        player = _positive_id(player_id)
        match = _positive_id(match_id)
        with self._lock:
            scan = self._active
            if scan is None or not self._is_active_locked(scan) or time.monotonic() >= scan.ends:
                return
            if player not in scan.player_ids:
                return
            if match is None:
                if player not in scan.terminal and player not in scan.assignments:
                    self._emit_recon_locked(scan, player, None, "error", None, None, False, "没有可查询的最近对局编号")
                return
            if scan.assignments.get(player) == match:
                return
            scan.assignments[player] = match
            scan.terminal.pop(player, None)
            if match in scan.results:
                data, from_cache = scan.results[match]
                self._publish_player_locked(scan, player, match, data, from_cache)
            elif match in scan.errors:
                self._emit_recon_locked(scan, player, match, "error", None, None, False, scan.errors[match])
            elif match not in scan.queued:
                scan.queued.add(match)
                self._jobs.put((scan, match))

    def poll_events(self) -> list[dict[str, Any]]:
        result = []
        with self._lock:
            active_id = None if self._active is None else self._active.scan_id
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                if (
                    not self._closed
                    and event["scan_id"] == active_id
                    and self._active is not None
                    and event["match_id"] == self._active.assignments.get(event["player_id"])
                ):
                    result.append(event)
        return result

    def cancel(self) -> None:
        with self._lock:
            if self._active is not None:
                self._active.cancelled.set()
                self._active = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.cancel()
            self._closed = True
            for _ in self._workers:
                self._jobs.put(None)

    def _is_active_locked(self, scan: _Round) -> bool:
        return not self._closed and self._active is scan and not scan.cancelled.is_set() and not scan.finished

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            scan, match_id = job
            with self._lock:
                if not self._is_active_locked(scan) or time.monotonic() >= scan.ends:
                    continue
            cached = self._read_cache(match_id)
            if cached is not None:
                with self._lock:
                    if self._is_active_locked(scan) and time.monotonic() < scan.ends:
                        self._publish_match_locked(scan, match_id, cached, True)
                continue
            with self._lock:
                if not self._is_active_locked(scan):
                    continue
                remaining = scan.ends - time.monotonic()
                if remaining <= 0:
                    continue
                cooldown = self._cooldown_until - time.monotonic()
                if cooldown > 0:
                    self._publish_error_locked(scan, match_id, f"数据站请求限流，约 {math.ceil(cooldown)} 秒后可重试")
                    continue
            try:
                raw = self.fetcher(match_id, timeout=min(8.0, remaining))
                data = self._validate_result(raw, match_id)
                with self._lock:
                    if not self._is_active_locked(scan) or time.monotonic() >= scan.ends:
                        continue
                    useful = any(
                        assigned == match_id and self._available_summary(data, player_id) is not None
                        for player_id, assigned in scan.assignments.items()
                    )
                # A failed/missing requested summary must not leave a success
                # cache entry.  A shared page may still be useful for others.
                if useful:
                    self._write_cache(match_id, data, scan)
                with self._lock:
                    if self._is_active_locked(scan) and time.monotonic() < scan.ends:
                        self._publish_match_locked(scan, match_id, data, False)
            except Exception as exc:
                message = str(exc).replace("\n", " ").replace("\r", " ")[:180] or type(exc).__name__
                with self._lock:
                    if isinstance(exc, HTTPError) and exc.code == 429:
                        self._cooldown_until = max(self._cooldown_until, time.monotonic() + 60.0)
                        message = "数据站请求限流，已暂停新查询至少 60 秒"
                    if self._is_active_locked(scan) and time.monotonic() < scan.ends:
                        self._publish_error_locked(scan, match_id, "近期对局查询失败：" + message)

    @staticmethod
    def _validate_result(raw: Any, match_id: int) -> dict[str, Any]:
        if not isinstance(raw, dict) or _positive_id(raw.get("match_id")) != match_id:
            raise ValueError("对局编号与请求不匹配")
        if not isinstance(raw.get("players"), dict):
            raise ValueError("对局摘要缺少玩家数据")
        data = dict(raw)
        data["match_id"] = match_id
        fetched_at = data.get("fetched_at")
        if not fetched_at:
            data["fetched_at"] = _utc_now()
        else:
            _timestamp(fetched_at)
        return data

    @staticmethod
    def _available_summary(data: dict[str, Any], player_id: int) -> dict[str, Any] | None:
        summary = data["players"].get(str(player_id))
        if not isinstance(summary, dict) or not summary or summary.get("available") is False:
            return None
        return summary

    def _publish_match_locked(self, scan: _Round, match_id: int, data: dict[str, Any], from_cache: bool) -> None:
        scan.results[match_id] = (data, from_cache)
        for player_id, assigned in scan.assignments.items():
            if assigned == match_id:
                self._publish_player_locked(scan, player_id, match_id, data, from_cache)

    def _publish_player_locked(self, scan: _Round, player_id: int, match_id: int, data: dict[str, Any], from_cache: bool) -> None:
        if player_id in scan.terminal:
            return
        summary = self._available_summary(data, player_id)
        if summary is not None:
            self._emit_recon_locked(scan, player_id, match_id, "ok", dict(summary), data, from_cache, "使用近期对局缓存" if from_cache else "近期对局摘要已取得")
            return
        raw_summary = data["players"].get(str(player_id))
        if isinstance(raw_summary, dict) and raw_summary.get("available") is False:
            reason = raw_summary.get("reason") or raw_summary.get("message")
            message = "该玩家的近期对局摘要不可用"
            if isinstance(reason, str) and reason.strip():
                message += "：" + reason[:150]
        else:
            message = "该对局未提供此玩家的有效摘要"
        self._emit_recon_locked(scan, player_id, match_id, "error", None, data, from_cache, message)

    def _publish_error_locked(self, scan: _Round, match_id: int, message: str) -> None:
        scan.errors[match_id] = message
        for player_id, assigned in scan.assignments.items():
            if assigned == match_id and player_id not in scan.terminal:
                self._emit_recon_locked(scan, player_id, match_id, "error", None, None, False, message)

    def _emit_recon_locked(
        self,
        scan: _Round,
        player_id: int,
        match_id: int | None,
        status: str,
        summary: dict[str, Any] | None,
        envelope: dict[str, Any] | None,
        from_cache: bool,
        message: str,
    ) -> None:
        if self._closed or self._active is not scan:
            return
        envelope = envelope or {}
        scan.terminal[player_id] = status
        self._events.put({
            "type": "recon",
            "scan_id": scan.scan_id,
            "player_id": player_id,
            "match_id": match_id,
            "status": status,
            "data": summary,
            "source_url": envelope.get("source_url"),
            "match_end_time": envelope.get("match_end_time"),
            "fetched_at": envelope.get("fetched_at"),
            "from_cache": from_cache,
            "message": message,
        })

    def _watch_deadline(self, scan: _Round) -> None:
        if scan.cancelled.wait(max(0.0, scan.ends - time.monotonic())):
            return
        with self._lock:
            if not self._is_active_locked(scan):
                return
            for player_id in scan.player_ids:
                if player_id not in scan.terminal:
                    match_id = scan.assignments.get(player_id)
                    message = "本轮已截止，暂无近期对局摘要" if match_id else "本轮已截止，暂未取得最近对局编号"
                    self._emit_recon_locked(scan, player_id, match_id, "timeout", None, None, False, message)
            scan.finished = True
            scan.cancelled.set()

    def _read_cache(self, match_id: int) -> dict[str, Any] | None:
        path = self.cache_dir / f"{match_id}.json"
        try:
            if path.stat().st_size > _MAX_CACHE_BYTES:
                return None
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(wrapper, dict) or wrapper.get("schema") != 1 or _positive_id(wrapper.get("match_id")) != match_id:
                return None
            stored_at = _timestamp(wrapper.get("stored_at"))
            age = time.time() - stored_at
            if age < -300 or age > _CACHE_SECONDS:
                return None
            return self._validate_result(wrapper.get("data"), match_id)
        except (OSError, ValueError, TypeError, OverflowError):
            return None

    def _write_cache(self, match_id: int, data: dict[str, Any], scan: _Round) -> None:
        target = self.cache_dir / f"{match_id}.json"
        temporary = self.cache_dir / f".{match_id}.{uuid.uuid4().hex}.tmp"
        try:
            wrapper = {"schema": 1, "match_id": match_id, "stored_at": _utc_now(), "data": data}
            serialized = json.dumps(wrapper, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > _MAX_CACHE_BYTES:
                return
            temporary.write_text(serialized, encoding="utf-8")
            with self._lock:
                if self._is_active_locked(scan) and time.monotonic() < scan.ends:
                    os.replace(temporary, target)
        except (OSError, ValueError, TypeError, OverflowError):
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
