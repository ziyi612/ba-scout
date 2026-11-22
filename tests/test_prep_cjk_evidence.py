"""Fabricated WinRT word boxes and model readings, through the normal roster and controller."""
from concurrent.futures import Future
from queue import Queue
from time import monotonic, sleep
import unittest

from ba_scout.prep_engine import PrepController
from ba_scout.prep_roster import extract_prep_roster
from ba_scout.request_gate import RequestGate
from tests.test_prep_roster import calibrated_frame


DIGITS = "雾谷38"
LONG = "雪岸松风照晚舟"
VIEWS = ("colour_3x", "light_text_140_3x", "light_text_120_3x", "soft_text_185_3x")


def segment(text, start=0, shift=0):
    return [dict(text=char, x=1516 + index * 13 + shift, y=317, width=12.5, height=13, roi_index=5)
            for index, char in enumerate(text, start)]


def capture(name, views, **model_changes):
    frame, regions = calibrated_frame()
    original_tiles = frame["region_layout"]
    frame.update(ok=True, results=[], region_layout=[], auxiliary_names=[
        dict(dict(team="Bravo", index=0, text=name, confidence=.98, clipped=False), **model_changes)])
    for treatment, words in zip(VIEWS, views):
        frame["region_layout"].extend(dict(tile, preprocessing=treatment) for tile in original_tiles)
        frame["results"].append(dict(language="zh-Hans-CN", preprocessing=treatment, lines=[
            dict(text=" ".join(w["text"] for w in words), words=words, roi_index=5)]))
    return frame, regions


def with_digits(gap=0, second_number="38", **model_changes):
    def letters_and_number(number):
        return segment("雾谷") + [dict(text=number, x=1542 + gap, y=317, width=15, height=13, roi_index=5)]
    return capture(DIGITS, [letters_and_number("38"), letters_and_number(second_number)], **model_changes)


def prefix_suffix(prefix=5, start=2, shift=0, **model_changes):
    return capture(LONG, [segment(LONG[:prefix]), segment(LONG[:prefix]), segment(LONG[start:], start, shift)],
                   **model_changes)


def target(frame, regions):
    return extract_prep_roster(frame, regions)["teams"]["Bravo"][0]


class NextCapture:
    """The next outstanding capture proves the previous frame was fully processed."""
    def __init__(self):
        self.pending = Queue()

    def submit_capture_game(self, **options):
        future = Future()
        self.pending.put(future)
        return future


