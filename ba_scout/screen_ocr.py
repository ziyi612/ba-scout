"""Nonblocking, bounded Windows OCR requests through a private hidden worker.

Only the verified BrokenArrow window can be captured. No desktop capture,
activation, game-memory access or PIL dependency is used. Output remains in
the software folder; explicitly selected PNG inputs are read-only.
WinRT word rectangles are reported in original-image pixel coordinates.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from concurrent.futures import Future
import itertools
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from .settings import ROOT, app_path, read_path


DEFAULT_LANGUAGES = ("en-GB", "zh-Hans-CN", "ru-RU")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
_MAX_IMAGE_BYTES = 32 * 1024 * 1024




def _failure(status: str, error: str, request_id: str | None = None) -> dict:
    return {"ok": False, "status": status, "error": error, "request_id": request_id,
            "results": [], "words": []}


class ScreenOCR:
    """One worker and one bounded serial queue; submit methods never wait for OCR.

    ``Future.cancel()`` skips requests that have not started. A running request
    remains bounded by its submission-time timeout, and its result may simply
    be discarded by the caller. ``close`` terminates only this client's worker.
    """

    def __init__(self, temp_dir: str | Path | None = None, *, queue_size: int = 2):
        if queue_size < 1 or queue_size > 16:
            raise ValueError("queue_size must be between 1 and 16")
        self.temp_dir = app_path(temp_dir if temp_dir is not None else ROOT / "tmp" / "ocr")
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.worker_script = Path(__file__).with_name("ocr_worker.ps1").resolve()
        self._jobs: queue.Queue = queue.Queue(maxsize=queue_size)
        self._counter = itertools.count(1)
        self._closed = threading.Event()
        self._state_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._responses: queue.Queue | None = None
        self._stderr = deque(maxlen=12)
        self._metadata: dict = {}
        self._thread = threading.Thread(target=self._run, name="ba-scout-ocr", daemon=True)
        self._thread.start()

    def _submit(self, payload: dict, timeout: float) -> Future:
        future = Future()
        request_id = str(next(self._counter))
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            future.set_result(_failure("invalid_input", "timeout must be finite and within 0..30 seconds", request_id))
            return future
        payload = dict(payload, id=request_id)
        submitted = time.monotonic()
        with self._state_lock:
            if self._closed.is_set():
                future.set_result(_failure("closed", "OCR client is closed", request_id))
                return future
            try:
                self._jobs.put_nowait((future, payload, submitted, submitted + float(timeout)))
            except queue.Full:
                future.set_result(_failure("busy", "OCR request queue is full", request_id))
        return future

    @staticmethod
    def _languages(languages) -> list[str]:
        chosen = list(DEFAULT_LANGUAGES if languages is None else languages)
        if not chosen or len(chosen) > 4 or any(not isinstance(tag, str) or not _LANGUAGE.fullmatch(tag) for tag in chosen):
            raise ValueError("Supply one to four valid OCR language tags")
        return list(dict.fromkeys(chosen))

    @staticmethod
    def _regions(regions) -> list[dict] | None:
        """Copy ten normalized name boxes; no default layout is guessed."""
        if regions is None:
            return None
        if not isinstance(regions, Mapping) or set(regions) != {"Alpha", "Bravo"}:
            raise ValueError("regions must explicitly contain Alpha and Bravo")
        flattened = []

        def box(value):
            if isinstance(value, Mapping):
                value = [value.get(k) for k in ("x", "y", "width", "height")]
            if not isinstance(value, (list, tuple)) or len(value) != 4 or any(
                type(n) not in (int, float) or not math.isfinite(n) for n in value
            ):
                raise ValueError("Each region needs four finite normalized coordinates")
            x, y, width, height = map(float, value)
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > 1 + 1e-9 or y + height > 1 + 1e-9:
                raise ValueError("Each region must remain within the original image")
            return [x, y, width, height]

        for team in ("Alpha", "Bravo"):
            config = regions[team]
            rows = config.get("rows") if isinstance(config, Mapping) else None
            if isinstance(rows, (list, tuple)):
                boxes = rows
            elif isinstance(config, (list, tuple)) and len(config) == 5:
                boxes = config
            else:
                if isinstance(config, Mapping) and rows not in (None, 5):
                    raise ValueError("Each team needs five calibrated name rows")
                x, y, width, height = box(config.get("bbox") if isinstance(config, Mapping) else config)
                boxes = [[x, y + i * height / 5, width, height / 5] for i in range(5)]
            if len(boxes) != 5:
                raise ValueError("Each team needs five calibrated name rows")
            for index, value in enumerate(boxes):
                normalized = box(value)
                for previous in flattened:
                    a, b = previous["bbox"], normalized
                    if min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]) > 1e-9 and min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]) > 1e-9:
                        raise ValueError("Calibrated name regions must not overlap")
                flattened.append({"team": team, "index": index, "bbox": normalized})
        return flattened

    def submit_extract(self, image_path: str | Path, *, languages=None, regions=None, timeout: float = 8) -> Future:
        try:
            path = read_path(image_path)
            if path.suffix.lower() != ".png" or not path.is_file():
                raise ValueError("extract requires an existing PNG at an absolute path")
            if path.stat().st_size > _MAX_IMAGE_BYTES:
                raise ValueError("PNG exceeds the 32 MiB input limit")
            payload = {"op": "extract", "image_path": str(path), "languages": self._languages(languages),
                       "regions": self._regions(regions)}
        except (OSError, TypeError, ValueError) as exc:
            future = Future()
            future.set_result(_failure("invalid_input", str(exc)))
            return future
        return self._submit(payload, timeout)

    def submit_warmup(self, *, languages=None, timeout: float = 8) -> Future:
        """Load the private worker and language engines without reading a frame.

        Schedule this during application idle time and inspect the Future later.
        It shares the bounded OCR queue and submission-time deadline; cancelling
        a queued warmup works exactly like cancelling a queued extraction.
        No window enumeration, capture, image input, or network request occurs.
        """
        try:
            payload = {"op": "warmup", "languages": self._languages(languages)}
        except (TypeError, ValueError) as exc:
            future = Future()
            future.set_result(_failure("invalid_input", str(exc)))
            return future
        return self._submit(payload, timeout)

    def submit_capture_game(self, *, require_foreground: bool = True, capture_only: bool = False,
                            languages=None, regions=None, timeout: float = 8) -> Future:
        try:
            if type(require_foreground) is not bool or type(capture_only) is not bool:
                raise ValueError("Capture switches must be booleans")
            payload = {"op": "capture", "require_foreground": require_foreground,
                       "capture_only": capture_only, "languages": self._languages(languages),
                       "regions": self._regions(regions)}
        except (TypeError, ValueError) as exc:
            future = Future()
            future.set_result(_failure("invalid_input", str(exc)))
            return future
        return self._submit(payload, timeout)

    def _start_worker(self):
        with self._process_lock:
            if self._closed.is_set():
                raise RuntimeError("OCR client is closed")
            if self._process is not None and self._process.poll() is None:
                return self._process, self._responses
            if os.name != "nt" or not self.worker_script.is_file():
                raise RuntimeError("Windows OCR worker is unavailable")
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            env = os.environ.copy()
            env.update(TEMP=str(self.temp_dir), TMP=str(self.temp_dir), PYTHONDONTWRITEBYTECODE="1")
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            process = subprocess.Popen(
                [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(self.worker_script), "-TempRoot", str(self.temp_dir), "-ClientPid", str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, cwd=str(self.temp_dir), startupinfo=startup,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            responses = queue.Queue()
            self._process, self._responses = process, responses
            self._stderr.clear()
            threading.Thread(target=self._read_stdout, args=(process, responses), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
            return process, responses

    def _read_stdout(self, process, responses):
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    self._stderr.append(line.strip()[:300])
                    continue
                if isinstance(value, dict):
                    responses.put(value)
        except (OSError, ValueError):
            pass
        finally:
            process.stdout.close()
            responses.put({"type": "worker_eof"})

    def _read_stderr(self, process):
        try:
            for line in process.stderr:
                self._stderr.append(line.strip()[:300])
        except (OSError, ValueError):
            pass
        finally:
            process.stderr.close()

    def _stop_worker(self):
        with self._process_lock:
            process, self._process = self._process, None
            self._responses = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.7)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.7)
                except subprocess.TimeoutExpired:
                    pass
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass

    def _roundtrip(self, payload: dict, deadline: float) -> dict:
        process, responses = self._start_worker()
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()
        while not self._closed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OCR request exceeded its submission-time budget")
            try:
                result = responses.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("OCR worker did not finish within the request budget") from None
            if result.get("type") == "ready":
                self._metadata = result
                continue
            if result.get("type") in {"fatal", "worker_eof"}:
                raise RuntimeError(result.get("error") or "OCR worker exited: " + " | ".join(self._stderr))
            if str(result.get("id")) == payload["id"]:
                result["request_id"] = result.pop("id")
                return result
        return _failure("closed", "OCR client was closed", payload["id"])

    def _run(self):
        while not self._closed.is_set():
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            future, payload, submitted, deadline = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if time.monotonic() >= deadline:
                    result = _failure("timeout", "Request expired while waiting in the OCR queue", payload["id"])
                else:
                    result = self._roundtrip(payload, deadline)
            except TimeoutError as exc:
                result = _failure("timeout", str(exc), payload["id"])
                result["client_elapsed_seconds"] = round(time.monotonic() - submitted, 4)
                # Publish the deadline result before process cleanup so callers
                # can honor their UI deadline even if Windows delays termination.
                future.set_result(result)
                self._stop_worker()
                continue
            except Exception as exc:
                self._stop_worker()
                result = _failure("closed" if self._closed.is_set() else "worker_error", str(exc)[:1200], payload["id"])
            result["client_elapsed_seconds"] = round(time.monotonic() - submitted, 4)
            future.set_result(result)
        while True:
            try:
                future, payload, _, _ = self._jobs.get_nowait()
            except queue.Empty:
                break
            if future.set_running_or_notify_cancel():
                future.set_result(_failure("closed", "OCR client was closed", payload["id"]))

    def close(self):
        with self._state_lock:
            self._closed.set()
        self._stop_worker()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def extract(image_path: str | Path, *, temp_dir=None, languages=None, regions=None, timeout: float = 8) -> dict:
    """Blocking convenience for offline validation; use ScreenOCR from Tk code."""
    with ScreenOCR(temp_dir) as client:
        return client.submit_extract(image_path, languages=languages, regions=regions, timeout=timeout).result(timeout=timeout + 2)


def capture_game(*, temp_dir=None, require_foreground=True, capture_only=False, languages=None, regions=None, timeout=8) -> dict:
    """Blocking one-shot convenience; it never activates the game window."""
    with ScreenOCR(temp_dir) as client:
        return client.submit_capture_game(require_foreground=require_foreground, capture_only=capture_only,
                                          languages=languages, regions=regions, timeout=timeout).result(timeout=timeout + 2)
