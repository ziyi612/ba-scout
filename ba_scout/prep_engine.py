"""Bounded preparation-screen identity lookup; no Tk or game-state manipulation.

The UI must start this controller only for an explicit waiting/preparation
state and calibrated regions. Each Bravo row must be queryable and stable in
consecutive frames. Later rows are collected while earlier rows are queried.
Bravo is a screen-side label, not inferred from
an OCR nickname. Partial identities remain unconfirmed by the game's ID log.

One coordinator and a fixed daemon search pool are shared by every generation.
Cancellation never creates another pool or waits for a slow network operation.
The supplied OCR client and RequestGate are borrowed, not owned or closed here.
"""

from __future__ import annotations

import copy
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError

from .player_search import normalize_name, resolve_exact_name, search_players


_NAME_REQUEST_LIMIT = 8
_NAME_LIMIT_REASON = "本轮姓名搜索上限（8次），未完整查询本行昵称变体，部分待确认。"
_STREAM_REQUEST_LIMIT = 30
_STREAM_VARIANT_LIMIT = 12
_STREAM_RETRY_DELAY = 1.5
_STREAM_LIMIT_REASON = "本局姓名搜索上限（30次），尚未完整查询的对手保留待确认。"


class _NameRequestLimit(RuntimeError):
    pass


@dataclass
class _NameMemo:
    result: list | None = None
    error: Exception | None = None
    attempts: int = 0
    running: bool = False
    retry_at: float = float("inf")


@dataclass
class _StreamRow:
    row: dict
    signature: tuple
    version: int
    lookup_deadline: float
    report_deadline: float
    pending: bool = False
    attempted: bool = False
    resolution: dict | None = None
    scheduled: set = field(default_factory=set)
    batches: dict = field(default_factory=dict)


@dataclass
class _VariantTask:
    record: _StreamRow
    variant: str


@dataclass
class _Round:
    generation: int
    fight_id: str
    regions: object
    manual_rows: list | None
    source: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    started: float | None = None
    deadline: float | None = None
    lookup_deadline: float | None = None
    capture_started: float | None = None
    rows: list = field(default_factory=list)
    previous: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    pending: set = field(default_factory=set)
    name_requests: int = 0
    request_limit_reached: bool = False
    finished: bool = False
    status_key: tuple | None = None
    stream_rows: dict = field(default_factory=dict)
    name_cache: dict = field(default_factory=dict)
    first_seen: dict = field(default_factory=dict)
    revision: int = 0
    published_signature: object = None
    initial_reported: bool = False


def _default_extract(frame, regions):
    from .prep_roster import extract_prep_roster
    return extract_prep_roster(frame, regions)


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(label + " must be positive and finite")
    return float(value)


def _empty_resolution(status, reason, candidates=None):
    return {"status": status, "player_id": None, "name": None, "matches": [],
            "candidates": candidates or [], "normalized_variants": [], "reason": reason}


def _prepare_rows(raw_rows, manual=False):
    if not isinstance(raw_rows, list) or len(raw_rows) > 10:
        raise ValueError("preparation rows must be a list of at most ten rows")
    result = []
    seen = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("preparation row must be an object")
        team, index = raw.get("team"), raw.get("index")
        if team not in ("Alpha", "Bravo") or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 5:
            raise ValueError("each row needs Alpha/Bravo and index 0..4")
        key = team, index
        if key in seen:
            raise ValueError("duplicate preparation row location")
        seen.add(key)
        row = {"team": team, "index": index, "variants": [], "queryable": False,
               "observed_variants": [variant.get("text") if isinstance(variant, dict) else variant
                                     for variant in (raw.get("variants") or [])
                                     if isinstance(variant.get("text") if isinstance(variant, dict) else variant, str)],
               "issues": list(raw.get("issues") or []), "candidates": [],
               "resolution": _empty_resolution("missing", "此行昵称未完整识别或未稳定。")}
        if manual and "player_id" in raw:
            player_id = raw["player_id"]
            if isinstance(player_id, str) and player_id.isascii() and player_id.isdecimal():
                player_id = int(player_id)
            try:
                if isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0:
                    raise ValueError("confirmed ID must be a positive integer")
                normalize_name(raw.get("name"))
                row["resolution"] = {"status": "manual-confirmed", "player_id": player_id,
                                     "name": raw["name"], "matches": [], "candidates": [],
                                     "normalized_variants": [], "reason": "用户明确确认数字 ID；未自动按昵称推断。"}
            except ValueError as exc:
                row["resolution"] = _empty_resolution("invalid", str(exc))
            result.append(row)
            continue
        variants = raw.get("variants")
        if isinstance(variants, list):
            try:
                names = []
                for variant in variants:
                    if isinstance(variant, dict):
                        if variant.get("queryable") is not True or variant.get("manual_only") is True:
                            continue
                        value = variant.get("text")
                    else:
                        value = variant
                    names.append(normalize_name(value))
                row["variants"] = list(dict.fromkeys(names))
                row["queryable"] = bool(names) and (manual or raw.get("queryable") is True)
                if raw.get("valid") is False and not manual:
                    row["queryable"] = False
            except ValueError as exc:
                row["resolution"] = _empty_resolution("invalid", str(exc))
                row["queryable"] = False
        result.append(row)
    return sorted(result, key=lambda row: (row["team"] != "Bravo", row["index"]))


