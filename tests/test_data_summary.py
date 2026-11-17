"""Pure data integrity checks for descriptive opening/whole-match summaries."""

from copy import deepcopy
import json
import unittest

from ba_scout.data_summary import build_data_summary, summary_sections, summary_text


def roster():
    return {"fight_id": "900", "players": [
        {"id": 1, "name": "第一位对手的完整长名称", "team": "Bravo"},
        {"id": 2, "name": "第二位对手的完整长名称", "team": "Bravo"},
        {"id": 3, "name": "我方玩家", "team": "Alpha"},
    ]}


def catalog():
    roles = {10: ["tank"], 11: ["tank"], 12: ["transport_rotor"], 13: ["multirole_rotor"],
             14: ["air_defense"], 15: ["multirole_plane"], 16: ["infantry"], 17: ["recon", "infantry"]}
    return {"schema": 1, "source": {"build_id": "offline-test", "db_path": "F:/offline/catalog.json"},
            "units": {str(uid): {"id": uid, "name": f"完整型号 {uid} · 原名保留", "roles": role} for uid, role in roles.items()}}


def event(pid, records, mids=(100, 101, 102, 103, 104)):
    return {"type": "opening", "player_id": pid, "status": "ok", "finished": True, "data": {
        "sample_count": len(mids), "requested_count": 5, "window_seconds": 120,
        "matches": [{"match_id": str(mid), "source_url": f"https://example.test/match/{mid}", "match_end_time": 1790000000 + mid} for mid in mids],
        "units": [{"id": uid, "name": f"完整型号 {uid} · 原名保留", "matches_with_unit": len(set(unit_mids)),
                   "match_ids": [str(mid) for mid in unit_mids], "total_spawns": len(unit_mids) * 3, "first_offset_median": 7.5} for uid, unit_mids in records],
        "unusable_matches": [],
    }}


def build(openings=None, recon=None, **kwargs):
    return build_data_summary(roster(), {}, openings or {}, recon or {}, kwargs.pop("enemy_ids", [1, 2]), catalog=kwargs.pop("catalog", catalog()), **kwargs)


def category(summary, key):
    return next(item for item in summary["categories"] if item["key"] == key)


