"""Meaningful parser/watcher regressions; all scratch files stay under the project work directory."""

from pathlib import Path
from tests.optional_fixtures import load_optional_fixture, assert_expected_fields
import os
import tempfile
import unittest

from ba_scout.roster import LogWatcher, parse_latest_roster, recent_roster


PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = PROJECT / "work" / "roster-tests"


def block(fight="42", ids=None, names=None, terminate=True):
    ids = list(range(1, 11)) if ids is None else ids
    names = [f"玩家 {i}" for i in ids] if names is None else names
    lines = [
        "[2026-10-24 20:36:30.815]",
        f"Log: [NET_ROOM] GameRoom entered, fight id: {fight}",
        "",
        "[2026-10-24 20:36:30.816]",
        "Log: Player list: ",
    ]
    lines.extend(
        f"ID: {pid}, Name: {name}, Team: {'Alpha' if index < 5 else 'Bravo'}"
        for index, (pid, name) in enumerate(zip(ids, names))
    )
    return "\n".join(lines) + ("\n\n" if terminate else "")


class ParserTests(unittest.TestCase):
    def test_complete_unicode_and_commas(self):
        names = ["样例", "中文，逗号, ascii, Team: fake", "||ExAmP||"] + [f"N{i}" for i in range(7)]
        result = parse_latest_roster(block(names=names), "sample.log")
        self.assertEqual(result.fight_id, "42")
        self.assertEqual(result.players[1]["name"], names[1])
        self.assertEqual(result.log_timestamp, "2026-10-24 20:36:30.816")
        self.assertEqual(result.log_path, "sample.log")
        self.assertTrue(result.detected_at.endswith("+00:00"))

    def test_partial_block_and_partial_last_line_are_not_published(self):
        self.assertIsNone(parse_latest_roster(block(terminate=False)))
        first = block("11")
        second = block("12")
        self.assertEqual(parse_latest_roster(first + second[:-20]).fight_id, "11")

    def test_old_match_id_cannot_bind_to_next_list(self):
        unidentified = block("12").split("Log: Player list: ", 1)[1]
        text = block("11") + "Log: Player list: \n" + unidentified
        self.assertEqual(parse_latest_roster(text).fight_id, "11")
        text = "Log: FID:11 v1.2.1\nLog: Start loading battle...\nLog: Player list: \n" + unidentified
        self.assertIsNone(parse_latest_roster(text))

    def test_invalid_rosters(self):
        duplicate = list(range(1, 10)) + [1]
        self.assertIsNone(parse_latest_roster(block(ids=duplicate)))
        self.assertIsNone(parse_latest_roster(block().replace("Team: Alpha", "Team: Bravo", 1)))
        eleven = block().rstrip("\n") + "\nID: 11, Name: Extra, Team: Bravo\n\n"
        self.assertIsNone(parse_latest_roster(eleven))
        self.assertIsNone(parse_latest_roster(block().replace("ID: 10,", "ID: broken,")))

    def test_new_match_does_not_complete_old_partial_block(self):
        partial = block("1").split("ID: 6,", 1)[0]
        roster = parse_latest_roster(partial + block("2"))
        self.assertEqual(roster.fight_id, "2")
        self.assertEqual(len(roster.players), 10)

    def test_network_marker_supported(self):
        text = block().replace(
            "Log: [NET_ROOM] GameRoom entered, fight id: 42",
            "Log: [NET_ROOM] Network lifecycle | Stage=Game | Action=Entered | RoomType=GameRoom | FightId=42 | Status=WaitClients",
        )
        self.assertEqual(parse_latest_roster(text).fight_id, "42")

    def test_real_recent_log(self):
        fixture = load_optional_fixture(self, 'roster')
        path = Path(fixture['path'])
        result = parse_latest_roster(path.read_text(encoding='utf-8'), str(path))
        self.assertIsNotNone(result)
        assert_expected_fields(self, vars(result), fixture['expected'])


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRATCH.resolve().is_relative_to((PROJECT / "work").resolve()))
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=SCRATCH)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "Gamelog__2026_10_24__20_16.log"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, content, path=None):
        (path or self.path).write_bytes(content.encode("utf-8"))

    def append(self, content):
        with self.path.open("ab") as handle:
            handle.write(content.encode("utf-8"))

    def test_skip_history_and_read_new_append(self):
        self.write(block("1"))
        watcher = LogWatcher(str(self.directory))
        self.assertEqual(watcher.poll(), [])
        self.append(block("2"))
        self.assertEqual([r.fight_id for r in watcher.poll()], ["2"])
        self.assertEqual(watcher.poll(), [])

    def test_include_history_is_explicit(self):
        self.write(block("1"))
        watcher = LogWatcher(str(self.directory), include_existing=True)
        self.assertEqual([r.fight_id for r in watcher.poll()], ["1"])

    def test_new_file_before_first_poll_is_read_from_start(self):
        watcher = LogWatcher(str(self.directory))
        self.write(block("2"))
        self.assertEqual([r.fight_id for r in watcher.poll()], ["2"])

    def test_incremental_utf8_and_roster_lines(self):
        self.path.write_bytes(b"")
        watcher = LogWatcher(str(self.directory))
        data = block("3").encode("utf-8")
        cut = data.index("玩".encode("utf-8")) + 1
        with self.path.open("ab") as handle:
            handle.write(data[:cut])
        self.assertEqual(watcher.poll(), [])
        with self.path.open("ab") as handle:
            handle.write(data[cut:-1])
        self.assertEqual(watcher.poll(), [])
        with self.path.open("ab") as handle:
            handle.write(data[-1:])
        result = watcher.poll()
        self.assertEqual(result[0].players[0]["name"], "玩家 1")

    def test_file_rotation_and_reconnect_deduplication(self):
        self.write(block("1"))
        watcher = LogWatcher(str(self.directory), include_existing=True)
        self.assertEqual(len(watcher.poll()), 1)
        newer = self.directory / "Gamelog__2026_10_24__21_00.log"
        self.write(block("1") + block("2"), newer)
        now_ns = self.path.stat().st_mtime_ns + 1_000_000_000
        os.utime(newer, ns=(now_ns, now_ns))
        self.assertEqual([r.fight_id for r in watcher.poll()], ["2"])

    def test_truncation_and_regrow_are_detected(self):
        self.write(block("12345"))
        watcher = LogWatcher(str(self.directory), include_existing=True)
        watcher.poll()
        self.write(block("2"))
        self.assertEqual([r.fight_id for r in watcher.poll()], ["2"])
        self.write("Log: replacement header\n" + block("33333333333333333333"))
        self.assertEqual([r.fight_id for r in watcher.poll()], ["33333333333333333333"])

    def test_reordered_roster_and_name_change_do_not_repeat_match(self):
        self.write(block("1"))
        watcher = LogWatcher(str(self.directory), include_existing=True)
        watcher.poll()
        ids = [5, 4, 3, 2, 1, 10, 9, 8, 7, 6]
        self.append(block("1", ids=ids, names=["Changed"] * 10))
        self.assertEqual(watcher.poll(), [])
        self.append(block("1", ids=[6, 2, 3, 4, 5, 1, 7, 8, 9, 10]))
        self.assertEqual(len(watcher.poll()), 1)

    def test_recent_roster_does_not_fall_back_without_explicit_opt_in(self):
        self.write(block("1"))
        newer = self.directory / "Gamelog__2026_10_24__21_00.log"
        self.write("Log: starting\n", newer)
        now_ns = self.path.stat().st_mtime_ns + 1_000_000_000
        os.utime(newer, ns=(now_ns, now_ns))
        self.assertIsNone(recent_roster(str(self.directory)))
        self.assertEqual(recent_roster(str(self.directory), allow_older=True).fight_id, "1")

    def test_startup_events_restore_only_still_active_latest_roster(self):
        self.write(block("1") + "Log: [NET_ROOM] GameRoom exited\n" + block("2"))
        watcher = LogWatcher(str(self.directory))
        events = watcher.poll_events()
        self.assertEqual([(e["type"], e["phase"], e["fight_id"]) for e in events],
                         [("lifecycle", "game_room", "2"), ("roster", "roster", "2")])
        self.assertTrue(all(e["startup"] for e in events))
        self.assertEqual(events[1]["roster"].fight_id, "2")
        self.assertEqual(watcher.poll_events(), [])

    def test_startup_ended_or_lobby_does_not_emit_live_roster(self):
        for ending, phase in [
            ("Log: [NET_ROOM] Network traffic history | Cause=MatchEnded | FightId=1 | ignored=not retained", "ended"),
            ("Log: [NET_ROOM] GameRoom exited", "lobby"),
        ]:
            with self.subTest(phase=phase):
                self.write(block("1") + ending + "\n")
                events = LogWatcher(str(self.directory)).poll_events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["phase"], phase)
                self.assertEqual(events[0]["type"], "lifecycle")
                self.assertNotIn("ignored", str(events))
                # An explicitly requested historical test may still use the
                # most recent completed match from this exact file.
                self.assertEqual(recent_roster(str(self.directory)).fight_id, "1")

    def test_new_waiting_room_blocks_old_manual_roster(self):
        self.write(block("1") + "[2026-10-25 19:47:10.107]\n"
                   "Log: [NET_ROOM] Network lifecycle | Stage=Game | Action=Entered | RoomType=WaitingRoom | FightId=2 | Status=WaitClients\n"
                   "[2026-10-25 19:47:10.109]\n"
                   "Log: [NET_ROOM] WaitingRoom entered, fight id: 2\n")
        self.assertIsNone(recent_roster(str(self.directory)))
        events = LogWatcher(str(self.directory)).poll_events()
        self.assertEqual([(e["phase"], e["fight_id"]) for e in events], [("waiting", "2")])
        self.assertEqual(events[0]["log_timestamp"], "2026-10-25 19:47:10.107")

    def test_loading_startup_has_no_old_roster(self):
        self.write(block("1") + "Log: Start loading battle...\n")
        events = LogWatcher(str(self.directory)).poll_events()
        self.assertEqual([(e["phase"], e["fight_id"]) for e in events], [("loading", None)])
        self.assertIsNone(recent_roster(str(self.directory)))

    def test_lifecycle_and_roster_events_keep_log_order(self):
        self.write("")
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        self.append("Log: [NET_ROOM] WaitingRoom entered, fight id: 2\n"
                    "Log: Start loading battle...\n" + block("2") +
                    "Log: [NET_ROOM] Network traffic history | Cause=MatchEnded | FightId=2\n"
                    "Log: [NET_ROOM] GameRoom exited\n")
        events = watcher.poll_events()
        self.assertEqual([e["phase"] for e in events],
                         ["waiting", "loading", "game_room", "roster", "ended", "lobby"])
        self.assertEqual([e["fight_id"] for e in events], ["2", "2", "2", "2", "2", None])
        self.assertTrue(all(e["log_path"] == str(self.path) for e in events))

    def test_start_between_game_room_marker_and_roster_retains_context(self):
        prefix, suffix = block("8").split("Log: Player list:", 1)
        self.write(prefix)
        watcher = LogWatcher(str(self.directory))
        startup = watcher.poll_events()
        self.assertEqual(startup[0]["phase"], "game_room")
        self.assertEqual(startup[0]["fight_id"], "8")
        self.append("Log: Player list:" + suffix)
        events = watcher.poll_events()
        self.assertEqual([e["roster"].fight_id for e in events if e["type"] == "roster"], ["8"])

    def test_partial_replacement_list_is_not_restored_as_old_complete_list(self):
        replacement = block("1").split("ID: 6,", 1)[0]
        self.write(block("1") + replacement)
        events = LogWatcher(str(self.directory)).poll_events()
        self.assertFalse(any(e["type"] == "roster" for e in events))
        self.assertIsNone(recent_roster(str(self.directory)))

    def test_start_during_utf8_player_line_finishes_current_list(self):
        data = block("9").encode("utf-8")
        cut = data.index("玩".encode("utf-8")) + 1
        self.path.write_bytes(data[:cut])
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        with self.path.open("ab") as handle:
            handle.write(data[cut:])
        events = watcher.poll_events()
        roster = next(e["roster"] for e in events if e["type"] == "roster")
        self.assertEqual(roster.fight_id, "9")
        self.assertEqual(roster.players[0]["name"], "玩家 1")

    def test_new_file_before_first_event_poll_discards_old_startup_state(self):
        self.write(block("1"))
        watcher = LogWatcher(str(self.directory))
        newer = self.directory / "Gamelog__2026_10_25__19_44.log"
        self.write("Log: [NET_ROOM] WaitingRoom entered, fight id: 2\n", newer)
        now_ns = self.path.stat().st_mtime_ns + 1_000_000_000
        os.utime(newer, ns=(now_ns, now_ns))
        events = watcher.poll_events()
        self.assertFalse(any(e["type"] == "roster" for e in events))
        self.assertEqual(events[-1]["phase"], "waiting")
        self.assertEqual(events[-1]["fight_id"], "2")

    def test_reconnect_can_restore_invalidated_display_but_legacy_dedups(self):
        self.write(block("1"))
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        self.append("Log: Start reconnect to battle...\n" + block("1"))
        events = watcher.poll_events()
        restored = next(e for e in events if e["type"] == "roster")
        self.assertTrue(restored["reconnected"])
        self.append("Log: Start reconnect to battle...\n" + block("1"))
        self.assertEqual(watcher.poll(), [])

    def test_stale_match_ended_does_not_clear_new_waiting_room(self):
        self.write(block("1") + "Log: [NET_ROOM] WaitingRoom entered, fight id: 2\n"
                   "Log: [NET_ROOM] Network traffic history | Cause=MatchEnded | FightId=1\n")
        events = LogWatcher(str(self.directory)).poll_events()
        self.assertEqual([(e["phase"], e["fight_id"]) for e in events], [("waiting", "2")])

    def test_delayed_old_game_room_exit_does_not_clear_new_complete_roster(self):
        self.write(block("1") + block("2"))
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        self.append("[2026-10-25 20:00:00.000]\n"
                    "Log: [NET_ROOM] Network lifecycle | Action=Exited | RoomType=GameRoom | FightId=1 | Status=End\n")
        self.assertEqual(watcher.poll_events(), [])
        # A split write and the game's slightly later companion timestamp are
        # both supported, without reclassifying the current complete match.
        self.append("\n[2026-10-25 20:00:00.001]\nLog: [NET_ROOM] GameRoom exited\n")
        self.assertEqual(watcher.poll_events(), [])
        self.assertEqual(watcher._parser.current_roster.fight_id, "2")
        self.assertEqual(recent_roster(str(self.directory)).fight_id, "2")

    def test_current_identified_game_room_exit_still_clears(self):
        self.write(block("2"))
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        self.append("Log: [NET_ROOM] Network lifecycle | Action=Exited | RoomType=GameRoom | FightId=2 | Status=End\n"
                    "Log: [NET_ROOM] GameRoom exited\n")
        self.assertEqual([e["phase"] for e in watcher.poll_events()], ["lobby"])
        self.assertIsNone(watcher._parser.current_roster)

    def test_bare_legacy_game_room_exit_still_clears(self):
        self.write(block("2"))
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        self.append("Log: [NET_ROOM] GameRoom exited\n")
        self.assertEqual([e["phase"] for e in watcher.poll_events()], ["lobby"])
        self.assertIsNone(watcher._parser.current_roster)

    def test_old_exit_companion_suppression_ends_at_other_log_line(self):
        self.write(block("2"))
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        self.append("[2026-10-25 20:00:00.000]\n"
                    "Log: [NET_ROOM] Network lifecycle | Action=Exited | RoomType=GameRoom | FightId=1 | Status=End\n"
                    "Log: unrelated intervening event\nLog: [NET_ROOM] GameRoom exited\n")
        self.assertEqual([e["phase"] for e in watcher.poll_events()], ["lobby"])
        self.assertIsNone(watcher._parser.current_roster)

    def test_old_exit_companion_suppression_does_not_hide_later_real_exit(self):
        self.write(block("2"))
        watcher = LogWatcher(str(self.directory))
        watcher.poll_events()
        self.append("[2026-10-25 20:00:00.000]\n"
                    "Log: [NET_ROOM] Network lifecycle | Action=Exited | RoomType=GameRoom | FightId=1 | Status=End\n"
                    "\n[2026-10-25 20:00:03.000]\nLog: [NET_ROOM] GameRoom exited\n")
        self.assertEqual([e["phase"] for e in watcher.poll_events()], ["lobby"])
        self.assertIsNone(watcher._parser.current_roster)


if __name__ == "__main__":
    unittest.main()
