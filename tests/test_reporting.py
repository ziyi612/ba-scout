"""Pure offline text-export tests; no file, network, or desktop operations."""

from copy import deepcopy
import unittest

from ba_scout.reporting import build_scout_report
from ba_scout.roster import Roster
from ba_scout.data_summary import build_data_summary


LONG_NAME = "M1A2 SEP v3 装甲主战坦克（完整型号，不应缩写或截断）"
STAMP = "2026-10-25T09:00:00Z"


def snapshot():
    roster = Roster("123456", [{"id": 7, "name": "对手完整玩家名称", "team": "Bravo"},
                              {"id": 8, "name": "我方玩家", "team": "Alpha"}], STAMP, "unused", STAMP)
    profile = {
        "player_id": 7, "rating": 1800, "rank": 20, "ranked_games": 10,
        "wins": 6, "losses": 3, "leaves": 1,
        "ranked_killed_value": 11178, "ranked_lost_value": 10000,
        "ranked_kills": 9939, "ranked_losses_units": 10000,
        "ranked_duration_seconds": 12000, "nation_experience": {"1": 137739, "2": 49414},
        "specs": [{"key": "5_6", "label": "空降师+斯特瑞克旅", "experience": 1000},
                  {"key": "3_4", "label": "海军陆战队远征队+装甲旅级战斗队", "experience": 1000}],
        "source_url": "https://dash.batrace.top/player/7", "fetched_at": STAMP,
    }
    units = [{"id": index, "name": f"{LONG_NAME} {index}", "type": 4,
              "matches_with_unit": 2, "total_spawns": index, "first_offset_median": 3.5}
             for index in range(1, 8)]
    opening = {"status": "partial", "message": "已取得2/3局", "data": {
        "sample_count": 2, "requested_count": 3, "window_seconds": 120, "units": units,
        "matches": [{"match_id": "42", "source_url": "https://dash.batrace.top/match/42", "match_end_time": 1792846260}],
        "unusable_matches": [{"match_id": "43", "reason": "对局提前结束，未覆盖完整开局观察窗口"}],
    }}
    recon = {"status": "ok", "match_id": "42", "source_url": "https://dash.batrace.top/match/42",
             "match_end_time": 1792846260, "fetched_at": STAMP, "from_cache": True,
             "data": {"player_id": 7, "available": True, "total_spawns": 28,
                      "fixed_wing": 0, "rotorcraft": 0, "infantry": 0, "vehicles": 28,
                      "unknown_types": 0, "classification_complete": True,
                      "all_units": [{"id": u["id"], "name": u["name"], "count": u["total_spawns"]} for u in units]}}
    return roster, {7: {"status": "ok", "data": profile}}, {7: opening}, {7: recon}, [7]


def render(args, **kwargs):
    return build_scout_report(*args, generated_at=STAMP, **kwargs)


def dual_snapshot():
    args = list(snapshot())
    opening = args[2][7]["data"]
    opening["matches"].append({"match_id": "44", "source_url": "https://dash.batrace.top/match/44", "match_end_time": 1792846300})
    for unit in opening["units"]:
        unit["match_ids"] = ["42", "44"]
    first = deepcopy(args[2])
    first[7]["data"]["window_seconds"] = 20
    first[7]["data"]["units"] = first[7]["data"]["units"][:2]
    first[7]["data"]["unusable_matches"] = [{"match_id": "45", "reason": "未取得前20秒样本"}]
    first[7]["message"] = "前20秒独立样本状态"
    full_summary = build_data_summary(*args, window_seconds=120)
    first_summary = build_data_summary(args[0], args[1], first, args[3], args[4], window_seconds=20)
    return args, {"opening20_states": first, "data_summary20": first_summary, "data_summary": full_summary}


