"""Offline identity, ambiguity and transport safeguards for public name search."""

import io
import json
import math
from email.message import Message
from pathlib import Path
from tests.optional_fixtures import load_optional_fixture, assert_expected_fields
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from ba_scout.player_search import (
    MAX_NAME_LENGTH, MAX_RESPONSE_BYTES, SEARCH_LIMIT, SEARCH_URL, LEGACY_SEARCH_LIMIT,
    PlayerSearchError, SearchResults, normalize_name, parse_search_payload, resolve_exact_name, search_players,
)
from ba_scout.request_gate import RequestGate


def payload(*players):
    return {"players": list(players), "count": len(players)}


class Response(io.BytesIO):
    def __init__(self, data, declared_length=None):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        super().__init__(raw)
        self.headers = Message()
        if declared_length is not None:
            self.headers["Content-Length"] = str(declared_length)
        self.read_calls = 0

    def read(self, size=-1):
        self.read_calls += 1
        return super().read(size)


class SearchTests(unittest.TestCase):
    def test_synthetic_schema_strips_stats_and_steam_id(self):
        data = payload({"id": 810001, "name": "样例", "rating": 2125.3,
                        "steam_id": "76561190000000000", "updated_at": "2026-07-07T15:48:16.000Z"},
                       {"id": 810002, "name": "样例期", "rating": 338.928})
        self.assertEqual(parse_search_payload(data), [{"id": 810001, "name": "样例"}, {"id": 810002, "name": "样例期"}])

    def test_actual_saved_response_if_available(self):
        fixture = load_optional_fixture(self, 'search')
        saved = json.loads(Path(fixture['path']).read_text(encoding='utf-8-sig'))
        candidates = parse_search_payload(saved['data'])
        result = resolve_exact_name(fixture['names'], candidates)
        assert_expected_fields(self, result, fixture['expected'])

    def test_search_url_encoding_timeout_and_identity_only(self):
        response = Response(payload({"id": 1, "name": "甲 & 乙", "rating": 1000}))
        with patch("ba_scout.player_search.urlopen", return_value=response) as opened:
            found = search_players("  甲　& 乙  ", timeout=2.5)
            self.assertEqual(found, [{"id": 1, "name": "甲 & 乙"}])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url.split("?")[0], SEARCH_URL)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"q": ["甲 & 乙"], "limit": ["50"]})
        self.assertEqual(found.metadata(), {"query": "甲 & 乙", "requested_limit": 50,
                         "returned_count": 1, "complete": True, "completeness_basis": "below_requested_limit"})
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.kwargs, {"timeout": 2.5})
        self.assertIn("BA-Scout", request.get_header("User-agent"))
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertTrue(response.closed)

    def test_invalid_name_and_timeout_do_not_request(self):
        with patch("ba_scout.player_search.urlopen") as opened:
            for name in (None, 3, "", " \n\t ", "a" * (MAX_NAME_LENGTH + 1), "a\x00b", "a\ud800"):
                with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                    search_players(name)
            for timeout in (None, True, "4", 0, -1, math.nan, math.inf):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    search_players("valid", timeout=timeout)
            opened.assert_not_called()

    def test_declared_size_limit_stops_before_body_read(self):
        response = Response(b"", MAX_RESPONSE_BYTES + 1)
        with patch("ba_scout.player_search.urlopen", return_value=response):
            with self.assertRaisesRegex(PlayerSearchError, "size limit"):
                search_players("valid")
        self.assertEqual(response.read_calls, 0)
        self.assertTrue(response.closed)

    def test_actual_size_limit_without_content_length(self):
        response = Response(b" " * (MAX_RESPONSE_BYTES + 1))
        with patch("ba_scout.player_search.urlopen", return_value=response):
            with self.assertRaisesRegex(PlayerSearchError, "size limit"):
                search_players("valid")

    def test_malformed_json_utf8_and_schema_raise(self):
        for data in (b"<html>error</html>", b"\xff", b"{}", b"[]", b"null"):
            with self.subTest(data=data), patch("ba_scout.player_search.urlopen", return_value=Response(data)):
                with self.assertRaises(PlayerSearchError):
                    search_players("valid")

    def test_403_429_and_timeout_are_not_retried_or_replaced(self):
        for error in (HTTPError(SEARCH_URL, 403, "denied", {}, None),
                      HTTPError(SEARCH_URL, 429, "limited", {}, None), TimeoutError("timed out")):
            with self.subTest(error=error), patch("ba_scout.player_search.urlopen", side_effect=error) as opened:
                with self.assertRaises(type(error)) as caught:
                    search_players("valid")
                self.assertIs(caught.exception, error)
                self.assertEqual(opened.call_count, 1)

    def test_existing_shared_gate_stops_search_and_other_fetch_after_429(self):
        gate = RequestGate(clock=lambda: 0)
        error = HTTPError(SEARCH_URL, 429, "limited", {}, None)
        with patch("ba_scout.player_search.urlopen", side_effect=error) as opened:
            with self.assertRaises(HTTPError):
                gate.run(search_players, "valid", timeout=4)
            with self.assertRaisesRegex(RuntimeError, "限流"):
                gate.run(search_players, "another")
            with self.assertRaisesRegex(RuntimeError, "限流"):
                gate.run(lambda: self.fail("profile/match request must not run"))
            self.assertEqual(opened.call_count, 1)

    def test_invalid_candidate_is_not_silently_dropped(self):
        invalid = [{"id": True, "name": "valid"}, {"id": 0, "name": "valid"},
                   {"id": -2, "name": "valid"}, {"id": 1.0, "name": "valid"},
                   {"id": "1", "name": "valid"}, {"id": 1, "name": None},
                   {"id": 1, "name": " "}, {"id": 1, "name": "a\x00b"}, None]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(PlayerSearchError):
                parse_search_payload(payload({"id": 2, "name": "valid"}, row))

    def test_count_mismatch_missing_or_over_limit_rejected(self):
        for data in ({"players": []}, {"players": [], "count": True},
                     {"players": [], "count": -1}, {"players": [], "count": 10},
                     payload(*({"id": i + 1, "name": str(i)} for i in range(SEARCH_LIMIT + 1)))):
            with self.subTest(data=data), self.assertRaises(PlayerSearchError):
                parse_search_payload(data)
        self.assertEqual(parse_search_payload(payload()), [])

    def test_explicit_limit_validated_and_legacy_default_retained(self):
        rows = [{"id": i + 1, "name": f"player{i}"} for i in range(21)]
        with self.assertRaises(PlayerSearchError):
            parse_search_payload(payload(*rows))
        found = parse_search_payload(payload(*rows), requested_limit=50, query="player")
        self.assertEqual((found.requested_limit, found.returned_count, found.complete), (50, 21, True))
        for value in (True, 0, 51, 50.0, "50", None):
            with self.subTest(value=value), self.assertRaises(PlayerSearchError):
                parse_search_payload(payload(), requested_limit=value)

    def test_full_fifty_http_response_does_not_start_followup_request(self):
        rows = [{"id": i + 1, "name": f"player{i}"} for i in range(50)]
        with patch("ba_scout.player_search.urlopen", return_value=Response(payload(*rows))) as opened:
            found = search_players("player0")
        self.assertEqual(opened.call_count, 1)
        self.assertFalse(found.complete)
        self.assertEqual(found.metadata()["completeness_basis"], "at_requested_limit")
        self.assertEqual(resolve_exact_name(["player0"], found)["status"], "incomplete")