class DataSummaryTests(unittest.TestCase):
    def test_category_union_does_not_add_two_model_frequencies(self):
        result = build({1: event(1, [(10, [100, 101, 102]), (11, [102, 103, 104])])})
        tank = category(result, "tank")["players"][0]
        self.assertEqual((tank["matches_with_category"], tank["sample_count"]), (5, 5))
        self.assertEqual(tank["match_ids"], ["100", "101", "102", "103", "104"])
        self.assertNotIn("6/5", summary_text(result))

    def test_each_player_retains_own_denominator_including_four(self):
        result = build({1: event(1, [(12, [100, 101, 102])]), 2: event(2, [(12, [100, 101])], [100, 101, 102, 103])})
        rows = category(result, "transport_rotor")["players"]
        self.assertEqual([(r["matches_with_category"], r["sample_count"]) for r in rows], [(3, 5), (2, 4)])
        text = summary_text(result)
        self.assertIn("3/5局", text)
        self.assertIn("2/4局", text)
        self.assertNotIn("5/9", text)

    def test_fixed_category_order_unchanged_by_higher_frequencies(self):
        result = build({1: event(1, [(15, [100, 101, 102, 103, 104]), (10, [100]), (14, [100, 101]), (16, [100])])})
        self.assertEqual([r["key"] for r in result["categories"]], ["infantry", "tank", "air_defense", "multirole_plane"])

    def test_multirole_rotor_remains_separate_from_transport_and_loadouts_unknown(self):
        result = build({1: event(1, [(12, [100]), (13, [101, 102]), (15, [100])])})
        self.assertEqual(category(result, "transport_rotor")["players"][0]["matches_with_category"], 1)
        self.assertEqual(category(result, "multirole_rotor")["players"][0]["matches_with_category"], 2)
        self.assertIn("挂载未知", category(result, "multirole_plane")["label"])

    def test_detailed_text_keeps_all_names_counts_and_times_and_main_discloses_top_three(self):
        result = build({1: event(1, [(uid, [100, 101]) for uid in [10, 11, 12, 13, 14]])})
        text = summary_text(result)
        for uid in [10, 11, 12, 13, 14]:
            self.assertIn(f"完整型号 {uid} · 原名保留 [单位ID {uid}]：2/5局；总出场 6 次；各出现局首次出场中位 7.5 秒", text)
        sections = summary_sections(result)
        first = next((title, body) for title, body in sections if "第一位对手" in title)
        self.assertIn("前3/共5种", first[0])
        self.assertEqual(first[1].count("各出现局首次出场中位"), 3)
        self.assertIn("数值排序", first[1])
        self.assertIn("https://example.test/match/100", text)

    def test_missing_units_is_unknown_but_explicit_empty_list_is_observed_empty(self):
        missing = event(1, [])
        missing["data"].pop("units")
        result = build({1: missing})
        self.assertFalse(result["players"][0]["opening_units_available"])
        for text in [summary_text(result), summary_text(result, detailed=False)]:
            self.assertIn("开局单位明细未取得", text)
            self.assertNotIn("窗口内没有单位出场", text)
            self.assertNotIn("窗口内无单位出场", text)
            self.assertNotIn("共0种", text)
            self.assertNotIn("共 0 种", text)
        empty = build({1: event(1, [])})
        self.assertTrue(empty["players"][0]["opening_units_available"])
        self.assertIn("窗口内没有单位出场", summary_text(empty))

    def test_no_catalog_keeps_full_units_and_marks_classification_unknown(self):
        result = build({1: event(1, [(10, [100, 101])])}, catalog=None)
        self.assertEqual([c["key"] for c in result["categories"]], ["unclassified"])
        self.assertEqual(result["players"][0]["units"][0]["matches_with_unit"], 2)
        text = summary_text(result)
        self.assertIn("完整型号 10 · 原名保留", text)
        self.assertIn("角色目录未取得", text)

    def test_unknown_model_prevents_false_exact_zero_for_known_category(self):
        result = build({1: event(1, [(10, [100])]), 2: event(2, [(999, [100, 101])])})
        row = category(result, "tank")["players"][1]
        self.assertTrue(row["known_only"])
        self.assertEqual(row["matches_with_category"], 0)
        self.assertIn("至少已知 0/5局", summary_text(result))

    def test_zero_samples_never_claim_zero_category_or_zero_deployments(self):
        result = build({1: event(1, [(10, [100])]), 2: event(2, [], [])})
        row = category(result, "tank")["players"][1]
        self.assertIsNone(row["matches_with_category"])
        self.assertEqual(row["sample_count"], 0)
        self.assertIn("有效样本未取得", summary_text(result))
        self.assertNotIn("0/0局", summary_text(result))

    def test_recent_whole_match_is_separate_and_does_not_change_opening_categories(self):
        opening = {1: event(1, [(12, [100])])}
        recent = {1: {"type": "recon", "player_id": 1, "status": "ok", "match_id": "200", "from_cache": True,
                      "source_url": "https://example.test/match/200", "fetched_at": "2026-10-25T00:00:00Z", "match_end_time": 1790000200,
                      "data": {"available": True, "player_id": 1, "total_spawns": 99, "fixed_wing": 99,
                               "all_units": [{"id": 15, "name": "整场独有完整飞机名称", "count": 99}]}}}
        result = build(opening, recent)
        self.assertEqual(result["categories"], build(opening)["categories"])
        self.assertEqual(result["players"][0]["recent"]["all_units"][0]["count"], 99)
        text = summary_text(result)
        self.assertIn("最近1场整场记录（与开局分开", text)
        self.assertIn("整场独有完整飞机名称 [单位ID 15] × 99", text)
        self.assertIn("来自缓存", text)

    def test_stale_history_and_replay_are_disclosed_without_changing_counts(self):
        event_map = {1: event(1, [(12, [100, 101])])}
        normal = build(event_map)
        old = build(event_map, stale_history_ids=["1"], is_replay=True)
        self.assertEqual(old["players"][0]["units"], normal["players"][0]["units"])
        text = summary_text(old)
        self.assertIn("旧比赛清单", text)
        self.assertIn("历史名单回放", text)
        self.assertIn("实际最新比赛可能已经变化", text)

    def test_explicit_enemy_scope_and_identity_mismatches(self):
        result = build({1: event(1, [(10, [100])]), 3: event(3, [(12, [100])])})
        self.assertEqual([p["player_id"] for p in result["players"]], [1, 2])
        self.assertNotIn("transport_rotor", [c["key"] for c in result["categories"]])
        self.assertEqual(build(enemy_ids=None)["players"], [])
        self.assertEqual(build(enemy_ids=[1, 3])["players"], [])
        wrong = build({1: event(999, [(10, [100])])})
        self.assertEqual(wrong["categories"], [])
        self.assertEqual(wrong["players"][0]["status"], "error")

    def test_invalid_frequency_retains_name_and_count_but_does_not_classify(self):
        invalid = event(1, [(10, [100])])
        invalid["data"]["units"][0]["match_ids"] = ["999"]
        result = build({1: invalid})
        self.assertEqual(result["categories"], [])
        unit = result["players"][0]["units"][0]
        self.assertIsNone(unit["matches_with_unit"])
        self.assertEqual(unit["total_spawns"], 3)
        self.assertIn("逐局频率未取得", summary_text(result))

    def test_player_index_avoids_long_names_in_every_overview_row(self):
        result = build({1: event(1, [(10, [100]), (12, [100])]), 2: event(2, [(10, [101])])})
        sections = dict(summary_sections(result))
        self.assertIn("①第一位对手的完整长名称", sections["玩家索引（完整名称）"])
        matrix = sections["固定类别概览（部分分类，完整见详情）"]
        self.assertIn("① 1/5局", matrix)
        self.assertIn("② 1/5局", matrix)
        self.assertNotIn("完整长名称", matrix)

    def test_descriptive_result_has_no_advice_ratings_or_tactical_fields(self):
        result = build({1: event(1, [(12, [100, 101])])})
        for forbidden_key in ["threats", "deck", "confidence", "phases", "kit", "response"]:
            self.assertNotIn(forbidden_key, result)
        text = summary_text(result)
        for phrase in ["推荐", "优先", "风险", "应对", "置信", "选卡组", "建议", "强弱"]:
            self.assertNotIn(phrase, text)

    def test_no_mutation_serializable_and_empty_startup_supported(self):
        source = {1: event(1, [(10, [100, 101])])}
        before = deepcopy(source)
        first = build(source)
        self.assertEqual(source, before)
        self.assertEqual(first, build(source))
        self.assertEqual(json.loads(json.dumps(first, ensure_ascii=False, allow_nan=False)), first)
        empty = build_data_summary(None, {}, {}, {}, None)
        self.assertEqual(empty["players"], [])
        self.assertTrue(summary_sections(empty))
        self.assertIn("未确认", summary_text(empty))


if __name__ == "__main__":
    unittest.main()
