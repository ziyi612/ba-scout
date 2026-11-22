"""Bounded queue/lifecycle tests; optional WinRT test reads an existing PNG."""
from __future__ import annotations

import hashlib
import json
import copy
import os
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest

from ba_scout.screen_ocr import ScreenOCR


SCRATCH = Path(__file__).resolve().parents[1] / "work" / "screen-ocr-tests"


class ControlledOCR(ScreenOCR):
    def __init__(self, directory, **kwargs):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests = []
        self.stops = 0
        super().__init__(directory, **kwargs)

    def _roundtrip(self, payload, deadline):
        self.requests.append(payload)
        self.entered.set()
        while not self.release.wait(0.005):
            if self._closed.is_set():
                return {"ok": False, "status": "closed", "request_id": payload["id"]}
            if time.monotonic() >= deadline:
                raise TimeoutError("bounded fake request")
        return {"ok": True, "status": "ok", "request_id": payload["id"]}

    def _stop_worker(self):
        self.stops += 1


class ScreenOCRTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.directory.cleanup)

    def client(self, **kwargs):
        client = ControlledOCR(self.directory.name, **kwargs)
        self.addCleanup(client.close)
        return client

    def test_submit_is_nonblocking_and_defaults_are_game_only(self):
        client = self.client()
        started = time.monotonic()
        future = client.submit_capture_game()
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertTrue(client.entered.wait(1))
        self.assertFalse(future.done())
        self.assertEqual(client.requests[0]["op"], "capture")
        self.assertTrue(client.requests[0]["require_foreground"])
        self.assertFalse(client.requests[0]["capture_only"])
        client.release.set()
        self.assertTrue(future.result(1)["ok"])

    def test_native_capture_accepts_game_and_own_overlay_focus_only(self):
        # The worker defines the same Win32 eligibility check on both sides of
        # PrintWindow. Hidden native windows exercise ownership without taking focus.
        env = dict(os.environ, TEMP=self.directory.name, TMP=self.directory.name,
                   BA_CAPTURE_WORKER=str(SCRATCH.parents[1] / "ba_scout" / "ocr_worker.ps1"),
                   BA_CAPTURE_CLIENT=str(os.getpid()))
        command = r"""
$ErrorActionPreference = 'Stop'
& $env:BA_CAPTURE_WORKER -TempRoot $env:TEMP -ClientPid ([int]$env:BA_CAPTURE_CLIENT)
Add-Type -AssemblyName System.Windows.Forms
$game = New-Object System.Windows.Forms.Form
$overlay = New-Object System.Windows.Forms.Form
try {
    $flags = [Reflection.BindingFlags]'Static,NonPublic'
    $test = [BAScoutOCR.NativeCapture].GetMethod('EligibleForeground',$flags)
    $otherClient = [int]$env:BA_CAPTURE_CLIENT
    $gameChild = New-Object System.Windows.Forms.Button
    $game.Controls.Add($gameChild)
    $hwnd = $game.Handle
    $cover = $overlay.Handle
    [ordered]@{
        game = $test.Invoke($null,@($hwnd,$hwnd,$otherClient))
        child = $test.Invoke($null,@($gameChild.Handle,$hwnd,$otherClient))
        overlay = $test.Invoke($null,@($cover,$hwnd,[int]$PID))
        other = $test.Invoke($null,@($cover,$hwnd,$otherClient))
        noFocus = $test.Invoke($null,@([IntPtr]::Zero,$hwnd,[int]$PID))
    } | ConvertTo-Json -Compress
} finally {
    $overlay.Dispose()
    $game.Dispose()
}
"""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            input="", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", timeout=25, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]),
                         {"game": True, "child": True, "overlay": True, "other": False, "noFocus": False})

    def test_secondary_name_view_preserves_dim_strokes_and_marks_crop_edges(self):
        env = dict(os.environ, TEMP=self.directory.name, TMP=self.directory.name,
                   BA_NAME_CS=str(SCRATCH.parents[1] / "ba_scout" / "name_ocr.cs"))
        command = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -Path $env:BA_NAME_CS -ReferencedAssemblies System.Drawing