class ResolutionTests(unittest.TestCase):
    @staticmethod
    def batch(query, count, *, limit=50, exact=True, base=1000):
        rows = [{"id": base + i, "name": f"unrelated-{base}-{i}"} for i in range(count)]
        if exact and rows:
            rows[0] = {"id": 810003, "name": "Laser"}
        return parse_search_payload(payload(*rows), requested_limit=limit, query=query)

    def test_twenty_results_only_complete_when_requested_limit_is_known_larger(self):
        verified = self.batch("Laser", 20)
        result = resolve_exact_name(["Laser"], verified)
        self.assertEqual((result["status"], result["player_id"]), ("resolved", 810003))
        self.assertEqual(result["queries"][0]["requested_limit"], 50)
        self.assertTrue(result["queries"][0]["complete"])
        for rows in (list(verified), self.batch("Laser", 20, limit=20)):
            with self.subTest(type=type(rows).__name__):
                result = resolve_exact_name(["Laser"], rows)
                self.assertEqual(result["status"], "incomplete")
                self.assertIsNone(result["player_id"])
                self.assertEqual(result["queries"][0]["requested_limit"], LEGACY_SEARCH_LIMIT)

    def test_merged_small_complete_pages_are_not_a_full_page(self):
        first = self.batch("Laser", 30)
        second = self.batch("Laser-", 30, exact=False, base=2000)
        result = resolve_exact_name(["Laser", "Laser-"], first + second,
                                    query_results=[("Laser", first), ("Laser-", second)])
        self.assertEqual((result["status"], result["player_id"]), ("resolved", 810003))
        self.assertEqual(len(result["candidates"]), 60)
        self.assertEqual([item["returned_count"] for item in result["queries"]], [30, 30])
        self.assertTrue(all(item["complete"] for item in result["queries"]))

    def test_each_query_page_must_be_complete_even_if_other_page_has_exact_name(self):
        first = self.batch("Laser", 1)
        for second in (self.batch("Laser-", 50, exact=False),
                       list(self.batch("Laser-", 20, exact=False))):
            result = resolve_exact_name(["Laser", "Laser-"], first + second,
                                        query_results=[("Laser", first), ("Laser-", second)])
            self.assertEqual(result["status"], "incomplete")
            self.assertIsNone(result["player_id"])
            self.assertFalse(result["queries"][1]["complete"])

    def test_every_required_variant_must_have_exactly_one_complete_batch(self):
        first = self.batch("Laser", 1)
        second = self.batch("Laser-", 0, exact=False)
        extra = self.batch("other", 0, exact=False)
        for batches in ([('Laser', first)], [('Laser', first), ('Laser', first)],
                        [('Laser', first), ('other', extra)],
                        [('Laser', first), ('Laser-', second), ('other', extra)], []):
            with self.subTest(queries=[key for key, _ in batches]):
                candidates = [item for _, found in batches for item in found]
                result = resolve_exact_name(["Laser", "Laser-"], candidates, query_results=batches)
                self.assertEqual(result["status"], "incomplete")
                self.assertIsNone(result["player_id"])

    def test_single_tagged_result_cannot_stand_in_for_unqueried_variant(self):
        result = resolve_exact_name(["Laser", "Laser-"], self.batch("Laser", 1))
        self.assertEqual(result["status"], "incomplete")

    def test_metadata_mismatch_or_dropped_candidates_never_auto_resolves(self):
        for field, value in (("returned_count", 2), ("returned_count", True),
                             ("complete", False), ("complete", 1),
                             ("requested_limit", 100), ("requested_limit", True),
                             ("query", "different")):
            with self.subTest(field=field, value=value):
                found = self.batch("Laser", 1)
                setattr(found, field, value)
                result = resolve_exact_name(["Laser"], found, query_results=[("Laser", found)])
                self.assertEqual(result["status"], "invalid")
                self.assertIsNone(result["player_id"])
        found = self.batch("Laser", 2)
        result = resolve_exact_name(["Laser"], found[:1], query_results=[("Laser", found)])
        self.assertEqual(result["status"], "invalid")

    def test_missing_provenance_and_unknown_list_subclass_keep_legacy_cap(self):
        for field in ("requested_limit", "returned_count", "complete"):
            found = self.batch("Laser", 20)
            delattr(found, field)
            result = resolve_exact_name(["Laser"], found)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["queries"][0]["completeness_basis"], "legacy_limit")
        class UnknownResults(list):
            complete = True
            requested_limit = 50
            returned_count = 20
        result = resolve_exact_name(["Laser"], UnknownResults(self.batch("Laser", 20)))
        self.assertEqual(result["status"], "incomplete")

    def test_full_metadata_does_not_relax_same_name_multiple_identity_rule(self):
        first = parse_search_payload(payload({"id": 1, "name": "SamplePlayer"}),
                                     requested_limit=50, query="SamplePlayer")
        second = parse_search_payload(payload({"id": 2, "name": "SamplePlayer-"}),
                                      requested_limit=50, query="SamplePlayer-")
        result = resolve_exact_name(["SamplePlayer", "SamplePlayer-"], first + second,
                                    query_results=[("SamplePlayer", first), ("SamplePlayer-", second)])
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["player_id"])

    def test_unique_exact_name_beats_higher_rank_and_first_candidate(self):
        candidates = [{"id": 2, "name": "样例期", "rating": 9999}, {"id": 1, "name": "样例", "rating": 0}]
        result = resolve_exact_name(["样例"], candidates)
        self.assertEqual((result["status"], result["player_id"], result["name"]), ("resolved", 1, "样例"))
        self.assertEqual(result["matches"], [{"id": 1, "name": "样例"}])
        self.assertTrue(all(set(item) == {"id", "name"} for item in result["candidates"]))

    def test_nfkc_and_whitespace_only(self):
        self.assertEqual(normalize_name(" \tＡＢＣ　玩\n家  "), "ABC 玩 家")
        result = resolve_exact_name(" ＡＢＣ　玩家 ", [{"id": 1, "name": "ABC 玩家"}])
        self.assertEqual(result["status"], "resolved")

    def test_same_normalized_name_multiple_ids_is_ambiguous(self):
        result = resolve_exact_name(["ABC 玩家"], [{"id": 1, "name": "ABC 玩家"}, {"id": 2, "name": "ＡＢＣ　玩家"}])
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["player_id"])
        self.assertIsNone(result["name"])
        self.assertEqual(len(result["matches"]), 2)

    def test_distinct_ocr_variants_matching_distinct_ids_are_ambiguous(self):
        result = resolve_exact_name(["pilot0", "pilotO"], [{"id": 1, "name": "pilot0"}, {"id": 2, "name": "pilotO"}])
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["player_id"])

    def test_duplicate_same_id_hits_do_not_create_ambiguity(self):
        result = resolve_exact_name(["ABC", "ＡＢＣ"], [{"id": 1, "name": "ABC"}, {"id": 1, "name": "ＡＢＣ"}])
        self.assertEqual((result["status"], result["player_id"]), ("resolved", 1))
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["normalized_variants"], ["ABC"])

    def test_no_casefold_confusable_punctuation_or_fuzzy_selection(self):
        for observed, candidate in (("Pilot", "pilot"), ("Owl", "0wl"), ("AB", "A-B"),
                                    ("Alice", "Аlice"), ("cafe", "café"), ("样例", "样例期"),
                                    ("AB", "A\u200bB")):
            with self.subTest(observed=observed, candidate=candidate):
                result = resolve_exact_name([observed], [{"id": 1, "name": candidate}])
                self.assertEqual(result["status"], "unmatched")
                self.assertIsNone(result["player_id"])
                self.assertEqual(result["candidates"], [{"id": 1, "name": candidate}])

    def test_empty_results_are_unmatched_not_fake_id_zero(self):
        result = resolve_exact_name(["valid"], [])
        self.assertEqual(result["status"], "unmatched")
        self.assertIsNone(result["player_id"])

    def test_invalid_input_never_resolves_from_remaining_rows(self):
        for variants, candidates in (([], []), ([None], []), (None, []),
                                     (["valid"], None), (["valid"], [{"id": 1, "name": "valid"}, {"id": False, "name": "valid"}])):
            with self.subTest(variants=variants, candidates=candidates):
                result = resolve_exact_name(variants, candidates)
                self.assertEqual(result["status"], "invalid")
                self.assertIsNone(result["player_id"])

    def test_limit_reached_keeps_hints_but_refuses_unique_confirmation(self):
        candidates = [{"id": i + 1, "name": f"player{i}"} for i in range(SEARCH_LIMIT)]
        result = resolve_exact_name(["player0"], candidates)
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["player_id"])
        self.assertEqual(result["matches"], [{"id": 1, "name": "player0"}])
        self.assertEqual(len(result["candidates"]), SEARCH_LIMIT)


if __name__ == "__main__":
    unittest.main()