class ScoutReportTests(unittest.TestCase):
    def test_full_unit_names_and_all_rows_in_both_analyses(self):
        text = render(snapshot(), is_replay=True)
        for index in range(1, 8):
            self.assertEqual(text.count(f"{LONG_NAME} {index}"), 2)
        self.assertNotIn("…", text)
        self.assertIn("有效样本 2/3 局", text)
        self.assertIn("出现 2/2 局；总部署 7 次", text)
        self.assertIn("各出现局首次出场时间中位数 3.5 秒", text)
        self.assertIn("对局提前结束，未覆盖完整开局观察窗口", text)
        self.assertIn("https://dash.batrace.top/player/7", text)
        self.assertIn("https://dash.batrace.top/match/42", text)
        self.assertIn("阵容对局ID：123456", text)
        self.assertIn("历史阵容回放（不是当前正在进行的对局）", text)

    def test_verified_formulas_and_experience_labels(self):
        text = render(snapshot())
        self.assertIn("胜率 60.0%；退局率 10.0%", text)
        self.assertIn("价值交换比 1.1178", text)
        self.assertIn("单位数量 K/D 0.9939", text)
        self.assertIn("空降+骑兵：经验 1,000；完整名称 空降师+斯特瑞克旅", text)
        self.assertIn("最高经验存在并列", text)
        self.assertIn("经验不是近期使用率或下局出场概率", text)
        self.assertIn("国家经验：俄 137,739 · 美 49,414", text)

    def test_unknown_values_do_not_become_zero_or_invalid_percent(self):
        args = list(snapshot())
        args[1] = {7: {"status": "ok", "data": {"player_id": 7, "wins": 0, "ranked_games": 0,
                    "ranked_killed_value": 10, "ranked_lost_value": 0, "rating": float("nan")}}}
        text = render(args)
        self.assertIn("胜率 未取得；退局率 未取得", text)
        self.assertIn("价值交换比 未取得", text)
        self.assertIn("评分 未取得；排名 未取得", text)
        self.assertNotIn("未取得%", text)
        self.assertNotIn("nan", text)

    def test_zero_is_preserved_when_observed(self):
        args = list(snapshot())
        data = args[1][7]["data"]
        data.update(wins=0, leaves=0, ranked_killed_value=0, ranked_kills=0)
        text = render(args)
        self.assertIn("胜率 0.0%；退局率 0.0%", text)
        self.assertIn("价值交换比 0.0000", text)
        self.assertIn("固定翼 0 / 旋翼 0", text)

    def test_single_match_is_not_labeled_as_a_habit(self):
        args = list(snapshot())
        args[2][7]["data"].update(sample_count=1, requested_count=5)
        text = render(args)
        self.assertIn("有效样本 1/5 局", text)
        self.assertIn("当前取得1场记录", text)

    def test_legacy_advice_is_ignored_in_data_only_reports(self):
        args = snapshot()
        clean = render(args)
        legacy = render(args, advice={"deck":{"label":"推荐卡组测试"},"threats":[{"response":"应对测试"}]})
        self.assertEqual(clean, legacy)
        self.assertNotIn("推荐卡组测试", legacy)
        self.assertNotIn("应对测试", legacy)

    def test_empty_valid_window_distinguished_from_missing_samples(self):
        args = list(snapshot())
        args[2][7]["data"]["units"] = []
        self.assertIn("完整观察窗口内没有部署记录", render(args))
        args[2][7]["data"]["sample_count"] = 0
        text = render(args)
        self.assertIn("未取得有效开局样本", text)
        self.assertNotIn("完整观察窗口内没有部署记录", text)

    def test_team_groups_and_unknown_team_detection(self):
        args = list(snapshot())
        text = render(args, is_replay=False)
        self.assertIn("对手 · 1 人", text)
        self.assertIn("我方 · 1 人", text)
        self.assertIn("模式：当前阵容查询", text)
        args[4] = []
        text = render(args)
        self.assertIn("Alpha（敌我未判定）", text)
        self.assertIn("Bravo（敌我未判定）", text)
        self.assertNotIn("我方 ·", text)

    def test_stale_failure_and_partial_legacy_units_are_explicit(self):
        args = list(snapshot())
        args[1][7]["status"] = "timeout"
        args[1][7]["data"]["from_cache"] = True
        sample = args[3][7]["data"]
        sample["top_units"] = sample.pop("all_units")[:2]
        sample.update(classification_complete=False, vehicles=None)
        text = render(args)
        self.assertIn("截止未完成；以下沿用旧缓存", text)
        self.assertIn("完整单位列表未取得", text)
        self.assertIn("车辆 未取得", text)
        self.assertIn("分类不完整", text)

    def test_empty_roster_and_all_unavailable_do_not_crash(self):
        text = build_scout_report(None, {}, {}, {}, [], generated_at=STAMP)
        self.assertIn("未取得阵容名单", text)
        args = list(snapshot())
        args[1:4] = [{}, {}, {}]
        text = render(args)
        self.assertIn("战绩、评分与专精数据未取得", text)
        self.assertIn("有效样本 未取得/未取得 局", text)

    def test_dict_roster_string_state_keys_and_no_input_mutation(self):
        args = list(snapshot())
        args[0] = vars(args[0]).copy()
        for index in (1, 2, 3):
            args[index] = {str(key): value for key, value in args[index].items()}
        before = deepcopy(args)
        first = render(args)
        self.assertEqual(first, render(args))
        self.assertEqual(args, before)
        self.assertEqual(first.encode("utf-8").decode("utf-8"), first)

    def test_mismatched_player_data_is_not_attributed(self):
        args = list(snapshot())
        args[1][7]["data"]["player_id"] = 999
        args[3][7]["data"]["player_id"] = 999
        text = render(args)
        self.assertEqual(text.count("数据玩家ID不匹配，本段未采用"), 2)
        self.assertNotIn("价值交换比 1.1178", text)

    def test_identifiers_are_not_number_formatted(self):
        args = list(snapshot())
        args[0].players[0]["id"] = 810001
        args[4] = [810001]
        text = render(args)
        self.assertIn("ID 810001", text)
        self.assertNotIn("ID 373,557", text)

    def test_dual_window_overviews_then_full_lists_without_repeated_core_or_recent(self):
        args, extra = dual_snapshot()
        text = render(args, **extra)
        self.assertLess(text.index("前20秒概览"), text.index("前120秒概览"))
        self.assertLess(text.index("前120秒概览"), text.index("完整资料与原始依据"))
        self.assertLess(text.index("前20秒全部单位"), text.index("前120秒全部单位"))
        self.assertLess(text.index("前120秒全部单位"), text.index("最近1场整场出场记录"))
        self.assertEqual(text.count("核心数据："), 2)  # One per roster member.
        self.assertEqual(text.count("最近1场整场出场记录："), 2)
        for index in range(1, 8):
            self.assertEqual(text.count(f"{LONG_NAME} {index}"), 3 if index <= 2 else 2)
        self.assertIn("0 <= t < 20 秒", text)
        self.assertIn("0 <= t < 120 秒", text)
        self.assertIn("前120秒包含前20秒，不是20–120秒区间", text)

    def test_dual_window_sample_counts_and_failures_are_independent(self):
        args, extra = dual_snapshot()
        args[2][7]["data"].update(sample_count=1, requested_count=3)
        extra["data_summary"] = None
        text = render(args, **extra)
        details = text.split("【对手完整玩家名称】", 1)[1]
        first, second = details.split("前20秒全部单位", 1)[1].split("前120秒全部单位", 1)
        self.assertIn("有效样本 2/3 局", first)
        self.assertIn("未取得前20秒样本", first)
        self.assertNotIn("对局提前结束", first)
        self.assertIn("有效样本 1/3 局", second)
        self.assertIn("对局提前结束", second)
        self.assertNotIn("未取得前20秒样本", second)

    def test_missing_20_second_results_remain_unknown_not_copied_from_120(self):
        args, extra = dual_snapshot()
        extra["opening20_states"] = {}
        extra["data_summary20"] = None
        text = render(args, **extra)
        first = text.split("【对手完整玩家名称】", 1)[1].split("前20秒全部单位", 1)[1].split("前120秒全部单位", 1)[0]
        self.assertIn("有效样本 未取得/未取得 局", first)
        self.assertIn("未取得有效开局样本", first)
        self.assertNotIn(LONG_NAME, first)
        self.assertNotIn("有效样本 0/", first)

    def test_wrong_window_is_not_relabelled_as_twenty_seconds(self):
        args, extra = dual_snapshot()
        extra["opening20_states"][7]["data"]["window_seconds"] = 120
        extra["data_summary20"]["context"]["window_seconds"] = 120
        text = render(args, **extra)
        self.assertIn("类别概览时间窗 120 秒与本栏不一致，未采用", text)
        first = text.split("【对手完整玩家名称】", 1)[1].split("前20秒全部单位", 1)[1].split("前120秒全部单位", 1)[0]
        self.assertIn("与本栏 20 秒不一致；该记录未采用", first)
        self.assertNotIn(LONG_NAME, first)

    def test_configured_second_window_uses_actual_duration(self):
        args, extra = dual_snapshot()
        args[2][7]["data"]["window_seconds"] = 90
        extra["data_summary"] = None
        text = render(args, opening_window_seconds=90, **extra)
        self.assertIn("前90秒概览（设置窗口）", text)
        self.assertIn("前90秒全部单位（设置窗口）", text)
        self.assertIn("前90秒包含前20秒，不是20–90秒区间", text)
        self.assertNotIn("前120秒", text)
        args[2][7]["data"]["window_seconds"] = 20
        text = render(args, opening_window_seconds=20, **extra)
        self.assertIn("设置窗口与固定前20秒窗口范围相同", text)

    def test_none_twenty_arguments_preserve_legacy_single_window_output(self):
        args = snapshot()
        self.assertEqual(render(args), render(args, opening20_states=None, data_summary20=None))
        self.assertNotIn("前20秒概览", render(args))

    def test_dual_export_is_pure_and_still_ignores_legacy_advice(self):
        args, extra = dual_snapshot()
        before = deepcopy((args, extra))
        clean = render(args, **extra)
        legacy = render(args, **extra, advice={"summary": "不得出现的主观推荐"})
        self.assertEqual(clean, legacy)
        self.assertEqual((args, extra), before)
        self.assertNotIn("不得出现的主观推荐", legacy)


if __name__ == "__main__":
    unittest.main()
