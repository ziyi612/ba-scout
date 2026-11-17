"""Offline deployment-time parsing and opening-denominator tests."""

from copy import deepcopy
import json
from pathlib import Path
from tests.optional_fixtures import load_optional_fixture, assert_expected_fields
import unittest

from ba_scout.opening import aggregate_openings
from ba_scout.recon_provider import parse_match_html


START = 1792845492


def match_fixture(spawns=None, *, match_id="42", start=START, end=START + 600):
    if spawns is None:
        spawns = [(1, 0), (2, 119), (1, 120)]
    instances = {}
    for index, (unit_id, offset) in enumerate(spawns):
        # Keys run backwards on purpose: NetID order must not imply time.
        item = {"Id": unit_id}
        if offset is not None:
            item["SpawnTime"] = START + offset
        instances[str(10000 - index)] = item
    return {
        "matchId": match_id,
        "initialMatchData": {"matchInfo": {
            "StartTime": start, "EndTime": end,
            "Data": {"7": {"Id": 7, "UnitData": instances}},
        }},
        "initialUnitRegistry": {
            "1": {"id": 1, "hudName": "飞机甲", "type": 16},
            "2": {"id": 2, "hudName": "运输乙", "type": 8},
            "3": {"id": 3, "hudName": "步兵丙", "type": 2},
        },
    }


def parse_fixture(props):
    stream = "1:" + json.dumps(props, ensure_ascii=False) + "\n"
    html = "<script>self.__next_f.push(" + json.dumps([1, stream], ensure_ascii=False) + ")</script>"
    return parse_match_html(html, props["matchId"])


def envelope(spawns=None, **kwargs):
    return parse_fixture(match_fixture(spawns, **kwargs))


class DeploymentTimingTests(unittest.TestCase):
    def test_real_time_offsets_preserve_all_instances(self):
        result = envelope([(2, 119), (1, 0), (1, 120)])
        self.assertEqual(result["recon_version"], 2)
        self.assertEqual(result["match_start_time"], START)
        timing = result["players"]["7"]["deployment_timing"]
        self.assertTrue(timing["available"])
        self.assertTrue(timing["complete"])
        self.assertEqual((timing["start_time"], timing["end_time"]), (START, START + 600))
        self.assertEqual([s["offset_s"] for s in timing["spawns"]], [0, 119, 120])
        self.assertEqual(timing["spawns"][0], {"id": 1, "name": "飞机甲", "type": 16, "offset_s": 0})
        self.assertEqual(sum(timing[k] for k in ("missing_spawn_times", "before_start", "after_end", "missing_unit_ids")), 0)

    def test_same_second_uses_stable_id_order_without_netid_order(self):
        result = envelope([(3, 3), (2, 2), (1, 3), (1, 3)])
        spawns = result["players"]["7"]["deployment_timing"]["spawns"]
        self.assertEqual([(s["offset_s"], s["id"]) for s in spawns], [(2, 2), (3, 1), (3, 1), (3, 3)])

    def test_negative_and_after_end_offsets_remain_but_disqualify_sample(self):
        result = envelope([(1, -1), (2, 0), (1, 601)])
        timing = result["players"]["7"]["deployment_timing"]
        self.assertTrue(timing["available"])
        self.assertFalse(timing["complete"])
        self.assertEqual((timing["before_start"], timing["after_end"]), (1, 1))
        self.assertEqual([s["offset_s"] for s in timing["spawns"]], [-1, 0, 601])
        result = aggregate_openings(7, [result])
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(len(result["unusable_matches"]), 1)

    def test_end_time_deployment_is_valid(self):
        timing = envelope([(1, 600)])["players"]["7"]["deployment_timing"]
        self.assertTrue(timing["complete"])
        self.assertEqual(timing["after_end"], 0)

    def test_missing_or_invalid_spawn_times_are_retained_as_unknown(self):
        for value in (None, True, False, 0, -1, "1792845493", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value):
                props = match_fixture([(1, 0), (2, 3)])
                props["initialMatchData"]["matchInfo"]["Data"]["7"]["UnitData"]["9999"]["SpawnTime"] = value
                timing = parse_fixture(props)["players"]["7"]["deployment_timing"]
                self.assertTrue(timing["available"])
                self.assertFalse(timing["complete"])
                self.assertEqual(timing["missing_spawn_times"], 1)
                self.assertEqual(len(timing["spawns"]), 2)
                self.assertIsNone(timing["spawns"][-1]["offset_s"])
        timing = envelope([(1, None)])["players"]["7"]["deployment_timing"]
        self.assertFalse(timing["available"])
        self.assertFalse(timing["complete"])

    def test_missing_unit_id_and_malformed_instance_are_not_lost(self):
        props = match_fixture([(None, 2), (1, 3)])
        props["initialMatchData"]["matchInfo"]["Data"]["7"]["UnitData"]["malformed"] = None
        timing = parse_fixture(props)["players"]["7"]["deployment_timing"]
        self.assertFalse(timing["complete"])
        self.assertEqual(timing["missing_unit_ids"], 2)
        self.assertEqual(timing["missing_spawn_times"], 1)
        self.assertEqual(len(timing["spawns"]), 3)
        self.assertIsNone(timing["spawns"][0]["id"])

    def test_missing_registry_names_and_types_do_not_invalidate_known_time(self):
        props = match_fixture([(1, 5)])
        props["initialUnitRegistry"] = {}
        timing = parse_fixture(props)["players"]["7"]["deployment_timing"]
        self.assertTrue(timing["complete"])
        self.assertEqual(timing["spawns"], [{"id": 1, "name": None, "type": None, "offset_s": 5}])

    def test_empty_unitdata_is_unavailable_not_zero(self):
        for value in ({}, [], None, "bad"):
            with self.subTest(value=value):
                props = match_fixture()
                props["initialMatchData"]["matchInfo"]["Data"]["7"]["UnitData"] = value
                timing = parse_fixture(props)["players"]["7"]["deployment_timing"]
                self.assertFalse(timing["available"])
                self.assertFalse(timing["complete"])
                self.assertEqual(timing["spawns"], [])

    def test_invalid_bounds_prevent_complete_timeline(self):
        for field, value in (("StartTime", None), ("EndTime", None), ("StartTime", True),
                             ("StartTime", float("inf")), ("EndTime", START - 1)):
            with self.subTest(field=field, value=value):
                props = match_fixture()
                props["initialMatchData"]["matchInfo"][field] = value
                result = parse_fixture(props)
                timing = result["players"]["7"]["deployment_timing"]
                self.assertFalse(timing["complete"])
                self.assertFalse(timing["available"])
                self.assertEqual(len(timing["spawns"]), 3)

    def test_optional_saved_match_opening_matches_expected_fields(self):
        fixture = load_optional_fixture(self, 'opening')
        result = parse_match_html(Path(fixture['path']).read_text(encoding='utf-8'), fixture['match_id'])
        opening = aggregate_openings(fixture['player_id'], [result], [fixture['match_id']])
        assert_expected_fields(self, {'match': result, 'opening': opening}, fixture['expected'])


