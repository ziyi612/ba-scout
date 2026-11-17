"""Observed punctuation alternatives use complete exact lookup, with fake I/O."""
from __future__ import annotations

import time
import unittest

from ba_scout.prep_engine import PrepController
from ba_scout.prep_roster import extract_prep_roster
from ba_scout.request_gate import RequestGate
from tests.test_prep_roster import crop_candidates


class PunctuationIdentityTests(unittest.TestCase):
    def run_lookup(self, records, *, observed=("SamplePlayer-", "SamplePlayer"), fail=None):
        value, regions = crop_candidates(*observed)
        target = extract_prep_roster(value, regions)["rows"][5]
        calls = []

        def fake_search(name, timeout):
            calls.append(name)
            if name == fail:
                raise TimeoutError("synthetic required-query failure")
            return [record for record in records if record["name"] == name]

        controller = PrepController(None, RequestGate(), searcher=fake_search,
                                    capture_interval=.01, lookup_budget=1, total_budget=1,
                                    concurrency=1, search_timeout=.2)
        try:
            # These are extractor-produced variants, not hand-written strings:
            # filtering a suppressed candidate here would expose the old bug.
            controller.start("TEST-PUNCTUATION", regions, manual_rows=[target])
            events = []
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                events.extend(controller.poll_events())
                resolved = [event for event in events if event["type"] == "resolved"]
                if resolved:
                    return calls, resolved[-1], events
                time.sleep(.003)
            self.fail("The fake lookup did not finish within its bounded deadline")
        finally:
            controller.close()

    def test_both_observed_names_with_distinct_ids_stay_ambiguous(self):
        calls, final, events = self.run_lookup([
            {"id": 910001, "name": "SamplePlayer"},
            {"id": 910002, "name": "SamplePlayer-"},
        ])
        self.assertCountEqual(calls, ["SamplePlayer", "SamplePlayer-"])
        self.assertEqual(final["rows"][0]["status"], "ambiguous")
        self.assertEqual(final["players"], [])
        self.assertFalse(any(event["type"] == "identity_ready" for event in events))

    def test_only_short_exact_account_resolves_after_both_searches(self):
        calls, final, _ = self.run_lookup([{"id": 910001, "name": "SamplePlayer"}])
        self.assertCountEqual(calls, ["SamplePlayer", "SamplePlayer-"])
        self.assertEqual(final["rows"][0]["status"], "resolved")
        self.assertEqual(final["players"][0]["id"], 910001)
        self.assertEqual(final["players"][0]["name"], "SamplePlayer")

    def test_legal_hyphenated_account_keeps_its_exact_punctuation(self):
        calls, final, _ = self.run_lookup([{"id": 910002, "name": "SamplePlayer-"}])
        self.assertCountEqual(calls, ["SamplePlayer", "SamplePlayer-"])
        self.assertEqual(final["rows"][0]["status"], "resolved")
        self.assertEqual(final["players"][0]["id"], 910002)
        self.assertEqual(final["players"][0]["name"], "SamplePlayer-")

    def test_required_punctuation_alternative_failure_cannot_confirm_the_other(self):
        calls, final, events = self.run_lookup(
            [{"id": 910001, "name": "SamplePlayer"}], fail="SamplePlayer-")
        self.assertCountEqual(calls, ["SamplePlayer", "SamplePlayer-"])
        self.assertNotEqual(final["rows"][0]["status"], "resolved")
        self.assertEqual(final["players"], [])
        self.assertFalse(any(event["type"] == "identity_ready" for event in events))

    def test_letter_suffix_fragment_still_does_not_start_an_extra_search(self):
        calls, final, _ = self.run_lookup([{"id": 910003, "name": "tide233:D"}],
                                         observed=("tide233:D", "tide233"))
        self.assertEqual(calls, ["tide233:D"])
        self.assertEqual(final["players"][0]["name"], "tide233:D")


if __name__ == "__main__":
    unittest.main()