class CjkEvidenceTests(unittest.TestCase):
    def assert_two_captures(self, captured, name, search_results, expected_status):
        frame, regions = captured
        slot = target(frame, regions)
        self.assertEqual(slot.get("visual_name"), name)
        self.assertFalse(slot.get("corroborated_name"))

        source = NextCapture()
        calls = []
        def search(query, timeout):
            calls.append(query)
            return search_results if query == name else []
        controller = PrepController(source, RequestGate(), searcher=search, capture_interval=.01,
                                    lookup_budget=1, total_budget=2)
        self.addCleanup(controller.close)
        controller.start("synthetic-cjk", regions)
        source.pending.get(timeout=2).set_result(frame)
        second = source.pending.get(timeout=2)
        first_events = controller.poll_events()
        self.assertFalse(any(row.get("stable_name") or row.get("corroborated_name")
                             for event in first_events for row in event.get("rows", [])))
        self.assertEqual(calls, [])
        second.set_result(frame)
        source.pending.get(timeout=2)
        deadline = monotonic() + 2
        events = []
        while monotonic() < deadline:
            events.extend(controller.poll_events())
            if any(row["status"] == expected_status for e in events if e["type"] == "resolved"
                   for row in e["rows"] if row["team"] == "Bravo" and row["index"] == 0):
                break
            sleep(.005)
        shown = [row for e in events if e["type"] == "resolved" for row in e["rows"]
                 if row["team"] == "Bravo" and row["index"] == 0]
        self.assertTrue(any(r.get("stable_name") == name for r in shown), shown)
        self.assertTrue(any(r["status"] == expected_status for r in shown), shown)
        self.assertIn(name, calls)
        self.assertFalse(any(e.get("players") for e in events), "Unconfirmed or duplicate account was published")

    def test_touching_han_and_digits_require_two_captures_and_preserve_ambiguous_account(self):
        duplicate = [{"id": 101, "name": DIGITS}, {"id": 102, "name": DIGITS}]
        self.assert_two_captures(with_digits(), DIGITS, duplicate, "ambiguous")

    def test_overlapping_prefix_suffix_require_two_captures_and_ignore_similar_account(self):
        nearby = [{"id": 103, "name": LONG[:-1] + "桥"}]
        self.assert_two_captures(prefix_suffix(), LONG, nearby, "unmatched")

    def test_conflicting_digit_pixels_block_an_exact_account_but_keep_other_slot_live(self):
        for reason, sample in (("real gap", with_digits(gap=6)),
                               ("different digit", with_digits(second_number="39"))):
            with self.subTest(reason=reason):
                frame, regions = sample
                # The second, unrelated name has incomplete Chinese coverage
                # (three missing WinRT glyphs), but no observed internal conflict.
                good = "枫溪云帆忆故川"
                tile = next(t for t in frame["region_layout"] if t["roi_index"] == 6)
                y = tile["source_bbox"][1] + 2
                for result in frame["results"]:
                    glyphs = [dict(w, y=y, roi_index=6) for w in segment(good[:4])]
                    result["lines"].append(dict(text=" ".join(w["text"] for w in glyphs), words=glyphs, roi_index=6))
                frame["auxiliary_names"].append(dict(team="Bravo", index=1, text=good, confidence=.99, clipped=False))
                self.assertFalse(extract_prep_roster(frame, regions)["teams"]["Bravo"][1].get("visual_name"))

                calls = []
                def exact_website(query, timeout):
                    calls.append(query)
                    accounts = {DIGITS: 91, good: 73}
                    return [{"id": accounts[query], "name": query}] if query in accounts else []
                source = NextCapture()
                controller = PrepController(source, RequestGate(), searcher=exact_website, capture_interval=.01,
                                            lookup_budget=.35, total_budget=.5)
                try:
                    controller.start("synthetic-cjk-conflict", regions)
                    for _ in range(2):
                        source.pending.get(timeout=2).set_result(frame)
                    source.pending.get(timeout=2)
                    deadline = monotonic() + 2
                    events = []
                    while monotonic() < deadline:
                        events.extend(controller.poll_events())
                        if any(e.get("initial_report_ready") for e in events):
                            break
                        sleep(.005)
                    self.assertTrue(any(e.get("initial_report_ready") for e in events))
                    ids = {p["id"] for e in events for p in e.get("players", [])}
                    self.assertEqual(ids, {73}, "A conflicting spelling borrowed the website's only exact account")
                    expected = {DIGITS, "雾谷 38", "雾 谷 38"} if reason == "real gap" else {DIGITS, "雾谷39"}
                    self.assertTrue(expected.issubset(calls), calls)
                finally:
                    controller.close()

    def test_two_repetitions_of_one_treatment_do_not_count_as_two_visual_views(self):
        # Repeating a language query over the same pixels supplies no additional view.
        for frame, regions in (with_digits(), prefix_suffix()):
            frame["results"][1]["preprocessing"] = frame["results"][0]["preprocessing"]
            name = frame["auxiliary_names"][0]["text"]
            with self.subTest(name=name):
                self.assertNotEqual(target(frame, regions).get("visual_name"), name)

    def test_han_digit_join_requires_tight_boxes_matching_digits_and_complete_model_reading(self):
        for reason, sample in (
            ("visible gap", with_digits(gap=6)),
            ("digit changed between views", with_digits(second_number="39")),
            ("model crop clipped", with_digits(clipped=True)),
            ("model disagrees on suffix", with_digits(text="雾谷39")),
        ):
            with self.subTest(reason=reason):
                row = target(*sample)
                self.assertNotEqual(DIGITS, row.get("visual_name"))
                self.assertNotEqual(DIGITS, row.get("corroborated_name"))

    def test_han_fragments_must_cover_full_name_and_share_the_same_glyph_positions(self):
        for reason, sample in (
            ("different pixels", prefix_suffix(shift=6)),
            ("unseen internal character", prefix_suffix(prefix=3, start=4)),
            ("overlap of only one glyph", prefix_suffix(prefix=4, start=3)),
            ("two competing complete spellings", prefix_suffix(confidence=.955, alternate_text=LONG[:-1] + "桥",
                                                               alternate_confidence=.953, alternate_clipped=False)),
        ):
            with self.subTest(reason=reason):
                row = target(*sample)
                self.assertNotEqual(LONG, row.get("visual_name"))
                self.assertNotEqual(LONG, row.get("corroborated_name"))
