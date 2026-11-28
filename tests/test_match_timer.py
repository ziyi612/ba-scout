import unittest

from ba_scout.match_timer import MatchTimer, format_elapsed, parse_reminder


class MatchTimerTests(unittest.TestCase):
    def test_parses_optional_reminder(self):
        self.assertIsNone(parse_reminder(" "))
        self.assertEqual(parse_reminder("2:30"), 150)
        self.assertEqual(parse_reminder(" 100：01 "), 6001)
        for invalid in ("0:00", "1:60", "-1:01", "2:3", "90"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_reminder(invalid)

    def test_reminder_fires_once_even_after_tick_delay(self):
        timer = MatchTimer(100.5, 30)
        self.assertEqual(timer.tick(129.99), (29, False))
        self.assertEqual(timer.tick(135.0), (34, True))
        self.assertEqual(timer.tick(135.1), (34, False))

    def test_next_run_rearms_reminder(self):
        timer = MatchTimer(100, 30)
        self.assertEqual(timer.tick(130), (30, True))
        timer = MatchTimer()
        self.assertEqual(timer.tick(170), (0, False))
        timer = MatchTimer(170, 30)
        self.assertEqual(timer.tick(200), (30, True))
        self.assertEqual(timer.tick(201), (31, False))

    def test_elapsed_without_a_reminder(self):
        timer = MatchTimer(30)
        self.assertEqual(timer.tick(6321), (6291, False))
        self.assertEqual(format_elapsed(6291), "104:51")
