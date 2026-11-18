"""Offline provider tests: identity, missing facts, RSC boundaries and HTTP limits."""

import io
import json
import unittest
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from tests.optional_fixtures import load_optional_fixture, assert_expected_fields
from unittest.mock import patch
from urllib.error import HTTPError

from ba_scout.provider import MAX_HTML_BYTES, PlayerDataError, fetch_player, parse_player_html


def page_fixture(data=None, *, matches=None, names=None, split=False):
    if data is None:
        data = {
            "info": {"id": 810001, "name": "样例", "rating": 2100.5, "rank": 123},
            "statInfo": {
                "playerId": 810001, "fightsCountRt": 12, "winCountRt": 7,
                "lossCountRt": 4, "leaveCountRt": 1,
                "updatedAt": "$D2026-10-25T06:08:36.095Z",
                "combinedSpecsExp": {"5_6": 100000, "4_5": 90000},
            },
            "last_fights_data": ["10", "9", "8"],
        }
    records = [
        ["$", "div", None, {"playerData": data}],
        {"specNames": names if names is not None else {"4": "装甲旅级战斗队", "5": "空降师", "6": "斯特瑞克旅"}},
        ["$", "div", None, {"matchMap": matches or {}, "stbid": 810001}],
    ]
    stream = "".join(f"{i:x}:" + json.dumps(record, ensure_ascii=False) + "\n" for i, record in enumerate(records))
    fragments = [stream[:37], stream[37:149], stream[149:]] if split else [stream]
    return "<html>" + "".join(
        "<script>self.__next_f.push(" + json.dumps([1, fragment], ensure_ascii=False) + ")</script>"
        for fragment in fragments
    ) + "</html>"


class FakeResponse(io.BytesIO):
    def __init__(self, raw, declared_length=None):
        super().__init__(raw)
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        if declared_length is not None:
            self.headers["Content-Length"] = str(declared_length)


class ChunkedResponse(FakeResponse):
    def __init__(self, chunks):
        super().__init__(b"")
        self.chunks = list(chunks)
        self.read_calls = 0

    def read1(self, size=-1):
        self.read_calls += 1
        if not self.chunks:
            return b""
        item = self.chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        if 0 <= size < len(item):
            self.chunks.insert(0, item[size:])
            return item[:size]
        return item


def rsc_script(record_id, value):
    stream = f"{record_id}:" + json.dumps(value, ensure_ascii=False) + "\n"
    return ("<script>self.__next_f.push(" + json.dumps([1, stream], ensure_ascii=False) + ")</script>").encode("utf-8")


