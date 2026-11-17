"""Offline match-summary tests, including the saved real ten-player match."""

from email.message import Message
from io import BytesIO
import json
from pathlib import Path
from tests.optional_fixtures import load_optional_fixture, assert_expected_fields
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ba_scout.recon_provider import (
    MAX_HTML_BYTES,
    MatchDataError,
    fetch_match,
    parse_match_html,
)




def fixture():
    return {
        "matchId": "42",
        "initialMatchData": {"matchInfo": {"EndTime": 1792846260, "Data": {
            "7": {"Id": 7, "UnitData": {
                "100": {"Id": 1, "WasRefunded": True},
                "101": {"Id": 1},
                "102": {"Id": 2},
                "103": {"Id": 3},
                "104": {"Id": 4},
            }, "TransferredUnits": {"105": {"Id": 1}}},
        }}},
        "initialUnitRegistry": {
            "1": {"id": 1, "hudName": "Fixed plane", "type": 16},
            "2": {"id": 2, "hudName": "Transport rotor", "type": 8},
            "3": {"id": 3, "hudName": "Infantry", "type": 2},
            "4": {"id": 4, "hudName": "Vehicle", "type": 4},
        },
    }


def html_for(props):
    stream = "1:" + json.dumps(props, ensure_ascii=False) + "\n"
    return "<html><script>self.__next_f.push(" + json.dumps([1, stream], ensure_ascii=False) + ")</script></html>"


class FakeResponse:
    def __init__(self, data, content_length=None, chunk_size=65536):
        self.buffer = BytesIO(data)
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.chunk_size = chunk_size
        self.bytes_read = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.buffer.close()

    def read(self, size):
        data = self.buffer.read(min(size, self.chunk_size))
        self.bytes_read += len(data)
        return data

    read1 = read


class MatchParserTests(unittest.TestCase):
    def test_unique_instances_refunds_and_transfers(self):
        result = parse_match_html(html_for(fixture()), "42")
        player = result["players"]["7"]
        self.assertTrue(player["available"])
        self.assertEqual(player["total_spawns"], 5)
        self.assertEqual((player["fixed_wing"], player["rotorcraft"], player["infantry"], player["vehicles"]), (2, 1, 1, 1))
        self.assertEqual(player["unknown_types"], 0)
        self.assertEqual(player["air_units"][0], {"id": 1, "name": "Fixed plane", "count": 2, "type": 16})
        self.assertEqual(len(player["all_units"]), 4)
        self.assertEqual(result["match_end_time"], 1792846260)
        self.assertIsNone(result["fetched_at"])

    def test_match_and_player_identity_must_agree(self):
        with self.assertRaisesRegex(MatchDataError, "different match"):
            parse_match_html(html_for(fixture()), "43")
        data = fixture()
        data["initialMatchData"]["matchInfo"]["Data"]["7"]["Id"] = 8
        with self.assertRaisesRegex(MatchDataError, "player identity"):
            parse_match_html(html_for(data), "42")

    def test_registry_identity_must_agree(self):
        data = fixture()
        data["initialUnitRegistry"]["1"]["id"] = 2
        with self.assertRaisesRegex(MatchDataError, "registry identity"):
            parse_match_html(html_for(data), "42")

    def test_explicit_unknown_type_is_not_guessed(self):
        data = fixture()
        data["initialUnitRegistry"]["4"]["type"] = 32
        player = parse_match_html(html_for(data), "42")["players"]["7"]
        self.assertEqual(player["vehicles"], 0)
        self.assertEqual(player["unknown_types"], 1)
        self.assertTrue(player["classification_complete"])
        self.assertEqual(player["top_units"][-1]["type"], 32)

    def test_missing_registry_or_type_does_not_invent_zero(self):
        for mutation in ("remove_item", "remove_registry", "remove_type"):
            with self.subTest(mutation=mutation):
                data = fixture()
                if mutation == "remove_item":
                    del data["initialUnitRegistry"]["1"]
                elif mutation == "remove_registry":
                    del data["initialUnitRegistry"]
                else:
                    del data["initialUnitRegistry"]["1"]["type"]
                player = parse_match_html(html_for(data), "42")["players"]["7"]
                self.assertTrue(player["available"])
                self.assertIsNone(player["fixed_wing"])
                self.assertIsNone(player["rotorcraft"])
                self.assertFalse(player["classification_complete"])
                self.assertGreater(player["unknown_types"], 0)
                self.assertEqual(player["known_counts"]["fixed_wing"], 0)

    def test_missing_or_empty_unitdata_is_unavailable_not_zero(self):
        for value in (None, {}, [], "bad"):
            with self.subTest(value=value):
                data = fixture()
                data["initialMatchData"]["matchInfo"]["Data"]["7"]["UnitData"] = value
                player = parse_match_html(html_for(data), "42")["players"]["7"]
                self.assertFalse(player["available"])
                self.assertIsNone(player["total_spawns"])
                self.assertIsNone(player["fixed_wing"])
                self.assertIsNone(player["unknown_types"])
        data = fixture()
        del data["initialMatchData"]["matchInfo"]["Data"]["7"]["UnitData"]
        self.assertFalse(parse_match_html(html_for(data), "42")["players"]["7"]["available"])

    def test_missing_unit_name_is_none_not_fabricated(self):
        data = fixture()
        del data["initialUnitRegistry"]["1"]["hudName"]
        player = parse_match_html(html_for(data), "42")["players"]["7"]
        self.assertIsNone(player["top_units"][0]["name"])
        data["unitNames"] = {"1": "可靠中文名"}
        self.assertEqual(parse_match_html(html_for(data), "42")["players"]["7"]["top_units"][0]["name"], "可靠中文名")

    def test_malformed_record_is_counted_as_unclassified(self):
        data = fixture()
        data["initialMatchData"]["matchInfo"]["Data"]["7"]["UnitData"]["100"] = None
        player = parse_match_html(html_for(data), "42")["players"]["7"]
        self.assertEqual(player["total_spawns"], 5)
        self.assertEqual(player["unknown_types"], 1)
        self.assertIsNone(player["fixed_wing"])

    def test_missing_end_time_stays_unknown(self):
        data = fixture()
        del data["initialMatchData"]["matchInfo"]["EndTime"]
        self.assertIsNone(parse_match_html(html_for(data), "42")["match_end_time"])

    def test_bad_id_and_size_are_rejected(self):
        for match_id in ("", "0", "../42", "42?x=1", 42, True):
            with self.subTest(match_id=match_id), self.assertRaises(ValueError):
                parse_match_html(html_for(fixture()), match_id)
        with self.assertRaisesRegex(MatchDataError, "8 MiB"):
            parse_match_html("x" * (MAX_HTML_BYTES + 1), "42")

    def test_real_ten_player_match(self):
        fixture = load_optional_fixture(self, 'match')
        result = parse_match_html(Path(fixture['path']).read_text(encoding='utf-8'), fixture['match_id'])
        assert_expected_fields(self, result, fixture['expected'])


