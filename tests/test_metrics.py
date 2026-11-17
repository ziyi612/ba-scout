import math
import unittest

from ba_scout.metrics import profile_metrics, tied_top_count


class ProfileMetricsTests(unittest.TestCase):
    def test_synthetic_values_have_distinct_value_and_unit_ratios(self):
        # Fixed synthetic counters exercise distinct metric denominators.
        data = {
            "wins": 21, "leaves": 2, "ranked_games": 40,
            "ranked_kills": 123, "ranked_losses_units": 99,
            "ranked_killed_value": 15000, "ranked_lost_value": 20000,
            "ranked_duration_seconds": 72000,
            "nation_experience": {"1": 123000, "2": 45600},
        }
        result = profile_metrics(data)
        self.assertAlmostEqual(result["win_rate"], 52.5)
        self.assertAlmostEqual(result["leave_rate"], 5.0)
        self.assertAlmostEqual(result["value_exchange"], 0.75)
        self.assertAlmostEqual(result["unit_kd"], 123 / 99)
        self.assertAlmostEqual(result["average_minutes"], 30)
        self.assertEqual(result["nation_experience_line"], "国家经验：俄 123,000 · 美 45,600")

    def test_legacy_cache_missing_metrics_remains_compatible(self):
        result = profile_metrics({"player_id": 810001, "rating": 2000})
        for key in ("win_rate", "leave_rate", "value_exchange", "unit_kd", "average_minutes"):
            self.assertIsNone(result[key], key)
        self.assertEqual(result["nation_experience_line"], "国家经验：未取得")

    def test_zero_or_missing_loss_denominators_are_unknown(self):
        for denominator in (0, None):
            with self.subTest(denominator=denominator):
                result = profile_metrics({
                    "ranked_killed_value": 100, "ranked_lost_value": denominator,
                    "ranked_kills": 10, "ranked_losses_units": denominator,
                })
                self.assertIsNone(result["value_exchange"])
                self.assertIsNone(result["unit_kd"])

    def test_actual_zero_numerators_remain_zero(self):
        result = profile_metrics({
            "wins": 0, "leaves": 0, "ranked_games": 10,
            "ranked_killed_value": 0, "ranked_lost_value": 100,
            "ranked_kills": 0, "ranked_losses_units": 1,
            "ranked_duration_seconds": 0,
        })
        for key in ("win_rate", "leave_rate", "value_exchange", "unit_kd", "average_minutes"):
            self.assertEqual(result[key], 0.0, key)

    def test_impossible_counts_do_not_become_percentages_over_100(self):
        result = profile_metrics({"wins": 11, "leaves": 12, "ranked_games": 10})
        self.assertIsNone(result["win_rate"])
        self.assertIsNone(result["leave_rate"])

    def test_invalid_values_are_rejected_in_both_ratio_operands(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, False, -1, "12", 10 ** 1000):
            for role in ("numerator", "denominator"):
                with self.subTest(bad=str(bad)[:30], role=role):
                    top, bottom = (bad, 10) if role == "numerator" else (5, bad)
                    result = profile_metrics({
                        "wins": top, "leaves": top, "ranked_games": bottom,
                        "ranked_killed_value": top, "ranked_lost_value": bottom,
                        "ranked_kills": top, "ranked_losses_units": bottom,
                        "ranked_duration_seconds": top,
                    })
                    for key in ("win_rate", "leave_rate", "value_exchange", "unit_kd", "average_minutes"):
                        self.assertIsNone(result[key], key)

    def test_overflowing_ratio_is_unknown(self):
        result = profile_metrics({"ranked_killed_value": 1e308, "ranked_lost_value": 1e-308})
        self.assertIsNone(result["value_exchange"])

    def test_unknown_country_ids_are_not_named_or_converted_to_probabilities(self):
        result = profile_metrics({"nation_experience": {"999": 123456}})
        self.assertEqual(result["nation_experience_line"], "国家经验：未取得")
        result = profile_metrics({"nation_experience": {"1": 0, "999": 123456}})
        self.assertEqual(result["nation_experience_line"], "国家经验：俄 0")
        self.assertNotIn("%", result["nation_experience_line"])

    def test_bad_country_values_are_not_printed_as_numbers(self):
        for bad in (None, True, float("nan"), float("inf"), -4, "100"):
            with self.subTest(bad=bad):
                result = profile_metrics({"nation_experience": {"1": bad, "2": 12}})
                self.assertEqual(result["nation_experience_line"], "国家经验：美 12")

    def test_unsuffixed_supply_counters_do_not_create_ranked_metrics(self):
        result = profile_metrics({"ranked_games": 10, "supplyPtsConsumed": 1000, "zoneCapturedCount": 20})
        self.assertIsNone(result["value_exchange"])
        self.assertIsNone(result["unit_kd"])
        self.assertEqual(set(result), {"win_rate", "leave_rate", "value_exchange", "unit_kd", "average_minutes", "nation_experience_line"})


class SpecializationTieTests(unittest.TestCase):
    def test_top_ties_only_count_positive_finite_experience(self):
        specs = [{"experience": x} for x in (100000, 80000, 100000, 0, -1, True, float("nan"), float("inf"))]
        self.assertEqual(tied_top_count(specs), 2)
        self.assertEqual(tied_top_count([{"experience": 5}, {"experience": 3}]), 1)

    def test_missing_invalid_and_zero_values_have_no_top_preference(self):
        for specs in (None, {}, [], [None, "x", {}, {"experience": 0}, {"experience": False}]):
            with self.subTest(specs=specs):
                self.assertEqual(tied_top_count(specs), 0)


if __name__ == "__main__":
    unittest.main()
