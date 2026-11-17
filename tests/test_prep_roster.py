"""Synthetic calibrated OCR tests; no screen capture, network, or GUI."""
from __future__ import annotations

import json
import math
import unittest
from copy import deepcopy

from ba_scout.prep_roster import extract_prep_roster


REGIONS = {"Alpha": [.1, .1, .3, .5], "Bravo": [.6, .1, .3, .5]}


def word(text, x, y, width=90, height=20):
    return dict(text=text, x=x, y=y, width=width, height=height)


def line(text, x, y):
    return {"text": text, "words": [word(text, x, y)]}


def frame():
    lines = [line(f"Alpha{i}", 120, 120 + i * 100) for i in range(5)]
    lines += [line(f"Bravo{i}", 620, 120 + i * 100) for i in range(5)]
    return {"image_width": 1000, "image_height": 1000,
            "results": [{"language": "en-GB", "lines": lines}]}


def extract(value=None, regions=None, **kwargs):
    return extract_prep_roster(frame() if value is None else value, REGIONS if regions is None else regions, **kwargs)


def calibrated_frame(regions=None):
    """A 1917x1078 calibration captured at 1920x1080, with worker provenance."""
    if regions is None:
        regions = {team: {"rows": [[x / 1917, (315 + i * 150) / 1078,
                                    176 / 1917, 20 / 1078] for i in range(5)]}
                   for team, x in (("Alpha", 229), ("Bravo", 1510))}
    declared, layout, lines = [], [], []
    for team in ("Alpha", "Bravo"):
        for index, bbox in enumerate(regions[team]["rows"]):
            roi_index = len(declared)
            declared.append({"team": team, "index": index, "bbox": list(bbox)})
            x = math.floor(bbox[0] * 1920 + 1e-7)
            y = math.floor(bbox[1] * 1080 + 1e-7)
            right = math.ceil((bbox[0] + bbox[2]) * 1920 - 1e-7)
            bottom = math.ceil((bbox[1] + bbox[3]) * 1080 - 1e-7)
            source = [x, y, right - x, bottom - y]
            layout.append({"roi_index": roi_index, "preprocessing": "colour_3x",
                           "source_bbox": source})
            name = f"{team}{index}"
            item = word(name, x, y, min(100, source[2]), min(15, source[3]))
            item["roi_index"] = roi_index
            lines.append({"text": name, "roi_index": roi_index, "words": [item]})
    value = {"image_width": 1920, "image_height": 1080,
             "ocr_method": "calibrated_name_regions", "coordinate_system": "original_image_pixels",
             "regions": declared, "region_layout": layout,
             "results": [{"language": "en-GB", "preprocessing": "colour_3x", "lines": lines}]}
    return value, regions


def crop_candidates(long_name="VDV Guard :D", short_name="Guard", *, short_bbox=None):
    value, regions = calibrated_frame()
    target = value["results"][0]["lines"][5]
    target["text"] = target["words"][0]["text"] = long_name
    target["words"][0].update(x=1513, y=316, width=165, height=15)
    bbox = short_bbox or [1550, 317, 35, 12]
    short = word(short_name, *bbox)
    short["roi_index"] = 5
    value["results"].append({"language": "ru", "preprocessing": "colour_3x",
                             "lines": [{"text": short_name, "roi_index": 5, "words": [short]}]})
    return value, regions