class ProviderTests(unittest.TestCase):
    def test_stream_merges_verified_fields_and_publishes_history_before_full_statistics(self):
        first = {"playerData": {"info": {"id": 71, "rank": 42}, "last_fights_data": [91, 90],
                                "statInfo": {"playerId": 71, "winCountRt": 1}},
                 "specNames": {"4": "装甲", "5": "空降"}}
        second = {"playerData": {"info": {"id": 71, "rating": 1700},
                                 "statInfo": {"playerId": 71, "lossCountRt": 1, "fightsCountRt": 2,
                                              "combinedSpecsExp": {"4_5": 80}}}}
        other = {"playerData": {"info": {"id": 99, "rating": 9999}, "last_fights_data": [1000],
                                "statInfo": {"playerId": 99, "winCountRt": 50}}}
        response = ChunkedResponse([rsc_script("1", first) + rsc_script("2", other), rsc_script("3", second),
                                    AssertionError("The remaining HTML is optional")])
        seen = []
        with patch("ba_scout.provider.urlopen", return_value=response):
            result = fetch_player(71, on_progress=lambda data: seen.append((response.read_calls, data)))
        self.assertEqual(len(seen), 1)
        at, early = seen[0]
        self.assertEqual((at, early["player_id"], early["recent_match_ids"]), (1, 71, ["91", "90"]))
        self.assertFalse(early["core_ready"])
        self.assertEqual((result["rank"], result["rating"], result["wins"], result["ranked_games"]), (42, 1700, 1, 2))
        self.assertEqual(result["specs"][0]["key"], "4_5")
        self.assertTrue(result["core_ready"])
        self.assertTrue(result["recent_history_available"])

    def test_missing_invalid_and_empty_recent_lists_have_distinct_meanings(self):
        for data, available in (({}, False), ({"last_fights_data": "$ff"}, False),
                                ({"last_fights_data": ["bad"]}, False), ({"last_fights_data": []}, True)):
            player = {"info": {"id": 71}, "statInfo": {"playerId": 71, "winCountRt": 1}, **data}
            with self.subTest(data=data):
                profile = parse_player_html(rsc_script("1", {"playerData": player}).decode(), 71)
                self.assertEqual(profile["recent_history_available"], available)
                self.assertFalse(profile["core_ready"])
                self.assertEqual(profile["recent_match_ids"], [])

    def test_recent_ids_available_without_match_map_and_deduplicated(self):
        profile = {"info": {"id": 810001}, "statInfo": {"playerId": 810001},
                   "last_fights_data": [True, 0, "0031", 31, "bad", "30", 29, -1, "28", 27, 26]}
        parsed = parse_player_html(page_fixture(profile), 810001)
        self.assertEqual(parsed["recent_match_ids"], ["31", "30", "29", "28", "27"])
        self.assertEqual(parsed["last_match_id"], "31")
        self.assertEqual(parsed["recent"], [])

    def test_complete_profile_and_split_rsc(self):
        result = parse_player_html(page_fixture(split=True), 810001)
        self.assertEqual(result["name"], "样例")
        self.assertEqual(result["ranked_games"], 12)
        self.assertEqual(result["leaves"], 1)
        self.assertEqual(result["stats_updated_at"], "2026-10-25T06:08:36.095Z")
        self.assertEqual(result["specs"][0], {
            "key": "5_6", "label": "空降师 + 斯特瑞克旅", "experience": 100000,
        })
        self.assertNotIn("frequency", result["specs"][0])
        self.assertIsNone(result["fetched_at"])
        self.assertTrue(result["core_ready"])
        self.assertTrue(result["spec_names_available"])

    def test_missing_values_stay_unknown_not_zero(self):
        result = parse_player_html(page_fixture({"info": {"id": 810001}}), 810001)
        for key in ("name", "rating", "rank", "wins", "losses", "leaves", "ranked_games", "stats_updated_at"):
            self.assertIsNone(result[key], key)
        self.assertEqual(result["recent"], [])
        self.assertEqual(result["specs"], [])
        self.assertFalse(result["core_ready"])

    def test_additional_profile_fields_missing_are_unknown(self):
        result = parse_player_html(page_fixture({"info": {"id": 810001}}), 810001)
        self.assertEqual(result["profile_version"], 4)
        for key in ("ranked_kills", "ranked_losses_units", "ranked_killed_value", "ranked_lost_value", "ranked_duration_seconds", "last_match_id"):
            self.assertIsNone(result[key], key)
        self.assertEqual(result["nation_experience"], {})

    def test_zero_losses_and_unknown_nations_are_preserved_without_ratios(self):
        data = {"info": {"id": 810001}, "statInfo": {
            "playerId": 810001, "unitsKilledCountRt": 5, "unitsLostCountRt": 0,
            "unitsKilledCostRt": 1234, "unitsLostCostRt": 0,
            "matchDurationTimeSecRt": 300, "nationExp": {"1": 0, "2": 300, "999": 45},
        }}
        result = parse_player_html(page_fixture(data), 810001)
        self.assertEqual(result["ranked_kills"], 5)
        self.assertEqual(result["ranked_losses_units"], 0)
        self.assertEqual(result["ranked_killed_value"], 1234)
        self.assertEqual(result["ranked_lost_value"], 0)
        self.assertEqual(result["ranked_duration_seconds"], 300)
        self.assertEqual(result["nation_experience"], {"1": 0, "2": 300, "999": 45})
        self.assertNotIn("ranked_value_ratio", result)

    def test_first_valid_match_id_is_numeric_and_not_a_boolean(self):
        for values, expected in (
            ([None, True, False, 0, -4, 3.5, "", "x123", "-7", "12.0", "000", "009000042", "12"], "9000042"),
            ([None, 123, "456"], "123"),
            (["１２３", " 123 ", None, False, 0], None),
            ("9000042", None),
        ):
            with self.subTest(values=values):
                data = {"info": {"id": 810001}, "last_fights_data": values}
                result = parse_player_html(page_fixture(data), 810001)
                self.assertEqual(result["last_match_id"], expected)

    def test_profile_identity_must_match(self):
        with self.assertRaisesRegex(PlayerDataError, "different player ID"):
            parse_player_html(page_fixture(), 123)

    def test_statistics_identity_must_match(self):
        data = {"info": {"id": 810001}, "statInfo": {"playerId": 9}}
        with self.assertRaisesRegex(PlayerDataError, "disagree"):
            parse_player_html(page_fixture(data), 810001)

    def test_unknown_labels_keep_combination_id(self):
        result = parse_player_html(page_fixture(names={"5": "空降师"}), 810001)
        self.assertEqual(result["specs"][0]["label"], "专精组合 5_6")

    def test_zero_experience_is_not_a_historical_preference(self):
        data = {"info": {"id": 810001}, "statInfo": {
            "playerId": 810001, "combinedSpecsExp": {"5_6": 0, "4_5": 0.0},
        }}
        result = parse_player_html(page_fixture(data), 810001)
        self.assertEqual(result["specs"], [])
        data["statInfo"]["combinedSpecsExp"]["4_5"] = 1
        result = parse_player_html(page_fixture(data), 810001)
        self.assertEqual([row["key"] for row in result["specs"]], ["4_5"])

    def test_recent_missing_team_is_unknown_even_if_rating_rises(self):
        matches = {
            "9": {"WinnerTeam": 1, "Data": {"810001": {"Id": 810001, "TeamId": 0, "OldRating": 2000, "NewRating": 1980}}},
            "10": {"WinnerTeam": 1, "Data": {"810001": {"Id": 810001, "OldRating": 1980, "NewRating": 2010}}},
            "8": {"WinnerTeam": 1, "Data": {"810001": {"Id": 810001, "TeamId": 1}}},
        }
        result = parse_player_html(page_fixture(matches=matches), 810001)
        self.assertEqual(result["recent"], [
            {"match_id": "10", "outcome": "unknown", "rating_delta": 30},
            {"match_id": "9", "outcome": "loss", "rating_delta": -20},
            {"match_id": "8", "outcome": "win", "rating_delta": None},
        ])

    def test_untrusted_javascript_is_never_evaluated(self):
        malicious = '<script>self.__next_f.push(__import__("os").system("bad"))</script>'
        with self.assertRaises(PlayerDataError):
            parse_player_html(malicious, 810001)

    def test_bad_ids_and_timeout_rejected(self):
        for player_id in (0, -1, True, "810001"):
            with self.subTest(player_id=player_id), self.assertRaises(ValueError):
                fetch_player(player_id)
        for timeout in (0, -1, float("nan"), True, "12"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                fetch_player(810001, timeout)

    def test_fetch_populates_provenance_with_one_request(self):
        raw = page_fixture().encode("utf-8")
        with patch("ba_scout.provider.urlopen", return_value=FakeResponse(raw)) as mocked:
            result = fetch_player(810001, timeout=2)
        self.assertEqual(mocked.call_count, 1)
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://dash.batrace.top/player/810001")
        self.assertEqual(mocked.call_args.kwargs["timeout"], 2.0)
        self.assertTrue(result["fetched_at"].endswith("Z"))
        self.assertGreaterEqual(result["fetch_seconds"], 0)
        self.assertTrue(result["partial_page"])
        self.assertTrue(result["recent_partial"])

    def test_stream_returns_before_slow_tail_after_verified_core_and_names(self):
        profile = {"playerData": {"info": {"id": 810001, "name": "样例"},
                    "statInfo": {"playerId": 810001, "winCountRt": 1, "fightsCountRt": 2,
                                 "combinedSpecsExp": {"5_6": 100}}, "last_fights_data": []}}
        response = ChunkedResponse([
            rsc_script("1", profile),
            rsc_script("2", {"specNames": {"5": "空降师", "6": "斯特瑞克旅"}}),
            TimeoutError("The slow recent-match tail must not be read"),
        ])
        with patch("ba_scout.provider.urlopen", return_value=response):
            result = fetch_player(810001)
        self.assertEqual(response.read_calls, 2)
        self.assertTrue(response.closed)
        self.assertEqual(result["specs"][0]["label"], "空降师 + 斯特瑞克旅")
        self.assertTrue(result["recent_partial"])
        self.assertEqual(result["recent"], [])

    def test_stream_utf8_character_and_script_boundary_split(self):
        raw = page_fixture(split=True).encode("utf-8")
        chinese = raw.index("样".encode("utf-8"))
        closing = raw.rindex(b"</script>")
        response = ChunkedResponse([
            raw[:chinese + 1], raw[chinese + 1:chinese + 2],
            raw[chinese + 2:closing + 5], raw[closing + 5:],
            TimeoutError("Do not wait for EOF"),
        ])
        with patch("ba_scout.provider.urlopen", return_value=response):
            result = fetch_player(810001)
        self.assertEqual(result["name"], "样例")
        self.assertEqual(response.read_calls, 4)
        self.assertTrue(result["partial_page"])

    def test_eof_falls_back_when_specialization_names_are_absent(self):
        response = ChunkedResponse([page_fixture(names={}).encode("utf-8"), b""])
        with patch("ba_scout.provider.urlopen", return_value=response):
            result = fetch_player(810001)
        self.assertEqual(response.read_calls, 2)
        self.assertEqual(result["name"], "样例")
        self.assertEqual(result["specs"][0]["label"], "专精组合 5_6")
        self.assertFalse(result["partial_page"])
        self.assertFalse(result["recent_partial"])

    def test_info_only_page_does_not_finish_early(self):
        response = ChunkedResponse([
            page_fixture({"info": {"id": 810001}}).encode("utf-8"),
            TimeoutError("stats have not arrived"),
        ])
        with patch("ba_scout.provider.urlopen", return_value=response):
            result = fetch_player(810001)
        self.assertEqual(response.read_calls, 2)
        self.assertTrue(result['profile_incomplete'])
        self.assertFalse(result['core_ready'])
        self.assertEqual(result['read_warning'], 'TimeoutError')
        self.assertTrue(result['recent_partial'])

    def test_info_only_eof_is_not_successful_core_statistics(self):
        response = ChunkedResponse([page_fixture({"info": {"id": 810001, "name": "样例"}}).encode(), b""])
        with patch("ba_scout.provider.urlopen", return_value=response) as mocked:
            result = fetch_player(810001)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(response.read_calls, 2)
        self.assertTrue(result['profile_incomplete'])
        self.assertFalse(result['core_ready'])
        self.assertFalse(result['partial_page'])
        self.assertIsNone(result['wins'])

    def test_statistics_id_without_any_statistic_is_not_core_ready(self):
        data = {"info": {"id": 810001}, "statInfo": {"playerId": 810001}}
        self.assertFalse(parse_player_html(page_fixture(data), 810001)["core_ready"])
        with patch("ba_scout.provider.urlopen", return_value=FakeResponse(page_fixture(data).encode())):
            result = fetch_player(810001)
        self.assertTrue(result['profile_incomplete'])
        self.assertFalse(result['core_ready'])

    def test_incomplete_read_retains_only_received_complete_basic_model(self):
        profile = {'info': {'id': 810001, 'name': '样例', 'rating': 1800.25, 'rank': 9876},
                   'statInfo': None, 'last_fights_data': ['100', '99']}
        raw = page_fixture(profile).encode('utf-8')
        split = len(raw) // 2
        response = ChunkedResponse([raw[:split], IncompleteRead(raw[split:], 100)])
        with patch('ba_scout.provider.urlopen', return_value=response) as mocked:
            result = fetch_player(810001)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(result['recent_match_ids'], ['100', '99'])
        self.assertEqual(result['rating'], 1800.25)
        self.assertEqual(result['rank'], 9876)
        self.assertTrue(result['profile_incomplete'])
        self.assertFalse(result['core_ready'])
        self.assertEqual(result['read_warning'], 'IncompleteRead')
        self.assertTrue(result['partial_page'])
        self.assertIsNone(result['wins'])

    def test_read_failure_without_complete_model_still_fails(self):
        for error in (TimeoutError('tail'), IncompleteRead(b'{"playerData":', 100)):
            response = ChunkedResponse([b'<html><script>', error])
            with self.subTest(error=type(error).__name__), patch('ba_scout.provider.urlopen', return_value=response):
                with self.assertRaises(type(error)):
                    fetch_player(810001)

    def test_incomplete_read_cannot_hide_wrong_id_or_size_limit(self):
        bad = page_fixture({'info': {'id': 999, 'name': 'wrong'}}).encode()
        for tail, pattern in ((bad, 'different player'), (b'x' * (MAX_HTML_BYTES + 1), 'size limit')):
            response = ChunkedResponse([IncompleteRead(tail, 100)])
            with self.subTest(pattern=pattern), patch('ba_scout.provider.urlopen', return_value=response):
                with self.assertRaisesRegex(PlayerDataError, pattern):
                    fetch_player(810001)

    def test_incomplete_read_cannot_hide_conflicting_statistics_id(self):
        raw = page_fixture({'info': {'id': 810001}, 'statInfo': {'playerId': 999, 'winCountRt': 1}}).encode()
        response = ChunkedResponse([IncompleteRead(raw, 100)])
        with patch('ba_scout.provider.urlopen', return_value=response):
            with self.assertRaisesRegex(PlayerDataError, 'disagree'):
                fetch_player(810001)

    def test_later_complete_model_is_not_shadowed_by_early_identity_only_model(self):
        first = {"playerData": {"info": {"id": 810001, "name": "样例"}}}
        second = {"playerData": {"info": {"id": 810001, "name": "样例"},
                                 "statInfo": {"playerId": 810001, "winCountRt": 9, "fightsCountRt": 12,
                                              "combinedSpecsExp": {}}, "last_fights_data": []}}
        response = ChunkedResponse([rsc_script("1", first), rsc_script("2", second),
                                    rsc_script("3", {"specNames": {"5": "空降师"}}), TimeoutError("tail")])
        with patch("ba_scout.provider.urlopen", return_value=response):
            parsed = fetch_player(810001)
        self.assertEqual(parsed["wins"], 9)
        self.assertTrue(parsed["core_ready"])
        self.assertEqual(response.read_calls, 3)

    def test_separate_rsc_profile_and_statistics_models_resolve_before_return(self):
        response = ChunkedResponse([
            rsc_script("1", {"playerData": "$@2", "specNames": "$5"}),
            rsc_script("2", {"info": "$3", "statInfo": "$@4", "last_fights_data": "$6"}),
            rsc_script("3", {"id": 810001, "name": "$1", "rating": 2100}),
            rsc_script("4", {"playerId": 810001, "winCountRt": 10, "lossCountRt": 0, "fightsCountRt": 10,
                             "combinedSpecsExp": "$7:experience", "nationExp": "$8"}),
            rsc_script("5", {"5": "空降师", "6": "斯特瑞克旅"}),
            rsc_script("6", ["123", "122"]),
            rsc_script("7", {"experience": {"5_6": 100}}),
            rsc_script("8", {"1": 300}),
        ])
        # Statistics/names are sufficient to stop early. Test complete offline
        # parsing separately to verify referenced optional containers as well.
        raw = b"".join(response.chunks).decode()
        parsed = parse_player_html(raw, 810001)
        self.assertEqual(parsed["name"], "$1", "Literal nickname text must not become a model")
        self.assertEqual(parsed["wins"], 10)
        self.assertEqual(parsed["losses"], 0)
        self.assertEqual(parsed["recent_match_ids"], ["123", "122"])
        self.assertEqual(parsed["specs"][0]["experience"], 100)
        self.assertEqual(parsed["nation_experience"], {"1": 300})
        with patch("ba_scout.provider.urlopen", return_value=response):
            fetched = fetch_player(810001)
        self.assertTrue(fetched["core_ready"])
        self.assertEqual(fetched["wins"], 10)
        self.assertEqual(response.read_calls, 8)
        self.assertEqual(fetched["specs"][0]["experience"], 100)

    def test_missing_or_cyclic_structured_refs_stay_unavailable(self):
        for reference, extra in (("$99", b""), ("$2:forward", rsc_script("2", {"forward": "$1:playerData:statInfo"}))):
            with self.subTest(reference=reference):
                raw = rsc_script("1", {"playerData": {"info": {"id": 810001}, "statInfo": reference}}) + extra
                parsed = parse_player_html(raw.decode(), 810001)
                self.assertFalse(parsed["core_ready"])
                self.assertIsNone(parsed["wins"])

    def test_referenced_statistics_wrong_id_is_rejected_even_after_other_ready_model(self):
        raw = (rsc_script("1", {"playerData": {"info": {"id": 810001},
                                                "statInfo": {"playerId": 810001, "winCountRt": 1}}})
               + rsc_script("2", {"playerData": {"info": {"id": 810001}, "statInfo": "$3"}})
               + rsc_script("3", {"playerId": 999, "winCountRt": 100}))
        with self.assertRaisesRegex(PlayerDataError, "disagree"):
            parse_player_html(raw.decode(), 810001)

    def test_explicit_zero_counts_are_available_statistics(self):
        data = {"info": {"id": 810001}, "statInfo": {"playerId": 810001,
                "winCountRt": 0, "lossCountRt": 0, "fightsCountRt": 0}}
        with patch("ba_scout.provider.urlopen", return_value=FakeResponse(page_fixture(data, names={}).encode())):
            parsed = fetch_player(810001)
        self.assertTrue(parsed["core_ready"])
        self.assertEqual(parsed["wins"], 0)
        self.assertEqual(parsed["ranked_games"], 0)
        self.assertFalse(parsed["partial_page"])

    def test_zero_experience_profile_can_finish_early(self):
        data = {"info": {"id": 810001}, "statInfo": {
            "playerId": 810001, "combinedSpecsExp": {"5_6": 0}, "winCountRt": 0, "fightsCountRt": 0,
        }, "last_fights_data": []}
        response = ChunkedResponse([page_fixture(data).encode("utf-8"), TimeoutError("tail")])
        with patch("ba_scout.provider.urlopen", return_value=response):
            result = fetch_player(810001)
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(result["specs"], [])
        self.assertTrue(result["core_ready"])
        self.assertTrue(result["recent_partial"])

    def test_http_429_propagates_without_retry(self):
        error = HTTPError("https://dash.batrace.top/player/810001", 429, "Too Many Requests", {}, None)
        with patch("ba_scout.provider.urlopen", side_effect=error) as mocked:
            with self.assertRaises(HTTPError) as caught:
                fetch_player(810001)
        self.assertIs(caught.exception, error)
        self.assertEqual(mocked.call_count, 1)

    def test_size_limit_with_or_without_content_length(self):
        for response in (
            FakeResponse(b"", declared_length=MAX_HTML_BYTES + 1),
            FakeResponse(b"x" * (MAX_HTML_BYTES + 1)),
        ):
            with self.subTest(declared=response.headers.get("Content-Length")):
                with patch("ba_scout.provider.urlopen", return_value=response):
                    with self.assertRaisesRegex(PlayerDataError, "size limit"):
                        fetch_player(810001)

    def test_real_captured_page_when_available(self):
        fixture = load_optional_fixture(self, 'player')
        result = parse_player_html(Path(fixture['path']).read_text(encoding='utf-8'), fixture['player_id'])
        assert_expected_fields(self, result, fixture['expected'])


if __name__ == "__main__":
    unittest.main()