class MatchFetcherTests(unittest.TestCase):
    def test_one_request_and_early_complete_props(self):
        body = html_for(fixture()).encode("utf-8")
        response = FakeResponse(body + b"padding" * 100000, chunk_size=len(body))
        with patch("ba_scout.recon_provider.urlopen", return_value=response) as opener:
            result = fetch_match("42", timeout=3)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.kwargs["timeout"], 3.0)
        self.assertEqual(response.bytes_read, len(body))
        self.assertEqual(result["match_id"], "42")
        self.assertTrue(result["fetched_at"].endswith("Z"))

    def test_utf8_split_across_reads(self):
        data = fixture()
        data["initialUnitRegistry"]["1"]["hudName"] = "固定翼中文"
        body = html_for(data).encode("utf-8")
        response = FakeResponse(body + b"tail" * 1000, chunk_size=1)
        with patch("ba_scout.recon_provider.urlopen", return_value=response):
            result = fetch_match("42")
        self.assertEqual(result["players"]["7"]["top_units"][0]["name"], "固定翼中文")
        self.assertLessEqual(response.bytes_read, len(body))

    def test_http_error_propagates_without_retry(self):
        error = HTTPError("https://dash.batrace.top/match/42", 429, "Too Many Requests", {}, None)
        with patch("ba_scout.recon_provider.urlopen", side_effect=error) as opener:
            with self.assertRaises(HTTPError) as caught:
                fetch_match("42")
        self.assertIs(caught.exception, error)
        self.assertEqual(opener.call_count, 1)

    def test_declared_and_streamed_size_limits(self):
        response = FakeResponse(b"", content_length=MAX_HTML_BYTES + 1)
        with patch("ba_scout.recon_provider.urlopen", return_value=response), self.assertRaisesRegex(MatchDataError, "8 MiB"):
            fetch_match("42")
        self.assertEqual(response.bytes_read, 0)
        response = FakeResponse(b"x" * (MAX_HTML_BYTES + 1))
        with patch("ba_scout.recon_provider.urlopen", return_value=response), self.assertRaisesRegex(MatchDataError, "8 MiB"):
            fetch_match("42")

    def test_invalid_timeout_never_opens_network(self):
        for value in (0, -1, True, float("nan"), float("inf"), "8"):
            with self.subTest(timeout=value), patch("ba_scout.recon_provider.urlopen") as opener:
                with self.assertRaises(ValueError):
                    fetch_match("42", timeout=value)
                opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
