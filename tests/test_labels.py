"""Exact display wording, conservative fallbacks, and source preservation."""

import copy
import unittest

from ba_scout.labels import (
    SPEC_ALIASES,
    SPEC_COUNTRIES,
    SPEC_COUNTRY,
    SPEC_FULL_NAMES,
    SPEC_NAMES,
    combo_country,
    short_combo,
)


class LabelTests(unittest.TestCase):
    def test_exact_user_names_and_complete_mapping(self):
        self.assertEqual(
            SPEC_NAMES,
            {3: "马润", 4: "装甲", 5: "空降", 6: "骑兵", 13: "特种", 16: "波波",
             7: "vdv", 8: "近坦", 9: "摩托化", 10: "海步", 12: "国", 15: "机械化"},
        )
        self.assertEqual(set(SPEC_NAMES), set(SPEC_FULL_NAMES))
        self.assertEqual(set(SPEC_NAMES), set(SPEC_COUNTRIES))
        self.assertIs(SPEC_COUNTRY, SPEC_COUNTRIES)
        self.assertEqual(SPEC_ALIASES["波罗的海营"], "波波")
        self.assertEqual(SPEC_ALIASES["近卫空中突击旅"], "vdv")

    def test_known_combo_uses_ids_not_unreliable_label_and_keeps_order(self):
        self.assertEqual(short_combo({"key": "4_5", "label": "任意旧名称"}), "装甲+空降")
        self.assertEqual(short_combo({"key": "5_4"}), "空降+装甲")
        self.assertEqual(short_combo({"key": "7_12"}), "vdv+国")
        self.assertEqual(short_combo({"key": "13_16"}), "特种+波波")
        self.assertEqual(short_combo({"key": "3_6"}), "马润+骑兵")
        self.assertEqual(short_combo({"key": "8_10"}), "近坦+海步")
        self.assertEqual(short_combo({"key": "9_15"}), "摩托化+机械化")

    def test_unknown_id_preserves_source_label_without_partial_guess(self):
        source_label = "  未来专精 + 来源名称  "
        self.assertEqual(short_combo({"key": "4_999", "label": source_label}), source_label)
        self.assertEqual(short_combo({"key": "999_1000", "label": source_label}), source_label)
        self.assertEqual(short_combo({"key": "4_999"}), "组合 4_999")

    def test_missing_and_malformed_input_has_explicit_fallback(self):
        for spec in [None, {}, [], "4_5", {"key": None}, {"label": "  "}]:
            with self.subTest(spec=spec):
                self.assertEqual(short_combo(spec), "未知专精")
        for key in ["4", "4_5_6", "4+5", "4_4", "-4_5", "04_5", 4, [4, 5]]:
            with self.subTest(key=key):
                self.assertEqual(short_combo({"key": key, "label": "原始标签"}), "原始标签")

    def test_country_requires_distinct_known_same_country_ids(self):
        for key in ["3_4", "4_5", "13_16", " 6_16 "]:
            self.assertEqual(combo_country(key), "美国")
        for key in ["7_8", "9_10", "12_15"]:
            self.assertEqual(combo_country(key), "俄罗斯")
        for key in [None, "", "3_7", "4_12", "4_999", "999_1000", "4_4", "4", "4_5_6", 4, [4, 5]]:
            with self.subTest(key=key):
                self.assertIsNone(combo_country(key))

    def test_display_helpers_do_not_mutate_source_record(self):
        original = {"key": "4_5", "label": "装甲旅级战斗队 + 空降师", "experience": 90317}
        record = copy.deepcopy(original)
        self.assertEqual(short_combo(record), "装甲+空降")
        self.assertEqual(combo_country(record["key"]), "美国")
        self.assertEqual(record, original)


if __name__ == "__main__":
    unittest.main()