class OpeningAggregationTests(unittest.TestCase):
    def test_window_is_half_open_and_changeable_without_reparse(self):
        match = envelope([(1, 0), (1, 119), (2, 120), (3, 179), (3, 180)])
        result = aggregate_openings(7, [match], window_seconds=120)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual([(u["id"], u["total_spawns"]) for u in result["units"]], [(1, 2)])
        result = aggregate_openings(7, [match], window_seconds=180)
        self.assertEqual({u["id"]: u["total_spawns"] for u in result["units"]}, {1: 2, 2: 1, 3: 1})

    def test_valid_zero_opening_match_enters_denominator(self):
        matches = [envelope([(1, 5), (1, 15)]), envelope([(2, 200)], match_id="43")]
        result = aggregate_openings(7, matches, ["42", "43"])
        self.assertEqual((result["sample_count"], result["requested_count"]), (2, 2))
        self.assertEqual(result["units"][0], {
            "id": 1, "name": "飞机甲", "type": 16,
            "matches_with_unit": 1, "total_spawns": 2, "mean_count": 1.0,
            "first_offset_median": 5, "match_ids": ["42"],
        })
        self.assertEqual(result["matches"][1], {"match_id": "43", "source_url": "https://dash.batrace.top/match/43", "match_end_time": START + 600})

    def test_short_match_excluded_but_exact_window_duration_is_valid(self):
        short = envelope([(1, 5)], end=START + 119)
        exact = envelope([(2, 120)], match_id="43", end=START + 120)
        result = aggregate_openings(7, [short, exact], ["42", "43"], window_seconds=120)
        self.assertEqual((result["sample_count"], result["requested_count"]), (1, 2))
        self.assertEqual([m["match_id"] for m in result["matches"]], ["43"])
        self.assertEqual(result["units"], [])
        self.assertEqual(result["unusable_matches"], [{
            "match_id": "42", "reason": "对局提前结束，未覆盖完整开局观察窗口",
        }])
        # The same preserved timeline is usable with a shorter requested window.
        self.assertEqual(aggregate_openings(7, [short], window_seconds=60)["sample_count"], 1)

    def test_first_offset_median_is_per_match_not_per_instance(self):
        result = aggregate_openings(7, [
            envelope([(1, 2), (1, 3), (1, 4)]),
            envelope([(1, 20)], match_id="43"),
        ])
        unit = result["units"][0]
        self.assertEqual(unit["first_offset_median"], 11)
        self.assertEqual((unit["matches_with_unit"], unit["total_spawns"], unit["mean_count"]), (2, 4, 2))

    def test_sorting_is_presence_then_counts_then_first_time_then_id(self):
        result = aggregate_openings(7, [
            envelope([(1, 10), (2, 5), (3, 4), (3, 4), (4, 4)]),
            envelope([(1, 10), (2, 5), (4, 4)], match_id="43"),
        ])
        self.assertEqual([u["id"] for u in result["units"]], [4, 2, 1, 3])
        # Exact ties are stable by unit ID; no claim about order within a second.
        result = aggregate_openings(7, [envelope([(3, 4), (1, 4), (2, 4)])])
        self.assertEqual([u["id"] for u in result["units"]], [1, 2, 3])

    def test_duplicate_matches_and_requests_count_once_prefer_complete(self):
        good = envelope([(1, 2)])
        bad = envelope([(1, None)])
        result = aggregate_openings(7, [bad, good, deepcopy(good)], ["42", 42, "42"])
        self.assertEqual((result["sample_count"], result["requested_count"]), (1, 1))
        self.assertEqual(result["units"][0]["total_spawns"], 1)
        self.assertEqual(result["unusable_matches"], [])

    def test_failed_missing_and_wrong_player_matches_excluded(self):
        missing_player = envelope(match_id="44")
        missing_player["players"] = {}
        result = aggregate_openings(7, [envelope([(1, 3)]), envelope([(1, None)], match_id="43"), missing_player], ["42", "43", "44", "45"])
        self.assertEqual((result["sample_count"], result["requested_count"]), (1, 4))
        self.assertEqual([m["match_id"] for m in result["unusable_matches"]], ["43", "44", "45"])
        self.assertEqual(result["unusable_matches"][-1]["reason"], "未取得对局数据")

    def test_unrequested_matches_do_not_change_requested_sample(self):
        result = aggregate_openings(7, [envelope(), envelope(match_id="43")], ["42"])
        self.assertEqual((result["sample_count"], result["requested_count"]), (1, 1))
        self.assertEqual([m["match_id"] for m in result["matches"]], ["42"])

    def test_old_cache_or_inconsistent_complete_claim_does_not_enter_denominator(self):
        mutations = [
            lambda m: m.pop("recon_version"),
            lambda m: m["players"]["7"].update(available=False),
            lambda m: m["players"]["7"].update(player_id=8),
            lambda m: m["players"]["7"]["deployment_timing"].update(spawns=[]),
            lambda m: m["players"]["7"]["deployment_timing"].update(missing_spawn_times=1),
            lambda m: m["players"]["7"]["deployment_timing"].update(before_start=True),
            lambda m: m["players"]["7"]["deployment_timing"]["spawns"][0].update(offset_s=-1),
            lambda m: m["players"]["7"]["deployment_timing"]["spawns"][0].update(offset_s=float("nan")),
            lambda m: m["players"]["7"]["deployment_timing"]["spawns"][0].update(offset_s=True),
            lambda m: m["players"]["7"]["deployment_timing"]["spawns"][0].update(offset_s=601),
            lambda m: m["players"]["7"]["deployment_timing"]["spawns"][0].update(id=True),
            lambda m: m.update(match_start_time=START + 1),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                match = envelope([(1, 3)])
                mutate(match)
                result = aggregate_openings(7, [match])
                self.assertEqual(result["sample_count"], 0)
                self.assertEqual(len(result["unusable_matches"]), 1)

    def test_no_samples_never_divides_by_zero(self):
        self.assertEqual(aggregate_openings(7, []) , {
            "sample_count": 0, "requested_count": 0, "window_seconds": 120,
            "units": [], "matches": [], "unusable_matches": [],
        })
        self.assertEqual(aggregate_openings(7, [envelope([(1, 200)])])["units"], [])

    def test_bad_function_parameters_are_rejected(self):
        for player_id in (True, 0, -1, "7", 7.0):
            with self.subTest(player_id=player_id), self.assertRaises(ValueError):
                aggregate_openings(player_id, [])
        for window in (True, False, 0, -1, "120", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(window=window), self.assertRaises(ValueError):
                aggregate_openings(7, [], window_seconds=window)
        for requested in ([True], [0], ["../42"], "42"):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                aggregate_openings(7, [], requested)

    def test_aggregation_does_not_mutate_envelopes(self):
        matches = [envelope([(2, 5), (1, 3)])]
        before = deepcopy(matches)
        aggregate_openings(7, matches, ["42"])
        self.assertEqual(matches, before)


if __name__ == "__main__":
    unittest.main()