class PrepRosterTests(unittest.TestCase):
    def test_independently_observed_edge_punctuation_names_both_require_resolution(self):
        for long_name in ("SamplePlayer-", "-SamplePlayer", "[SamplePlayer]",
                          "SamplePlayer_", "SamplePlayer：", "“SamplePlayer”"):
            with self.subTest(long_name=long_name):
                value, regions = crop_candidates(long_name, "SamplePlayer")
                row = extract(value, regions)["rows"][5]
                self.assertTrue(row["queryable"])
                self.assertFalse(row["auto_confirmable"])
                self.assertIsNone(row["name"])
                self.assertEqual({v["text"] for v in row["variants"]}, {long_name, "SamplePlayer"})
                self.assertTrue(all(v["queryable"] and not v["manual_only"] for v in row["variants"]))
                self.assertIn("multiple_variants", row["issue_codes"])
                self.assertNotIn("contained_name_fragment", row["issue_codes"])

    def test_new_edge_punctuation_rule_does_not_keep_letter_digit_or_space_extensions(self):
        for long_name in ("SamplePlayerX", "SamplePlayer7", "SamplePlayer:D",
                          "XSamplePlayer-", "SamplePlayer -", "SamplePlayer+"):
            with self.subTest(long_name=long_name):
                value, regions = crop_candidates(long_name, "SamplePlayer")
                short = next(v for v in extract(value, regions)["rows"][5]["variants"]
                             if v["text"] == "SamplePlayer")
                self.assertFalse(short["queryable"])
                self.assertIn("contained_name_fragment", short["issue_codes"])

    def test_punctuation_is_not_removed_to_invent_an_unobserved_name(self):
        value, regions = calibrated_frame()
        item = value["results"][0]["lines"][5]
        item["text"] = item["words"][0]["text"] = "SamplePlayer-"
        row = extract(value, regions)["rows"][5]
        self.assertEqual([v["text"] for v in row["variants"]], ["SamplePlayer-"])
        self.assertEqual(row["name"], "SamplePlayer-")

    def test_edge_punctuation_variants_do_not_bypass_boundary_checks(self):
        value, regions = crop_candidates("SamplePlayer-", "SamplePlayer",
                                         short_bbox=[1511.9, 317, 100, 12])
        row = extract(value, regions)["rows"][5]
        self.assertFalse(row["queryable"])
        self.assertIn("boundary_crossing", row["issue_codes"])
        self.assertTrue(all(v["manual_only"] for v in row["variants"]))

    def test_edge_punctuation_variants_do_not_bypass_placeholder_single_glyph_or_ellipsis(self):
        for long_name, short_name, issue in (("Player name-", "Player name", "placeholder_name"),
                                              ("君-", "君", "single_cjk_glyph"),
                                              ("SamplePlayer...", "SamplePlayer", "truncated_name")):
            with self.subTest(issue=issue):
                value, regions = crop_candidates(long_name, short_name)
                row = extract(value, regions)["rows"][5]
                self.assertFalse(row["queryable"])
                self.assertIn(issue, row["issue_codes"])
                self.assertTrue(all(not v["queryable"] and v["manual_only"] for v in row["variants"]))
                if issue == "truncated_name":
                    short = next(v for v in row["variants"] if v["text"] == short_name)
                    self.assertIn("contained_name_fragment", short["issue_codes"])

    def test_literal_contained_crop_prefix_suffix_or_middle_is_manual_only(self):
        for long_name, short_name in (("VDV Guard :D", ":D"), ("VDV Guard :D", "Guard"),
                                     ("VDV Guard", "VDV"), ("tide233:D", "tide233")):
            with self.subTest(long_name=long_name, short_name=short_name):
                value, regions = crop_candidates(long_name, short_name)
                row = extract(value, regions)["rows"][5]
                variants = {v["text"]: v for v in row["variants"]}
                short, long = variants[short_name], variants[long_name]
                self.assertTrue(row["queryable"])
                self.assertFalse(short["queryable"])
                self.assertTrue(short["manual_only"])
                self.assertIn("contained_name_fragment", short["issue_codes"])
                self.assertIn("手动", short["reason"])
                self.assertEqual(short["contained_by"], [long_name])
                self.assertTrue(long["queryable"])
                self.assertFalse(long["manual_only"])

    def test_literal_substring_more_than_one_pixel_outside_is_not_suppressed(self):
        # Still inside the actual ROI, but >1 pixel outside the longer word.
        for bbox in ([1650, 317, 29.01, 12], [1550, 318, 35, 14.01]):
            with self.subTest(bbox=bbox):
                value, regions = crop_candidates(short_bbox=bbox)
                row = extract(value, regions)["rows"][5]
                self.assertTrue(row["queryable"])
                self.assertTrue(all(v["queryable"] for v in row["variants"]))
                self.assertNotIn("contained_name_fragment", row["issue_codes"])

    def test_cross_preprocessing_subpixel_suffix_measurement_is_suppressed(self):
        for offset in (1 / 3, 1):
            with self.subTest(offset=offset):
                value, regions = crop_candidates(short_name=":D", short_bbox=[1650, 316 - offset, 20, 12])
                tile = deepcopy(value["region_layout"][5])
                tile["preprocessing"] = "light_text_140_3x"
                value["region_layout"].append(tile)
                value["results"][1]["preprocessing"] = "light_text_140_3x"
                row = extract(value, regions)["rows"][5]
                variants = {v["text"]: v for v in row["variants"]}
                self.assertFalse(variants[":D"]["queryable"])
                self.assertTrue(variants[":D"]["manual_only"])
                self.assertIn("contained_name_fragment", variants[":D"]["issue_codes"])
                self.assertTrue(variants["VDV Guard :D"]["queryable"])
                self.assertTrue(row["queryable"])

    def test_fragment_tolerance_never_allows_a_word_outside_its_source_crop(self):
        value, regions = crop_candidates(short_name=":D", short_bbox=[1650, 314.9, 20, 12])
        # The longer text could contain this suffix under the one-pixel
        # comparison, but source y=315 still rejects the suffix first.
        value["results"][0]["lines"][5]["words"][0]["y"] = 315.5
        row = extract(value, regions)["rows"][5]
        self.assertFalse(row["queryable"])
        self.assertIn("boundary_crossing", row["issue_codes"])
        self.assertNotIn("contained_name_fragment", row["issue_codes"])

    def test_subpixel_measurement_tolerance_never_suppresses_different_text(self):
        value, regions = crop_candidates(short_name=":)", short_bbox=[1650, 315 + 2 / 3, 20, 12])
        row = extract(value, regions)["rows"][5]
        self.assertTrue(all(v["queryable"] for v in row["variants"]))
        self.assertNotIn("contained_name_fragment", row["issue_codes"])

    def test_text_similarity_or_short_length_alone_does_not_suppress(self):
        for short in ("guard", "Gua rd", "Gvard", ":)"):
            with self.subTest(short=short):
                value, regions = crop_candidates(short_name=short)
                row = extract(value, regions)["rows"][5]
                self.assertTrue(all(v["queryable"] for v in row["variants"]))
                self.assertNotIn("contained_name_fragment", row["issue_codes"])

    def test_cjk_whitespace_original_and_compact_remain_queryable(self):
        value, regions = crop_candidates("张 三 Team", "张三")
        row = extract(value, regions)["rows"][5]
        self.assertEqual({v["text"] for v in row["variants"]}, {"张 三 Team", "张三 Team", "张三"})
        self.assertTrue(all(v["queryable"] for v in row["variants"]))
        self.assertNotIn("contained_name_fragment", row["issue_codes"])

    def test_non_crop_matching_substring_candidates_are_unchanged(self):
        value, regions = crop_candidates()
        for key in ("ocr_method", "region_layout", "regions"):
            value.pop(key)
        row = extract(value, regions)["rows"][5]
        self.assertTrue(row["queryable"])
        self.assertTrue(all(v["queryable"] for v in row["variants"]))
        self.assertNotIn("contained_name_fragment", row["issue_codes"])

    def test_unsafe_long_name_does_not_make_fragment_a_safe_alternative(self):
        value, regions = crop_candidates("VDV Guard...", "Guard")
        row = extract(value, regions)["rows"][5]
        self.assertFalse(row["queryable"])
        self.assertIn("truncated_name", row["issue_codes"])
        self.assertTrue(all(v["manual_only"] and not v["queryable"] for v in row["variants"]))
        short = next(v for v in row["variants"] if v["text"] == "Guard")
        self.assertIn("contained_name_fragment", short["issue_codes"])

    def test_suppressed_shared_fragment_does_not_block_other_complete_name(self):
        value, regions = crop_candidates()
        value["results"][0]["lines"][6]["text"] = "Guard"
        value["results"][0]["lines"][6]["words"][0]["text"] = "Guard"
        result = extract(value, regions)
        self.assertTrue(result["rows"][5]["queryable"])
        self.assertTrue(result["rows"][6]["queryable"])
        self.assertEqual(result["rows"][6]["name"], "Guard")
        self.assertNotIn("duplicate_name", result["rows"][5]["issue_codes"])
        self.assertNotIn("duplicate_name", result["rows"][6]["issue_codes"])

    def test_actual_duplicate_long_names_are_still_blocked(self):
        value, regions = crop_candidates()
        value["results"][0]["lines"][6]["text"] = "VDV Guard :D"
        value["results"][0]["lines"][6]["words"][0]["text"] = "VDV Guard :D"
        result = extract(value, regions)
        self.assertFalse(result["rows"][5]["queryable"])
        self.assertFalse(result["rows"][6]["queryable"])
        self.assertIn("duplicate_name", result["rows"][5]["issue_codes"])

    def test_matching_crop_rounding_accepts_fractional_resolution_scale(self):
        value, regions = calibrated_frame()
        before = deepcopy(value)
        self.assertEqual(value["region_layout"][5]["source_bbox"], [1512, 315, 177, 21])
        result = extract(value, regions)
        self.assertEqual(result["queryable_count"], 10)
        self.assertTrue(result["row_geometry_complete"])
        self.assertTrue(result["auto_confirmable"])
        self.assertEqual(result["rows"][5]["bbox"],
                         [regions["Bravo"]["rows"][0][i] * (1920 if i % 2 == 0 else 1080) for i in range(4)])
        self.assertEqual(result["rows"][5]["variants"][0]["bbox"][:2], [1512, 315])
        self.assertEqual(value, before)
        self.assertEqual(result, extract(value, regions))

    def test_ordinary_full_image_keeps_strict_float_boundaries(self):
        value, regions = calibrated_frame()
        for key in ("ocr_method", "region_layout", "regions"):
            value.pop(key)
        result = extract(value, regions)
        self.assertFalse(result["rows"][5]["queryable"])
        self.assertIn("boundary_crossing", result["rows"][5]["issue_codes"])

    def test_declared_crop_cannot_fall_back_after_losing_provenance(self):
        for removed in ("region_layout", "regions"):
            with self.subTest(removed=removed):
                value, regions = calibrated_frame()
                value.pop(removed)
                # Move all words wholly inside floating rows so a fall-back
                # would mistakenly accept them despite inconsistent indices.
                for item in value["results"][0]["lines"]:
                    item["roi_index"] = 0
                    item["words"][0]["roi_index"] = 0
                    item["words"][0]["x"] += 1
                    item["words"][0]["y"] += 1
                result = extract(value, regions)
                self.assertEqual(result["queryable_count"], 0)
                self.assertIn("crop_metadata_mismatch", result["issue_codes"])

    def test_both_worker_preprocessings_use_their_matching_tiles(self):
        value, regions = calibrated_frame()
        extra = deepcopy(value["region_layout"])
        for tile in extra:
            tile["preprocessing"] = "light_text_140_3x"
        value["region_layout"].extend(extra)
        result = deepcopy(value["results"][0])
        result["preprocessing"] = "light_text_140_3x"
        result["language"] = "ru"
        value["results"].append(result)
        parsed = extract(value, regions)
        self.assertEqual(parsed["queryable_count"], 10)
        self.assertEqual(parsed["rows"][5]["variants"][0]["languages"], ["en-GB", "ru"])

    def test_unmatched_declared_region_or_coordinate_system_is_rejected(self):
        for change in ("region", "coordinate", "dimensions", "duplicate_region", "bad_team"):
            with self.subTest(change=change):
                value, regions = calibrated_frame()
                if change == "region":
                    value["regions"][5]["bbox"][0] += .00001
                elif change == "coordinate":
                    value["coordinate_system"] = "montage_pixels"
                elif change == "dimensions":
                    value["image_width"] = 1917
                elif change == "duplicate_region":
                    value["regions"][5] = deepcopy(value["regions"][6])
                else:
                    value["regions"][5]["team"] = []
                result = extract(value, regions, image_size=(1920, 1080))
                self.assertEqual(result["queryable_count"], 0)
                self.assertIn("crop_metadata_mismatch", result["issue_codes"])

    def test_widened_or_fractional_source_rectangle_is_never_trusted(self):
        for field, delta in ((0, -.5), (1, -.5), (2, .5), (2, 1), (3, 1)):
            with self.subTest(field=field, delta=delta):
                value, regions = calibrated_frame()
                value["region_layout"][5]["source_bbox"][field] += delta
                result = extract(value, regions)
                self.assertEqual(result["queryable_count"], 0)
                self.assertIn("crop_metadata_mismatch", result["issue_codes"])

    def test_wrong_word_or_line_index_cannot_reassign_a_name(self):
        for where, index in (("word", 6), ("line", 6), ("word", True),
                             ("word", None), ("line", None), ("word", 99)):
            with self.subTest(where=where, index=index):
                value, regions = calibrated_frame()
                item = value["results"][0]["lines"][5]
                if where == "word":
                    item = item["words"][0]
                item["roi_index"] = index
                result = extract(value, regions)
                self.assertFalse(result["rows"][5]["queryable"])
                self.assertNotEqual(result["rows"][6]["name"], "Bravo0")

    def test_word_and_line_wrong_index_together_still_fail_source_geometry(self):
        value, regions = calibrated_frame()
        item = value["results"][0]["lines"][5]
        item["roi_index"] = item["words"][0]["roi_index"] = 6
        result = extract(value, regions)
        self.assertFalse(result["rows"][5]["queryable"])
        self.assertFalse(result["rows"][6]["queryable"])
        self.assertIn("boundary_crossing", result["rows"][6]["issue_codes"])

    def test_unknown_duplicate_or_malformed_tile_is_rejected(self):
        for change in ("duplicate", "index", "preprocessing", "preprocessing_list"):
            with self.subTest(change=change):
                value, regions = calibrated_frame()
                if change == "duplicate":
                    value["region_layout"].append(deepcopy(value["region_layout"][5]))
                elif change == "index":
                    value["region_layout"][5]["roi_index"] = 100
                else:
                    value["region_layout"][5]["preprocessing"] = [] if change.endswith("list") else "unknown"
                result = extract(value, regions)
                self.assertEqual(result["queryable_count"], 0)
                self.assertIn("crop_metadata_mismatch", result["issue_codes"])

    def test_missing_or_wrong_result_preprocessing_does_not_adopt_crop(self):
        for preprocessing in (None, [], "unknown", "light_text_140_3x"):
            with self.subTest(preprocessing=preprocessing):
                value, regions = calibrated_frame()
                value["results"][0]["preprocessing"] = preprocessing
                result = extract(value, regions)
                self.assertEqual(result["queryable_count"], 0)
                self.assertIn("crop_metadata_mismatch", result["issue_codes"])

    def test_word_outside_even_the_real_integer_crop_is_rejected(self):
        for axis in ("x", "y", "width", "height"):
            with self.subTest(axis=axis):
                value, regions = calibrated_frame()
                item = value["results"][0]["lines"][5]["words"][0]
                if axis in ("x", "y"):
                    item[axis] -= .1
                else:
                    item[axis] = value["region_layout"][5]["source_bbox"][2 if axis == "width" else 3] + .1
                result = extract(value, regions)
                self.assertFalse(result["rows"][5]["queryable"])
                self.assertIn("boundary_crossing", result["rows"][5]["issue_codes"])
                self.assertEqual(sum(r["queryable"] for r in result["teams"]["Bravo"]), 4)

    def test_actual_cross_row_word_is_rejected_even_inside_rounded_crop(self):
        _, regions = calibrated_frame()
        # Adjacent floating rows have no overlap. Their rounded crops overlap
        # by one pixel, which cannot authorize a word inside the other row.
        regions["Bravo"]["rows"][0] = [1512.4 / 1920, 315.4 / 1080, 177 / 1920, 20 / 1080]
        regions["Bravo"]["rows"][1] = [1512.4 / 1920, 335.4 / 1080, 177 / 1920, 20 / 1080]
        value, regions = calibrated_frame(regions)
        item = value["results"][0]["lines"][5]["words"][0]
        item.update(x=1513, y=335.3, width=100, height=.5)
        result = extract(value, regions)
        self.assertTrue(result["geometry_valid"])
        for row in result["teams"]["Bravo"][:2]:
            self.assertFalse(row["queryable"])
            self.assertIn("boundary_crossing", row["issue_codes"])

    def test_real_overlap_of_original_rows_is_not_erased_by_crop_metadata(self):
        _, regions = calibrated_frame()
        regions["Bravo"]["rows"][1] = deepcopy(regions["Bravo"]["rows"][0])
        value, regions = calibrated_frame(regions)
        result = extract(value, regions)
        self.assertFalse(result["geometry_valid"])
        self.assertIn("overlapping_regions", result["rows"][5]["issue_codes"])
        self.assertFalse(result["rows"][5]["queryable"])
        self.assertFalse(result["rows"][6]["queryable"])

    def test_single_chinese_glyph_is_retained_for_manual_correction(self):
        value = frame()
        value['results'][0]['lines'][0]['text'] = '君'
        value['results'][0]['lines'][0]['words'][0]['text'] = '君'
        item = extract(value)['teams']['Alpha'][0]
        self.assertFalse(item['queryable'])
        self.assertEqual(item['variants'][0]['text'], '君')
        self.assertTrue(item['variants'][0]['manual_only'])
        self.assertIn('single_cjk_glyph', item['issue_codes'])

    def test_ten_distinct_calibrated_rows_are_structurally_queryable(self):
        result = extract()
        self.assertTrue(result["geometry_valid"])
        self.assertTrue(result["complete"])
        self.assertTrue(result["auto_confirmable"])
        self.assertEqual(result["queryable_count"], 10)
        self.assertEqual([r["name"] for r in result["teams"]["Bravo"]], [f"Bravo{i}" for i in range(5)])
        self.assertEqual(result["rows"][5]["bbox"], [600.0, 100.0, 300.0, 100.0])
        self.assertEqual(result["rows"][5]["variants"][0]["bbox"], [620.0, 120.0, 90.0, 20.0])
        self.assertNotIn("self_id", result)
        self.assertNotIn("enemy_ids", result)

    def test_chat_and_names_outside_calibration_are_ignored(self):
        value = frame()
        value["results"][0]["lines"].extend([line("ChatAttacker", 120, 800), line("LobbyFriend", 430, 200)])
        result = extract(value)
        names = [v["text"] for row in result["rows"] for v in row["variants"]]
        self.assertNotIn("ChatAttacker", names)
        self.assertNotIn("LobbyFriend", names)
        self.assertTrue(result["auto_confirmable"])

    def test_absent_rows_do_not_block_other_rows(self):
        value = frame()
        value["results"][0]["lines"] = value["results"][0]["lines"][:8]
        result = extract(value)
        self.assertFalse(result["complete"])
        self.assertFalse(result["auto_confirmable"])
        self.assertEqual(result["queryable_count"], 8)
        self.assertEqual([r["queryable"] for r in result["teams"]["Bravo"]], [True, True, True, False, False])

    def test_different_languages_preserve_all_candidates_for_resolution(self):
        value = frame()
        value["results"].append({"language": "ru", "lines": [line("Браво", 620, 120)]})
        row = extract(value)["teams"]["Bravo"][0]
        self.assertTrue(row["queryable"])
        self.assertIsNone(row["name"])
        self.assertFalse(row["auto_confirmable"])
        self.assertEqual({v["text"] for v in row["variants"]}, {"Bravo0", "Браво"})
        self.assertTrue(all(v["queryable"] for v in row["variants"]))

    def test_same_candidate_across_languages_merges_sources(self):
        value = frame()
        value["results"].append({"language": "ru", "lines": [line("Bravo0", 620, 120)]})
        row = extract(value)["teams"]["Bravo"][0]
        self.assertEqual(len(row["variants"]), 1)
        self.assertEqual(row["variants"][0]["languages"], ["en-GB", "ru"])
        self.assertTrue(row["auto_confirmable"])

    def test_cjk_compact_variant_preserves_original_and_latin_spaces(self):
        value = frame()
        value["results"][0]["lines"][5] = line("张 三 Team One 李 四", 620, 120)
        row = extract(value)["teams"]["Bravo"][0]
        self.assertEqual({v["text"] for v in row["variants"]}, {"张 三 Team One 李 四", "张三 Team One 李四"})
        compact = next(v for v in row["variants"] if v["normalization"] != "none")
        self.assertEqual(compact["original_texts"], ["张 三 Team One 李 四"])
        self.assertTrue(row["queryable"])
        self.assertFalse(row["auto_confirmable"])

    def test_line_text_preserves_actual_spaces_between_words(self):
        value = frame()
        value["results"][0]["lines"][5] = {"text": "Player  One", "words": [word("Player", 620, 120, 50), word("One", 685, 120, 30)]}
        row = extract(value)["teams"]["Bravo"][0]
        self.assertEqual(row["name"], "Player  One")
        self.assertTrue(row["queryable"])

    def test_unrecoverable_word_spacing_is_not_auto_queried(self):
        value = frame()
        value["results"][0]["lines"][5] = {"words": [word("Player", 620, 120, 50), word("One", 685, 120, 30)]}
        row = extract(value)["teams"]["Bravo"][0]
        self.assertEqual(row["variants"][0]["text"], "Player One")
        self.assertFalse(row["queryable"])
        self.assertIn("spacing_uncertain", row["issue_codes"])

    def test_numeric_name_is_retained_for_manual_confirmation(self):
        value = frame()
        value["results"][0]["lines"][5] = line("123456", 620, 120)
        row = extract(value)["teams"]["Bravo"][0]
        self.assertEqual(row["variants"][0]["text"], "123456")
        self.assertTrue(row["variants"][0]["manual_only"])
        self.assertFalse(row["queryable"])
        self.assertIn("numeric_name", row["issue_codes"])

    def test_numeric_tokens_within_a_real_name_are_not_removed(self):
        value = frame()
        value["results"][0]["lines"][5] = {"text": "Player 123", "words": [word("Player", 620, 120, 50), word("123", 680, 120, 30)]}
        self.assertEqual(extract(value)["teams"]["Bravo"][0]["name"], "Player 123")

    def test_numeric_language_alternative_cannot_be_silently_discarded_to_pick_a_name(self):
        value = frame()
        value["results"].append({"language": "ru", "lines": [line("123456", 620, 120)]})
        row = extract(value)["teams"]["Bravo"][0]
        self.assertEqual({v["text"] for v in row["variants"]}, {"Bravo0", "123456"})
        self.assertFalse(row["queryable"])
        self.assertIsNone(row["name"])
        self.assertTrue(all(v["manual_only"] for v in row["variants"]))

    def test_obvious_ui_values_are_auditable_and_do_not_become_names(self):
        for text in ("40%", "3/5", "00:20", "Lv. 22"):
            with self.subTest(text=text):
                value = frame()
                value["results"][0]["lines"].append(line(text, 620, 170))
                row = extract(value)["teams"]["Bravo"][0]
                self.assertEqual(row["name"], "Bravo0")
                self.assertEqual(row["ignored"][0]["text"], text)

    def test_ellipsis_rejects_row_even_with_a_clean_alternative(self):
        for text in ("VeryLong...", "VeryLong…", "VeryLong⋯"):
            value = frame()
            value["results"][0]["lines"][5] = line(text, 620, 120)
            value["results"].append({"language": "ru", "lines": [line("VeryLongName", 620, 120)]})
            row = extract(value)["teams"]["Bravo"][0]
            self.assertFalse(row["queryable"])
            self.assertIn("truncated_name", row["issue_codes"])

    def test_duplicate_candidates_across_teams_block_both_rows(self):
        value = frame()
        value["results"][0]["lines"][5] = line("ALPHA0", 620, 120)
        result = extract(value)
        self.assertEqual(result["queryable_count"], 8)
        self.assertFalse(result["rows"][0]["queryable"])
        self.assertFalse(result["rows"][5]["queryable"])
        self.assertIn("duplicate_name", result["issue_codes"])

    def test_word_crossing_rows_blocks_only_affected_rows(self):
        value = frame()
        value["results"][0]["lines"][5] = line("CrossRow", 620, 190)
        result = extract(value)
        self.assertTrue(result["geometry_valid"])
        self.assertFalse(result["row_geometry_complete"])
        self.assertEqual([r["queryable"] for r in result["teams"]["Bravo"]], [False, False, True, True, True])

    def test_two_name_lines_in_one_calibrated_row_are_not_selected_arbitrarily(self):
        value = frame()
        value["results"][0]["lines"].append(line("SecondName", 620, 165))
        row = extract(value)["teams"]["Bravo"][0]
        self.assertFalse(row["queryable"])
        self.assertEqual(len(row["variants"]), 2)
        self.assertIn("multiple_text_lines", row["issue_codes"])

    def test_overlapping_words_are_rejected(self):
        value = frame()
        value["results"][0]["lines"].append(line("OtherName", 625, 120))
        row = extract(value)["teams"]["Bravo"][0]
        self.assertFalse(row["queryable"])
        self.assertIn("overlapping_words", row["issue_codes"])

    def test_one_ocr_line_spanning_two_team_rows_is_rejected(self):
        value = frame()
        value["results"][0]["lines"][5] = {"text": "Bravo0 Bravo1", "words": [word("Bravo0", 620, 120), word("Bravo1", 620, 220)]}
        del value["results"][0]["lines"][6]
        result = extract(value)
        self.assertFalse(result["rows"][5]["queryable"])
        self.assertFalse(result["rows"][6]["queryable"])
        self.assertIn("ocr_line_crosses_rows", result["issue_codes"])

    def test_ten_explicit_rectangles_and_original_pixel_coordinates(self):
        regions = {team: {"rows": [[x, .11 + i * .1, .3, .05] for i in range(5)]}
                   for team, x in (("Alpha", .1), ("Bravo", .6))}
        result = extract(regions=regions)
        self.assertTrue(result["auto_confirmable"])
        self.assertAlmostEqual(result["rows"][9]["bbox"][1], 510)

    def test_overlapping_team_calibration_is_globally_invalid(self):
        regions = deepcopy(REGIONS)
        regions["Bravo"] = regions["Alpha"]
        result = extract(regions=regions)
        self.assertFalse(result["geometry_valid"])
        self.assertEqual(result["queryable_count"], 0)

    def test_explicit_rows_can_be_selected_in_any_order_and_keep_indices(self):
        regions = {team: {"rows": [[x, .11 + i * .1, .3, .05] for i in range(5)]}
                   for team, x in (("Alpha", .1), ("Bravo", .6))}
        regions["Bravo"]["rows"][0], regions["Bravo"]["rows"][1] = regions["Bravo"]["rows"][1], regions["Bravo"]["rows"][0]
        result = extract(regions=regions)
        self.assertTrue(result["geometry_valid"])
        self.assertTrue(result["auto_confirmable"])
        self.assertEqual(result["teams"]["Bravo"][0]["name"], "Bravo1")
        self.assertEqual(result["teams"]["Bravo"][1]["name"], "Bravo0")

    def test_horizontal_cards_may_share_one_ocr_line(self):
        regions = {team: {"rows": [[.1 + i * .16, y, .1, .05] for i in range(5)]}
                   for team, y in (("Alpha", .1), ("Bravo", .3))}
        lines = []
        for team, y in (("Alpha", 110), ("Bravo", 310)):
            words = [word(f"{team}{i}", 110 + i * 160, y, 80) for i in range(5)]
            lines.append({"text": " ".join(w["text"] for w in words), "words": words})
        value = {"image_width": 1000, "image_height": 1000, "results": [{"language": "en-GB", "lines": lines}]}
        result = extract(value, regions)
        self.assertTrue(result["auto_confirmable"])
        self.assertEqual(result["queryable_count"], 10)

    def test_invalid_or_missing_calibration_never_uses_default_screen_positions(self):
        bad = [{}, {"Alpha": REGIONS["Alpha"]}, {**REGIONS, "Bravo": [0, 0, 0, .5]},
               {**REGIONS, "Bravo": [0, .9, .3, .5]}, {**REGIONS, "Bravo": [0, 0, float("nan"), .5]},
               {**REGIONS, "Bravo": {"rows": [[0, 0, .1, .1]]}}]
        for regions in bad:
            with self.subTest(regions=regions), self.assertRaises(ValueError):
                extract(regions=regions)
        with self.assertRaises(ValueError):
            extract_prep_roster({"results": []}, REGIONS)

    def test_input_is_unchanged_and_output_is_repeatable_json(self):
        value = frame()
        before = deepcopy(value)
        one, two = extract(value), extract(value)
        self.assertEqual(one, two)
        self.assertEqual(value, before)
        self.assertEqual(json.loads(json.dumps(one, ensure_ascii=False))["queryable_count"], 10)
        self.assertEqual(extract_prep_roster(value["results"], REGIONS, image_size=(1000, 1000)), one)

    def test_bad_word_geometry_is_not_used_or_claimed_fully_confirmed(self):
        value = frame()
        value["results"][0]["lines"].append({"words": [{"text": "BadBounds", "x": 700, "y": 125, "width": -1, "height": 20}]})
        result = extract(value)
        self.assertFalse(result["auto_confirmable"])
        self.assertIn("malformed_ocr", result["issue_codes"])
        self.assertNotIn("BadBounds", {v["text"] for r in result["rows"] for v in r["variants"]})

    def test_invisible_control_characters_require_manual_confirmation(self):
        value = frame()
        value["results"][0]["lines"][5] = line("Name\u200bHidden", 620, 120)
        row = extract(value)["rows"][5]
        self.assertFalse(row["queryable"])
        self.assertIn("control_characters", row["issue_codes"])

    def test_player_name_placeholder_variations_never_become_queryable_names(self):
        for name in ("Player name", "PLAYER NAME", "playername", "Player   name", "Ｐｌａｙｅｒ　ｎａｍｅ"):
            with self.subTest(name=name):
                value = frame()
                value["results"][0]["lines"][5] = line(name, 620, 120)
                result = extract(value)
                row = result["teams"]["Bravo"][0]
                self.assertTrue(row["is_placeholder"])
                self.assertFalse(row["queryable"])
                self.assertIsNone(row["name"])
                self.assertIn("placeholder_name", row["issue_codes"])
                self.assertEqual(result["placeholder_counts"], {"Alpha": 0, "Bravo": 1})
                self.assertTrue(all(v["manual_only"] and not v["queryable"] for v in row["variants"]))

    def test_placeholder_blocks_different_language_misreading_in_same_row(self):
        value = frame()
        value["results"][0]["lines"][5] = line("Player name", 620, 120)
        value["results"].append({"language": "ru", "lines": [line("Рlауег nаmе", 620, 120)]})
        result = extract(value)
        row = result["teams"]["Bravo"][0]
        self.assertEqual(len(row["variants"]), 2)
        self.assertTrue(row["is_placeholder"])
        self.assertFalse(row["queryable"])
        self.assertTrue(all(not variant["queryable"] for variant in row["variants"]))
        self.assertEqual(result["queryable_count"], 9)

    def test_five_placeholders_count_by_row_not_by_language_or_variant(self):
        value = frame()
        for index in range(5):
            value["results"][0]["lines"][5 + index] = line("Player name", 620, 120 + index * 100)
        value["results"].append({"language": "zh-Hans-CN", "lines": [line("PLAYER NAME", 620, 120 + i * 100) for i in range(5)]})
        result = extract(value)
        self.assertEqual(result["placeholder_counts"], {"Alpha": 0, "Bravo": 5})
        self.assertTrue(result["geometry_valid"])
        self.assertFalse(any(row["queryable"] for row in result["teams"]["Bravo"]))
        self.assertFalse(result["auto_confirmable"])

    def test_similar_actual_names_are_not_fuzzy_blocked_as_placeholders(self):
        for name in ("PlayerName123", "MyPlayerName", "Player Named", "Player-name", "Player name 2"):
            with self.subTest(name=name):
                value = frame()
                value["results"][0]["lines"][5] = line(name, 620, 120)
                row = extract(value)["teams"]["Bravo"][0]
                self.assertFalse(row["is_placeholder"])
                self.assertTrue(row["queryable"])
                self.assertEqual(row["name"], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