$image = [Drawing.Bitmap]::new(28,14)
try {
    for ($y=4; $y -le 9; $y++) {
        for ($x=4; $x -le 9; $x++) {
            $image.SetPixel($x,$y,[Drawing.Color]::FromArgb(200,120,35))
        }
    }
    $image.SetPixel(15,7,[Drawing.Color]::White)
    $box = [double[]]@(0,0,1,1)
    $base = [BAScoutOCR.NameRecognizer]::Prepare($image,$box,$false)
    $soft = [BAScoutOCR.NameRecognizer]::Prepare($image,$box,$true)
    # The white anchor ends the 18px crop. The secondary view adds 3px on each side.
    $baseSample = 24*$base.width + [int]($base.width*6.5/18)
    $softSample = 24*$soft.width + [int]($soft.width*9.5/24)
    $image.SetPixel(27,7,[Drawing.Color]::White)
    $touching = [BAScoutOCR.NameRecognizer]::Prepare($image,$box,$true)
    [ordered]@{
        dimStroke = ($base.pixels[$baseSample] -eq -1 -and $soft.pixels[$softSample] -gt -0.7)
        blackBorders = ($soft.pixels[24*$soft.width] -eq -1 -and $soft.pixels[25*$soft.width-1] -eq -1)
        cropFlags = (!$base.clipped -and !$soft.clipped -and $touching.clipped)
    } | ConvertTo-Json -Compress
} finally {
    $image.Dispose()
}
"""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=20,
            env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout), {"dimStroke": True, "blackBorders": True, "cropFlags": True})

    def test_packaged_recognizer_and_primary_fallback_on_identical_pixels(self):
        import shutil
        from tests.prep_acceptance import make_frames, NAMES
        scratch = Path(self.directory.name)
        regions = {team: {"rows": [[x, .22 + i*.14, .14, .025] for i in range(5)]}
                   for team, x in (("Alpha", .10), ("Bravo", .60))}
        make_frames(scratch, regions)
        image = scratch / "5.png"
        with ScreenOCR(scratch) as client:
            result = client.submit_extract(image, regions=regions, timeout=18).result(20)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result.get("auxiliary_status"), "ready")
            readings = [r for r in result["auxiliary_names"] if r["team"] == "Bravo"]
            self.assertEqual([r["index"] for r in readings], list(range(5)))
            self.assertEqual([r["text"] for r in readings], list(NAMES))
            self.assertTrue(all(r["confidence"] > .9 and not r["clipped"] for r in readings))
            self.assertTrue(all("alternate_text" not in r for r in readings if r["confidence"] > .96))
            worker = client.worker_script
        # An unavailable secondary model must not take down Windows OCR.
        for name in ("ocr_worker.ps1", "name_ocr.ps1", "name_ocr.cs"):
            shutil.copyfile(worker.with_name(name), scratch / name)
        with ScreenOCR(scratch) as client:
            client.worker_script = scratch / worker.name
            result = client.submit_extract(image, regions=regions, timeout=18).result(20)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["auxiliary_status"], "unavailable")
            self.assertEqual(result["auxiliary_names"], [])
            self.assertTrue(result["words"])

    def test_calibration_flags_survive_submission(self):
        client = self.client()
        client.release.set()
        client.submit_capture_game(require_foreground=False, capture_only=True).result(1)
        self.assertFalse(client.requests[0]["require_foreground"])
        self.assertTrue(client.requests[0]["capture_only"])

    def test_warmup_is_nonblocking_and_has_no_capture_or_image_request(self):
        client = self.client()
        started = time.monotonic()
        future = client.submit_warmup()
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertTrue(client.entered.wait(1))
        self.assertFalse(future.done())
        self.assertEqual(set(client.requests[0]), {"id", "op", "languages"})
        self.assertEqual(client.requests[0]["op"], "warmup")
        self.assertEqual(client.requests[0]["languages"], ["en-GB", "zh-Hans-CN", "ru-RU"])
        client.release.set()
        self.assertTrue(future.result(1)["ok"])

    def test_warmup_validates_languages_and_timeout_before_starting(self):
        client = self.client()
        for languages in [[], ["en-GB;"], ["en-GB"] * 5, [None], "en-GB"]:
            self.assertEqual(client.submit_warmup(languages=languages).result(1)["status"], "invalid_input")
        for timeout in [0, -1, 31, True, float("nan"), float("inf")]:
            self.assertEqual(client.submit_warmup(timeout=timeout).result(1)["status"], "invalid_input")
        self.assertEqual(client.requests, [])

    def test_queued_warmup_can_be_cancelled_and_uses_the_bounded_queue(self):
        client = self.client(queue_size=1)
        first = client.submit_capture_game()
        self.assertTrue(client.entered.wait(1))
        pending = client.submit_warmup()
        self.assertEqual(client.submit_warmup().result(1)["status"], "busy")
        self.assertTrue(pending.cancel())
        client.release.set()
        self.assertTrue(first.result(1)["ok"])
        client._thread.join(0.12)
        self.assertEqual(len(client.requests), 1)

    def test_warmup_timeout_stops_only_the_private_worker(self):
        client = self.client()
        self.assertEqual(client.submit_warmup(timeout=.025).result(1)["status"], "timeout")
        client.release.set()
        self.assertTrue(client.submit_warmup().result(1)["ok"])
        self.assertGreaterEqual(client.stops, 1)

    def test_closed_client_rejects_warmup_without_starting(self):
        client = self.client()
        client.close()
        self.assertEqual(client.submit_warmup().result(1)["status"], "closed")
        self.assertEqual(client.requests, [])

    def test_queue_is_bounded_and_cancelled_waiter_never_executes(self):
        client = self.client(queue_size=1)
        first = client.submit_capture_game()
        self.assertTrue(client.entered.wait(1))
        waiting = client.submit_capture_game()
        busy = client.submit_capture_game()
        self.assertEqual(busy.result(1)["status"], "busy")
        self.assertTrue(waiting.cancel())
        client.release.set()
        self.assertTrue(first.result(1)["ok"])
        client._thread.join(0.12)
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(waiting.cancelled())

    def test_running_future_cannot_be_cancelled(self):
        client = self.client()
        future = client.submit_capture_game()
        self.assertTrue(client.entered.wait(1))
        self.assertFalse(future.cancel())
        client.release.set()
        self.assertTrue(future.result(1)["ok"])

    def test_waiting_time_counts_against_deadline(self):
        client = self.client()
        first = client.submit_capture_game(timeout=1)
        self.assertTrue(client.entered.wait(1))
        expired = client.submit_capture_game(timeout=0.02)
        time.sleep(0.04)
        client.release.set()
        self.assertTrue(first.result(1)["ok"])
        self.assertEqual(expired.result(1)["status"], "timeout")
        self.assertEqual(len(client.requests), 1)

    def test_timeout_stops_private_worker_and_next_request_can_run(self):
        client = self.client()
        future = client.submit_capture_game(timeout=0.025)
        self.assertEqual(future.result(1)["status"], "timeout")
        client.release.set()
        self.assertTrue(client.submit_capture_game().result(1)["ok"])
        self.assertEqual(client.stops, 1)

    def test_timeout_result_is_not_delayed_by_process_cleanup(self):
        client = self.client()
        original_stop = client._stop_worker
        cleanup_entered = threading.Event()
        cleanup_release = threading.Event()

        def slow_stop():
            cleanup_entered.set()
            cleanup_release.wait(1)
            original_stop()

        client._stop_worker = slow_stop
        future = client.submit_capture_game(timeout=0.025)
        try:
            self.assertEqual(future.result(0.5)["status"], "timeout")
            self.assertTrue(cleanup_entered.wait(0.5))
            self.assertFalse(cleanup_release.is_set())
        finally:
            cleanup_release.set()

    def test_close_resolves_running_and_queued_requests(self):
        client = self.client()
        first = client.submit_capture_game()
        self.assertTrue(client.entered.wait(1))
        second = client.submit_capture_game()
        client.close()
        self.assertEqual(first.result(1)["status"], "closed")
        self.assertEqual(second.result(1)["status"], "closed")
        self.assertEqual(client.submit_capture_game().result(1)["status"], "closed")

    def test_close_does_not_revive_cancelled_queued_future(self):
        client = self.client()
        first = client.submit_capture_game()
        self.assertTrue(client.entered.wait(1))
        second = client.submit_capture_game()
        second.cancel()
        client.close()
        self.assertEqual(first.result(1)["status"], "closed")
        client._thread.join(1)
        self.assertTrue(second.cancelled())
        self.assertFalse(client._thread.is_alive())

    def test_concurrent_close_and_submission_never_strand_future(self):
        for _ in range(20):
            client = self.client()
            closing = threading.Thread(target=client.close)
            closing.start()
            future = client.submit_capture_game(timeout=0.1)
            closing.join(1)
            self.assertEqual(future.result(1)["status"], "closed")

    def test_invalid_timeout_never_starts_worker(self):
        client = self.client()
        for invalid in [0, -1, 31, float("inf"), float("nan"), True, "8"]:
            self.assertEqual(client.submit_capture_game(timeout=invalid).result(1)["status"], "invalid_input")
        self.assertEqual(client.requests, [])

    def test_rejects_non_f_temporary_directory_before_creation(self):
        with self.assertRaises(ValueError):
            ScreenOCR(r"C:\must-not-create-ba-scout-ocr")

    def test_rejects_missing_non_png_and_c_input(self):
        client = self.client()
        for path in [r"C:\Windows\sample.png", Path(self.directory.name) / "missing.png", Path(self.directory.name) / "x.jpg"]:
            self.assertEqual(client.submit_extract(path).result(1)["status"], "invalid_input")
        self.assertEqual(client.requests, [])

    def test_rejects_invalid_languages_and_capture_switches(self):
        client = self.client()
        for languages in [[], ["en-GB;"] , ["en-GB"] * 5, [7], "en-GB"]:
            self.assertEqual(client.submit_capture_game(languages=languages).result(1)["status"], "invalid_input")
        self.assertEqual(client.submit_capture_game(require_foreground=1).result(1)["status"], "invalid_input")

    @staticmethod
    def name_regions():
        return {team: {"rows": [[x, .1 + i * .1, .25, .05] for i in range(5)]}
                for team, x in (("Alpha", .1), ("Bravo", .6))}

    def test_regions_are_normalized_copied_and_forwarded_to_capture(self):
        client = self.client()
        regions = self.name_regions()
        future = client.submit_capture_game(regions=regions)
        self.assertTrue(client.entered.wait(1))
        regions["Alpha"]["rows"][0][0] = .8
        payload = client.requests[0]["regions"]
        self.assertEqual(len(payload), 10)
        self.assertEqual(payload[0], {"team": "Alpha", "index": 0, "bbox": [.1, .1, .25, .05]})
        self.assertEqual(payload[5]["team"], "Bravo")
        client.release.set()
        self.assertTrue(future.result(1)["ok"])

    def test_regions_accept_the_existing_equal_row_block_format(self):
        regions = ScreenOCR._regions({"Alpha": [.1, .1, .3, .5], "Bravo": {"bbox": [.6, .1, .3, .5], "rows": 5}})
        self.assertEqual(len(regions), 10)
        self.assertAlmostEqual(regions[4]["bbox"][1], .5)
        self.assertAlmostEqual(regions[9]["bbox"][3], .1)
        self.assertIsNone(ScreenOCR._regions(None))

    def test_invalid_overlapping_and_partial_regions_never_start_capture(self):
        client = self.client()
        valid = self.name_regions()
        bad_values = [{}, {"Alpha": valid["Alpha"]}, {"Alpha": {"rows": []}, "Bravo": valid["Bravo"]}]
        for value in ([float("nan"), .1, .2, .1], [True, .1, .2, .1], [.9, .1, .2, .1], [.1, .1, 0, .1]):
            bad = copy.deepcopy(valid)
            bad["Alpha"]["rows"][0] = value
            bad_values.append(bad)
        overlap = copy.deepcopy(valid)
        overlap["Bravo"]["rows"][0] = overlap["Alpha"]["rows"][0][:]
        bad_values.append(overlap)
        for regions in bad_values:
            with self.subTest(regions=regions):
                result = client.submit_capture_game(regions=regions).result(1)
                self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(client.requests, [])

    @unittest.skipUnless(os.environ.get("BA_SCOUT_TEST_OCR_REGIONS_IMAGE"), "Set an existing preparation-layout PNG for regions OCR")
    def test_real_preparation_regions_preserve_source_coordinates_and_name_variants(self):
        image = Path(os.environ["BA_SCOUT_TEST_OCR_REGIONS_IMAGE"])
        before = hashlib.sha256(image.read_bytes()).hexdigest()
        width, height = 1917, 1078
        pixel_boxes = [[x, 315 + 152 * i, box_width, 20]
                       for x, box_width in ((229, 177), (1510, 176)) for i in range(5)]
        regions = {team: {"rows": [[x / width, y / height, w / width, h / height]
                                    for x, y, w, h in pixel_boxes[start:start + 5]]}
                   for team, start in (("Alpha", 0), ("Bravo", 5))}
        with ScreenOCR(self.directory.name) as client:
            result = client.submit_extract(image, regions=regions).result(10)
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual((result["image_width"], result["image_height"]), (width, height))
            self.assertEqual(Path(result["image_path"]).resolve(), image.resolve())
            self.assertEqual(result["ocr_method"], "calibrated_name_regions")
            self.assertEqual(result["coordinate_system"], "original_image_pixels")
            self.assertEqual(result["region_scale"], 3)
            self.assertEqual(len(result["region_layout"]), 40)
            self.assertEqual(set(result["preprocessing"]), {"colour_3x", "light_text_140_3x", "light_text_120_3x", "soft_text_185_3x"})
            self.assertEqual(len(result["results"]), 4 * len(result["languages"]))
            self.assertLess(result["client_elapsed_seconds"], 8)
            self.assertTrue(result["words"])
            for word in result["words"]:
                x, y, w, h = pixel_boxes[word["roi_index"]]
                self.assertGreaterEqual(word["x"], x - 1e-6)
                self.assertGreaterEqual(word["y"], y - 1e-6)
                self.assertLessEqual(word["x"] + word["width"], x + w + 1e-6)
                self.assertLessEqual(word["y"] + word["height"], y + h + 1e-6)
            english = next(r for r in result["results"] if r["language"] == "en-GB" and r["preprocessing"] == "light_text_140_3x")
            lines = {line["roi_index"]: line["text"] for line in english["lines"]}
            self.assertTrue(lines)
            # Optional ground truth stays local, outside the public repository.
            expected_names = json.loads(os.environ.get('BA_SCOUT_TEST_OCR_EXPECTED_NAMES', '{}'))
            for index, name in expected_names.items():
                self.assertEqual(lines[int(index)], name)
            self.assertEqual(list(Path(self.directory.name).glob("ocr-regions-*.png")), [])
        self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), before)

    @unittest.skipUnless(os.environ.get("BA_SCOUT_TEST_OCR_IMAGE"), "Set an existing PNG for optional real WinRT OCR")
    def test_existing_png_with_real_worker_and_original_coordinates(self):
        image = Path(os.environ["BA_SCOUT_TEST_OCR_IMAGE"])
        before = hashlib.sha256(image.read_bytes()).hexdigest()
        with ScreenOCR(self.directory.name) as client:
            warmup = client.submit_warmup().result(10)
            self.assertTrue(warmup["ok"], warmup.get("error"))
            self.assertEqual(warmup["operation"], "warmup")
            self.assertNotIn("image_path", warmup)
            self.assertEqual(warmup["words"], [])
            self.assertEqual(warmup["results"], [])
            self.assertEqual(warmup["engine_cache"]["created"], len(warmup["languages"]))
            self.assertEqual(list(Path(self.directory.name).glob("*.png")), [])
            repeated = client.submit_warmup().result(10)
            self.assertTrue(repeated["ok"], repeated.get("error"))
            self.assertEqual(repeated["languages"], warmup["languages"])
            self.assertEqual(repeated["engine_cache"]["created"], 0)
            self.assertEqual(repeated["engine_cache"]["reused"], len(warmup["languages"]))
            for _ in range(2):
                result = client.submit_extract(image).result(10)
                self.assertTrue(result["ok"], result.get("error"))
                self.assertGreater(len(result["results"]), 0)
                self.assertEqual(result["languages"], warmup["languages"])
                self.assertEqual(result["engine_cache"]["created"], 0)
                self.assertEqual(result["engine_cache"]["reused"], len(warmup["languages"]))
                self.assertEqual(result["coordinate_system"], "original_image_pixels")
                self.assertFalse(result["confidence_available"])
                self.assertTrue(result["image_path"].lower().startswith("f:"))
                self.assertLess(result["client_elapsed_seconds"], 8)
                for word in result["words"]:
                    self.assertGreaterEqual(word["x"], 0)
                    self.assertGreaterEqual(word["y"], 0)
                    self.assertLessEqual(word["x"] + word["width"], result["image_width"] + 1)
                    self.assertLessEqual(word["y"] + word["height"], result["image_height"] + 1)
        self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