class PrepController:
    """Emit status/resolved events; all public methods return without waiting.

    ``start(fight_id, regions, manual_rows=None)`` returns a generation integer.
    A repeated automatic start of the active fight is a no-op, including after
    its result; explicit retry should call cancel() before start(). Manual rows
    always start a new round, and may contain one row or explicit player_id/name.

    Automatic rounds publish complete roster snapshots incrementally. Each
    enemy row needs two consecutive stable frames and gets its own lookup
    window; later names keep being captured after the initial 20-second report.
    ``revision`` orders snapshots, ``final`` means all five enemy slots were
    confirmed, and ``player_deadlines`` bounds each newly observed identity's
    data fetch. The first report deadline never moves. Unchanged successful
    name queries are cached within a generation; at most 30 HTTP calls and 12
    variants per row are admitted. Only transient failures may retry once.

    Manual input retains one final result, a shared 12-second lookup window,
    an eight-request cap, and enemies before allies. Neither path guesses IDs.
    """

    def __init__(self, ocr, request_gate, *, searcher=search_players, extractor=None,
                 capture_interval=0.35, lookup_budget=12.0, total_budget=20.0,
                 concurrency=5, search_timeout=4.0):
        self.capture_interval = _positive(capture_interval, "capture_interval")
        self.lookup_budget = _positive(lookup_budget, "lookup_budget")
        self.total_budget = _positive(total_budget, "total_budget")
        self.search_timeout = _positive(search_timeout, "search_timeout")
        if self.total_budget > 20 or self.lookup_budget > self.total_budget:
            raise ValueError("lookup budget must not exceed total budget, capped at 20 seconds")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 5:
            raise ValueError("concurrency must be between 1 and 5")
        if not callable(searcher) or not callable(getattr(request_gate, "run", None)):
            raise ValueError("searcher and a shared RequestGate are required")
        self.ocr = ocr
        self.request_gate = request_gate
        self.searcher = searcher
        self.extractor = extractor or _default_extract
        self.concurrency = concurrency
        self._condition = threading.Condition(threading.RLock())
        self._events = queue.Queue()
        self._jobs = queue.Queue(maxsize=10)
        self._active = None
        self._counter = 0
        self._closed = False
        self._capture_future = None
        self._workers = []
        for index in range(concurrency):
            worker = threading.Thread(target=self._work, name=f"ba-scout-name-{index + 1}", daemon=True)
            worker.start()
            self._workers.append(worker)
        self._coordinator = threading.Thread(target=self._coordinate, name="ba-scout-preparation", daemon=True)
        self._coordinator.start()

    def start(self, fight_id, regions, manual_rows=None):
        if isinstance(fight_id, bool) or not isinstance(fight_id, (str, int)) or not str(fight_id).strip():
            raise ValueError("a known fight_id is required")
        fight_id = str(fight_id).strip()
        rows = _prepare_rows(copy.deepcopy(manual_rows), manual=True) if manual_rows is not None else None
        with self._condition:
            if self._closed:
                raise RuntimeError("PrepController has been closed")
            if rows is None and self._active is not None and self._active.fight_id == fight_id:
                return self._active.generation
            self._cancel_locked()
            self._counter += 1
            scan = _Round(self._counter, fight_id, copy.deepcopy(regions), rows,
                          "manual" if rows is not None else "screen")
            if rows is not None:
                self._start_clock(scan)
                scan.rows = rows
            self._active = scan
            self._status(scan, "waiting", "等待准备界面的稳定昵称。" if rows is None else "正在解析手动确认的准备名单。")
            self._condition.notify_all()
            return scan.generation

    def cancel(self):
        with self._condition:
            self._cancel_locked()
            self._condition.notify_all()

    def _cancel_locked(self):
        if self._active is not None:
            self._active.cancelled.set()
        self._active = None
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break
        if self._capture_future is not None:
            self._capture_future.cancel()

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._cancel_locked()
            self._closed = True
            self._condition.notify_all()
        # Workers poll the closed flag, so no join or blocked executor shutdown.

    def poll_events(self):
        result = []
        with self._condition:
            generation = self._active.generation if self._active is not None else None
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                if not self._closed and event["generation"] == generation:
                    result.append(event)
        return result

    def _live(self, scan):
        return not self._closed and self._active is scan and not scan.cancelled.is_set() and not scan.finished

    def _start_clock(self, scan, started=None):
        if scan.started is None:
            scan.started = time.monotonic() if started is None else started
            scan.deadline = scan.started + self.total_budget
            scan.lookup_deadline = scan.started + self.lookup_budget

    def _emit(self, scan, event):
        if self._active is scan and not self._closed and not scan.cancelled.is_set():
            self._events.put({"generation": scan.generation, "fight_id": scan.fight_id,
                              "source": scan.source, "started": scan.started, "deadline": scan.deadline,
                              "initial_deadline": scan.deadline,
                              "progressive": scan.manual_rows is None,
                              "name_requests": scan.name_requests,
                              "name_request_limit": _NAME_REQUEST_LIMIT if scan.manual_rows is not None else _STREAM_REQUEST_LIMIT,
                              "request_limit_reached": scan.request_limit_reached,
                              "elapsed": None if scan.started is None else time.monotonic() - scan.started,
                              **event})

    def _status(self, scan, status, message, **extra):
        key = status, message, extra.get("capture_status")
        if scan.status_key != key:
            scan.status_key = key
            self._emit(scan, {"type": "status", "status": status, "message": message, **extra})

    def _pause(self, scan, seconds):
        end = time.monotonic() + max(0.0, seconds)
        with self._condition:
            while self._live(scan) and time.monotonic() < end:
                self._condition.wait(min(0.05, end - time.monotonic()))
            return self._live(scan)

    def _coordinate(self):
        while True:
            with self._condition:
                while not self._closed and (self._active is None or self._active.finished):
                    self._condition.wait()
                if self._closed:
                    return
                scan = self._active
            try:
                if scan.manual_rows is not None:
                    self._resolve(scan, [row for row in scan.rows if row["queryable"]])
                else:
                    self._stream_capture(scan)
            except Exception as exc:
                with self._condition:
                    if self._live(scan):
                        self._start_clock(scan)
                        self._status(scan, "error", "准备名单解析失败：" + str(exc)[:180])
                        if scan.manual_rows is None:
                            self._stream_publish(scan, force=True)
                            self._pause(scan, max(self.capture_interval, .5))
                        else:
                            self._finish(scan, reason="准备名单解析失败，需手动校正。")

    def _capture(self, scan):
        # A cancelled old OCR call can still be in flight. Wait/discard its
        # result before submitting another screenshot, keeping the OCR queue bounded.
        while self._capture_future is not None and not self._capture_future.done():
            if not self._pause(scan, 0.025):
                return None
            if scan.manual_rows is None:
                with self._condition:
                    self._stream_tick(scan)
            if scan.manual_rows is not None and scan.lookup_deadline is not None and time.monotonic() >= scan.lookup_deadline:
                return None
        if not self._live(scan):
            return None
        remaining = 8.0 if scan.manual_rows is None or scan.lookup_deadline is None else scan.lookup_deadline - time.monotonic()
        if remaining <= 0:
            return None
        scan.capture_started = time.monotonic()
        capture_options = {'timeout': min(8.0, remaining)}
        if scan.regions:
            capture_options['regions'] = scan.regions
        future = self.ocr.submit_capture_game(**capture_options)
        self._capture_future = future
        while not future.done():
            if not self._pause(scan, 0.025):
                return None
            if scan.manual_rows is None:
                with self._condition:
                    self._stream_tick(scan)
            elif scan.lookup_deadline is not None and time.monotonic() >= scan.lookup_deadline:
                future.cancel()
                return None
        if not self._live(scan):
            return None
        return future.result()

    def _stream_capture(self, scan):
        """Keep observing unfilled slots while a bounded worker pool searches."""
        while self._live(scan):
            frame = None
            try:
                frame = self._capture(scan)
                if frame is None:
                    continue
                if not isinstance(frame, dict) or frame.get("ok") is not True:
                    raise ValueError(str(frame.get("error") or frame.get("status")) if isinstance(frame, dict) else "invalid OCR response")
                extracted = self.extractor(frame, scan.regions)
                if not isinstance(extracted, dict):
                    raise ValueError("invalid roster extraction")
                rows = _prepare_rows(extracted.get("rows"))
                with self._condition:
                    if not self._live(scan):
                        return
                    if extracted.get("geometry_valid") is not True:
                        scan.previous = {}
                        scan.first_seen = {}
                        self._status(scan, "waiting", "昵称区域校准无效，暂不查询身份。", capture_status="invalid_geometry")
                    else:
                        scan.rows = rows
                        current = {(row["team"], row["index"]): tuple(sorted(row["variants"]))
                                   for row in rows if row["team"] == "Bravo" and row["queryable"]}
                        if current:
                            self._start_clock(scan, scan.capture_started)
                        stable_count = 0
                        # First-seen timestamps apply only to an uninterrupted
                        # two-frame observation. Placeholders reset stability,
                        # but never discard already validated identities.
                        first_seen = {}
                        for row in rows:
                            key = row["team"], row["index"]
                            if key not in current:
                                continue
                            signature = current[key]
                            stable = scan.previous.get(key) == signature
                            first_seen[key] = scan.first_seen.get(key, scan.capture_started) if stable else scan.capture_started
                            if not stable:
                                continue
                            stable_count += 1
                            record = scan.stream_rows.get(key)
                            if record is not None and record.signature == signature:
                                continue
                            if record is not None and self._reuse_resolved_subset(record, row, signature):
                                continue
                            # A stable changed name invalidates the old slot
                            # before new HTTP completes. Late old jobs carry a
                            # version and cannot republish a revoked identity.
                            record = _StreamRow(copy.deepcopy(row), signature,
                                                1 if record is None else record.version + 1,
                                                first_seen[key] + self.lookup_budget,
                                                max(scan.deadline, first_seen[key] + self.total_budget))
                            if len(signature) > _STREAM_VARIANT_LIMIT:
                                record.attempted = True
                                record.resolution = _empty_resolution("incomplete", "本行超过 12 个识别变体，保留待更清晰画面或手动核对；不会截取部分变体猜测身份。")
                            scan.stream_rows[key] = record
                        scan.previous, scan.first_seen = current, first_seen
                        placeholders = (extracted.get("placeholder_counts") or {}).get("Bravo", 0)
                        if not current and placeholders:
                            self._status(scan, "names_unavailable", "敌方栏暂显示 Player name 占位文字；等待真实昵称出现后自动查询。",
                                         placeholder_count=placeholders)
                        elif stable_count:
                            self._status(scan, "searching", f"已取得 {stable_count}/5 行稳定对手昵称；逐人查询，继续读取其余姓名。")
                        else:
                            self._status(scan, "stabilizing" if scan.started is not None else "waiting",
                                         f"等待连续两帧稳定昵称：当前可查询 {len(current)}/5 行，已稳定 0/5 行。")
                        self._stream_schedule(scan)
                        self._stream_publish(scan)
                    self._stream_tick(scan)
            except Exception as exc:
                with self._condition:
                    if not self._live(scan):
                        return
                    scan.previous = {}
                    scan.first_seen = {}
                    frame_status = frame.get("status") if isinstance(frame, dict) else None
                    self._status(scan, "capture_error", "准备画面暂不可用：" + str(exc)[:180], capture_status=frame_status)
                    self._stream_tick(scan)
            # After the first report, slow observation down. Cache and attempt
            # bounds prevent unchanged frames from generating more HTTP calls.
            interval = self.capture_interval
            if scan.deadline is not None and time.monotonic() >= scan.deadline:
                interval = max(interval, min(1.0, self.lookup_budget / 4.0))
            end = time.monotonic() + interval
            while self._live(scan) and time.monotonic() < end:
                self._pause(scan, min(.05, max(0.0, end - time.monotonic())))
                with self._condition:
                    self._stream_tick(scan)

    @staticmethod
    def _reuse_resolved_subset(record, row, signature):
        """Keep a proven identity when stable OCR only drops unused variants.

        A newly introduced spelling still invalidates the old identity. This
        path uses only complete, already queried batches and requires the exact
        resolved name to remain visible; it never case-folds or fuzzy-matches.
        """
        previous = record.resolution or {}
        if (previous.get("status") != "resolved" or not signature
                or not set(signature).issubset(record.signature)
                or normalize_name(previous["name"]) not in signature
                or set(record.batches) != set(record.signature)):
            return False
        batches = [(name, record.batches[name]) for name in row["variants"]]
        candidates = [candidate for _, batch in batches for candidate in batch]
        resolution = resolve_exact_name(row["variants"], candidates, query_results=batches)
        if resolution["status"] != "resolved" or resolution["player_id"] != previous["player_id"]:
            return False
        record.row = copy.deepcopy(row)
        record.signature = signature
        record.batches = dict(batches)
        record.scheduled = set(signature)
        record.resolution = resolution
        return True

    def _stream_schedule(self, scan):
        if not self._live(scan):
            return
        # Round robin over rows, one variant at a time. A long name may use
        # spare workers after other rows finish, without serializing 12 HTTPs
        # in one worker or allocating another pool.
        while not self._jobs.full():
            admitted = False
            for key, record in sorted(scan.stream_rows.items()):
                if record.resolution is not None:
                    continue
                if time.monotonic() >= record.lookup_deadline:
                    record.pending = False
                    record.resolution = _empty_resolution("timeout", "此行连续画面确认耗尽查询时间；等待姓名变化或手动核对。")
                    continue
                variant = next((name for name in record.row["variants"] if name not in record.scheduled), None)
                if variant is None or self._jobs.full():
                    continue
                record.attempted = record.pending = True
                record.scheduled.add(variant)
                self._jobs.put_nowait((scan, key, _VariantTask(record, variant)))
                admitted = True
            if not admitted:
                break

    def _stream_tick(self, scan):
        if not self._live(scan):
            return
        now = time.monotonic()
        changed = False
        for record in scan.stream_rows.values():
            if record.pending and now >= record.lookup_deadline:
                record.pending = False
                record.resolution = _empty_resolution("timeout", "此行身份查询时间已到，未完整查完所有变体；其他对手继续。")
                changed = True
        first_deadline = scan.deadline is not None and now >= scan.deadline and not scan.initial_reported
        if first_deadline:
            scan.initial_reported = True
        if changed or first_deadline:
            self._stream_publish(scan, force=first_deadline)

    @staticmethod
    def _transient(error):
        if isinstance(error, HTTPError):
            return error.code in {408, 500, 502, 503, 504}
        return isinstance(error, (OSError, TimeoutError))

    def _stream_current(self, scan, key, record):
        return self._live(scan) and scan.stream_rows.get(key) is record

    def _stream_query_active(self, scan, key, record):
        return self._stream_current(scan, key, record) and record.resolution is None

    def _stream_http(self, scan, key, record, variant, memo):
        # RequestGate calls this only after permitting actual network work.
        with self._condition:
            if not self._stream_query_active(scan, key, record):
                raise RuntimeError("姓名位置已更新或本局已取消")
            remaining = record.lookup_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("此行身份查询时间已到")
            if scan.name_requests >= _STREAM_REQUEST_LIMIT:
                scan.request_limit_reached = True
                self._status(scan, "request_limit", _STREAM_LIMIT_REASON)
                raise _NameRequestLimit(_STREAM_LIMIT_REASON)
            scan.name_requests += 1
            memo.attempts += 1
        return self.searcher(variant, timeout=min(self.search_timeout, remaining))

    def _stream_search(self, scan, key, record, variant):
        """Generation-local single flight cache, including bounded failures."""
        while True:
            with self._condition:
                if not self._stream_query_active(scan, key, record):
                    raise RuntimeError("姓名位置已更新或本局已取消")
                remaining = record.lookup_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("此行身份查询时间已到")
                memo = scan.name_cache.setdefault(variant, _NameMemo())
                if memo.result is not None:
                    return memo.result
                if memo.running:
                    self._condition.wait(min(.05, remaining))
                    continue
                if memo.error is not None:
                    if memo.attempts >= 2 or not self._transient(memo.error):
                        raise memo.error
                    delay = memo.retry_at - time.monotonic()
                    if delay > 0:
                        self._condition.wait(min(.05, delay, remaining))
                        continue
                memo.running = True
                attempts_before = memo.attempts
            result = None
            error = None
            try:
                result = self.request_gate.run(self._stream_http, scan, key, record, variant, memo)
                if not isinstance(result, list):
                    raise ValueError("姓名搜索没有返回候选列表")
            except Exception as exc:
                error = exc
            with self._condition:
                memo.running = False
                memo.error = error
                memo.result = result if error is None else None
                memo.retry_at = time.monotonic() + _STREAM_RETRY_DELAY if error is not None and self._transient(error) else float("inf")
                if (error is not None and memo.attempts == attempts_before
                        and not self._stream_query_active(scan, key, record)
                        and scan.name_cache.get(variant) is memo):
                    # An invalidated slot is not a network failure for another
                    # slot that shares this name. Keep only real response data.
                    scan.name_cache.pop(variant, None)
                self._condition.notify_all()
            # Returning through the loop verifies cancellation, row version,
            # and deadline even when the last network call returned late.

    def _stream_work(self, scan, key, task):
        record, variant = task.record, task.variant
        found, failure = None, None
        try:
            found = self._stream_search(scan, key, record, variant)
        except _NameRequestLimit:
            failure = _empty_resolution("incomplete", _STREAM_LIMIT_REASON)
        except TimeoutError:
            failure = _empty_resolution("timeout", "此行身份查询时间已到，未完整查完所有变体；其他对手继续。")
        except Exception as exc:
            failure = _empty_resolution("error", "此行搜索未完整成功，暂不确认身份：" + str(exc)[:180])
        with self._condition:
            if self._stream_query_active(scan, key, record) and time.monotonic() < record.lookup_deadline:
                if failure is not None:
                    failure["candidates"] = [candidate for batch in record.batches.values() for candidate in batch]
                    record.resolution = failure
                else:
                    record.batches[variant] = found
                    if len(record.batches) == len(record.row["variants"]):
                        batches = [(name, record.batches[name]) for name in record.row["variants"]]
                        candidates = [candidate for _, batch in batches for candidate in batch]
                        record.resolution = resolve_exact_name(record.row["variants"], candidates, query_results=batches)
                record.pending = record.resolution is None
                self._stream_schedule(scan)
                self._stream_publish(scan)
                self._condition.notify_all()

    def _stream_publish(self, scan, force=False):
        """Publish a complete snapshot; duplicate IDs revoke every occurrence."""
        if not self._live(scan) or scan.started is None:
            return
        rows_by_key = {(row["team"], row["index"]): copy.deepcopy(row) for row in scan.rows}
        for key, record in scan.stream_rows.items():
            row = copy.deepcopy(record.row)
            row["resolution"] = copy.deepcopy(record.resolution or _empty_resolution("searching", "此行已稳定，正在查询完整姓名变体。"))
            rows_by_key[key] = row
        rows = [row for _, row in sorted(rows_by_key.items(), key=lambda item: (item[0][0] != "Bravo", item[0][1]))]
        occurrences = {}
        for row in rows:
            decision = row["resolution"]
            if decision["status"] == "resolved":
                occurrences.setdefault(decision["player_id"], []).append(row)
        for player_id, conflicts in occurrences.items():
            if len(conflicts) > 1:
                for row in conflicts:
                    row["resolution"] = _empty_resolution("conflict", f"玩家 ID {player_id} 对应多个位置，相关身份全部撤回待确认。",
                                                          row["resolution"].get("candidates"))
        players, deadlines = [], {}
        for row in rows:
            decision = row["resolution"]
            row["status"] = decision["status"]
            row["candidates"] = decision.get("candidates", [])
            if decision["status"] == "resolved":
                player_id = decision["player_id"]
                players.append({"id": player_id, "name": decision["name"], "team": row["team"], "index": row["index"],
                                "identity_source": "screen-exact"})
                deadlines[player_id] = scan.stream_rows[(row["team"], row["index"])].report_deadline
        final = sum(player["team"] == "Bravo" for player in players) == 5
        signature = (final, tuple((row["team"], row["index"], tuple(row["variants"]) if row["status"] != "missing" else (), row["status"],
                                  row["resolution"].get("player_id"), row["resolution"].get("reason")) for row in rows))
        # Do not emit an empty roster merely because the first OCR frame was
        # seen; publish missing/failed rows after a lookup, revocation or deadline.
        has_decision = any(record.resolution is not None for record in scan.stream_rows.values())
        if not force and (signature == scan.published_signature or
                          not has_decision and scan.published_signature is None):
            return
        scan.published_signature = signature
        scan.revision += 1
        self._emit(scan, {"type": "resolved", "players": players, "rows": rows,
                          "revision": scan.revision, "final": final,
                          "initial_report_ready": scan.initial_reported,
                          "player_deadlines": deadlines,
                          "deadline": max([scan.deadline, *deadlines.values()]),
                          "identity_status": "screen_name_match_pending_log_id",
                          "message": f"准备阶段已匹配 {len(players)}/5 名对手；" + ("名单已齐，等待游戏 ID 核验。" if final else "继续自动识别其余姓名。"),
                          "lookup_elapsed": time.monotonic() - scan.started,
                          "lookup_budget": self.lookup_budget, "total_budget": self.total_budget})
        if final:
            scan.finished = True
        self._condition.notify_all()

    def _resolve(self, scan, query_rows):
        with self._condition:
            if not self._live(scan):
                return
            self._start_clock(scan)
            self._status(scan, "searching", f"正在查询 {len(query_rows)} 行昵称；不能确认的行保留待校正。")
            # Sorting one shared queue is insufficient: an idle worker could
            # start an ally while a slower opponent still needs more variants.
            # Only release the second batch after every opponent has finished.
            for team in ("Bravo", "Alpha"):
                if not self._live(scan):
                    return
                batch = [row for row in query_rows if row["team"] == team]
                for row in batch:
                    key = row["team"], row["index"]
                    if time.monotonic() >= scan.lookup_deadline:
                        scan.results[key] = _empty_resolution("timeout", "身份查询预算已截止，本行未启动查询。")
                    elif scan.name_requests >= _NAME_REQUEST_LIMIT:
                        self._name_limit(scan)
                        scan.results[key] = _empty_resolution("incomplete", _NAME_LIMIT_REASON)
                    else:
                        scan.pending.add(key)
                        self._jobs.put_nowait((scan, key, row))
                while self._live(scan) and scan.pending and time.monotonic() < scan.lookup_deadline:
                    self._condition.wait(min(0.05, scan.lookup_deadline - time.monotonic()))
            if self._live(scan):
                self._finish(scan)

    def _name_limit(self, scan):
        # Caller holds the shared condition lock.
        scan.request_limit_reached = True
        self._status(scan, "request_limit", "本轮姓名搜索上限（8次），部分待确认。")

    def _search_bounded(self, scan, variant):
        """Reserve just before the actual HTTP call, after the shared 429 gate.

        A rejected gate call does not consume a fictitious network request.
        The check and increment are atomic across all five workers. Requests
        already admitted before cancellation may finish, without publishing.
        """
        with self._condition:
            if not self._live(scan):
                raise RuntimeError("本轮已取消")
            remaining = scan.lookup_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("身份查询预算已截止")
            if scan.name_requests >= _NAME_REQUEST_LIMIT:
                self._name_limit(scan)
                raise _NameRequestLimit(_NAME_LIMIT_REASON)
            scan.name_requests += 1
        return self.searcher(variant, timeout=min(self.search_timeout, remaining))

    def _work(self):
        while True:
            if self._closed:
                return
            try:
                scan, key, row = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if scan.manual_rows is None:
                with self._condition:
                    current = self._stream_query_active(scan, key, row.record)
                if current:
                    self._stream_work(scan, key, row)
                else:
                    with self._condition:
                        self._stream_schedule(scan)
                continue
            with self._condition:
                if not self._live(scan) or time.monotonic() >= scan.lookup_deadline:
                    continue
            candidates = []
            query_results = []
            try:
                for variant in row["variants"]:
                    with self._condition:
                        if not self._live(scan):
                            raise RuntimeError("本轮已取消")
                        if time.monotonic() >= scan.lookup_deadline:
                            raise TimeoutError("身份查询预算已截止")
                    found = self.request_gate.run(self._search_bounded, scan, variant)
                    if not isinstance(found, list):
                        raise ValueError("姓名搜索没有返回候选列表")
                    candidates.extend(found)
                    query_results.append((variant, found))
                resolution = resolve_exact_name(row["variants"], candidates, query_results=query_results)
            except _NameRequestLimit:
                # Even one exact hit cannot establish identity when required
                # alternative spellings have not been searched completely.
                resolution = _empty_resolution("incomplete", _NAME_LIMIT_REASON, candidates)
            except Exception as exc:
                resolution = _empty_resolution("error", "此行搜索未完整成功，暂不确认身份：" + str(exc)[:180], candidates)
            with self._condition:
                if self._live(scan) and time.monotonic() < scan.lookup_deadline:
                    scan.results[key] = resolution
                    scan.pending.discard(key)
                    if resolution.get('status') == 'resolved':
                        # This row's complete variant set has resolved, but a
                        # different row may later identify the same ID. Only
                        # private prefetch may consume this event; _finish is
                        # still the sole duplicate-checked roster commit.
                        self._emit(scan, {'type': 'identity_ready', 'team': row['team'],
                                          'index': row['index'], 'player_id': resolution['player_id'],
                                          'name': resolution['name'], 'identity_final': False})
                    self._condition.notify_all()

    def _finish(self, scan, reason=None):
        if not self._live(scan):
            return
        rows = copy.deepcopy(scan.rows)
        for row in rows:
            key = row["team"], row["index"]
            if key in scan.results:
                row["resolution"] = scan.results[key]
            elif key in scan.pending:
                row["resolution"] = _empty_resolution("timeout", "身份查询预算已截止；未完成所有昵称变体搜索。")
            elif reason and row["resolution"]["status"] == "missing":
                row["resolution"]["reason"] = reason
            row["candidates"] = row["resolution"].get("candidates", [])
            row["status"] = row["resolution"]["status"]
        occurrences = {}
        for row in rows:
            decision = row["resolution"]
            if decision["status"] in ("resolved", "manual-confirmed"):
                occurrences.setdefault(decision["player_id"], []).append(row)
        for player_id, conflict_rows in occurrences.items():
            if len(conflict_rows) > 1:
                for row in conflict_rows:
                    row["resolution"] = _empty_resolution("conflict", f"玩家 ID {player_id} 同时对应多个名单位置，全部暂不确认。", row["candidates"])
                    row["status"] = "conflict"
        players = []
        for row in rows:
            decision = row["resolution"]
            if decision["status"] in ("resolved", "manual-confirmed"):
                players.append({"id": decision["player_id"], "name": decision["name"],
                                "team": row["team"], "index": row["index"],
                                "identity_source": "manual-confirmed" if decision["status"] == "manual-confirmed" else scan.source + "-exact"})
        scan.finished = True
        self._emit(scan, {"type": "resolved", "players": players, "rows": rows,
                          "revision": 1, "final": True,
                          "identity_status": "screen_name_match_pending_log_id" if scan.source == "screen" else "manual_input",
                          "message": ((f"已匹配 {len(players)} 个公开玩家 ID；屏幕名单仍待游戏日志核验。"
                                       if scan.source == "screen" else f"已取得 {len(players)} 个数字 ID；其余行待校正。")
                                      + (" 本轮姓名搜索上限（8次），部分待确认。" if scan.request_limit_reached else "")),
                          "lookup_elapsed": time.monotonic() - scan.started,
                          "lookup_budget": self.lookup_budget, "total_budget": self.total_budget})
        self._condition.notify_all()
