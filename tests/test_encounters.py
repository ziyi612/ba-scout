"""Local-only history, temporal filtering, retries, and duplicate match coverage."""
from contextlib import closing
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ba_scout.encounters import EncounterHistory
from ba_scout.roster import Roster

SCRATCH = Path(__file__).resolve().parents[1] / "work" / "encounter-tests"


def roster(fight="81", day=10, reverse=False, ids=None, path="Gamelog__test.log"):
    ids = list(range(1, 11)) if ids is None else ids
    if reverse:
        ids = [1, 7, 8, 9, 10, 6, 2, 3, 4, 5]
    players = [dict(id=pid, name=f"Demo {pid}", team="Alpha" if index < 5 else "Bravo")
               for index, pid in enumerate(ids)]
    return Roster(fight, players, "", path, f"2020-01-{day:02d} 17:01:22.115")


def block(match):
    return (f"[{match.log_timestamp}]\nLog: [NET_ROOM] GameRoom entered, fight id: {match.fight_id}\n"
            "Log: Player list:\n" +
            "".join(f"ID: {p['id']}, Name: {p['name']}, Team: {p['team']}\n" for p in match.players) + "\n")


class EncounterHistoryTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "data" / "encounters.sqlite3"
        self.history = EncounterHistory(self.db, 1)

    def test_roles_renamed_and_replayed_player_id_deduped(self):
        self.assertTrue(self.history.record(roster()))
        self.assertTrue(self.history.record(roster()))
        other = roster("89", 12, reverse=True)
        other.players[6]["name"] = "different spelling"
        self.history.record(other)
        self.assertEqual(self.history.counts([1, 2, 6, "2", 101], "92"),
                         {2: {"ally": 1, "enemy": 1}, 6: {"ally": 0, "enemy": 2}})
        self.assertFalse(self.history.record(roster("98"), replay=True))
        self.assertEqual(EncounterHistory(self.db, 1).counts([2], "92"), {2: {"ally": 1, "enemy": 1}})

    def test_never_match_by_name_or_partial_rosters_or_test_input(self):
        self.history.record(roster())
        self.assertEqual(self.history.counts([11], "81"), {})
        same = roster("91")
        same.players[1]["name"] = same.players[0]["name"]
        for bad in (Roster(same.fight_id, same.players, same.detected_at, same.log_path, same.log_timestamp,
                           source="screen_names"),
                    roster("UI-DEMO"), roster("93", path=""), roster("94", ids=list(range(2,12))),
                    roster("83", ids=[1]*10), roster("84", ids=list(range(1,9)))):
            self.assertFalse(self.history.record(bad))
        self.assertEqual(self.history.counts([2], "99"), {2: {"ally": 1, "enemy": 0}})

    def test_independent_self_account_and_fight_chronology(self):
        self.history.record(roster("99", 10))
        self.history.record(roster("77", 20))
        self.history.record(roster("45", 25))
        self.assertEqual(self.history.counts([2], "77"), {2: {"ally": 1, "enemy": 0}})
        self.assertEqual(self.history.counts([2], "77", "2020-01-09 23:00:00"), {})
        self.assertEqual(self.history.counts([2], "fresh", "invalid time"), {})
        self.assertEqual(self.history.counts([2], "123", "2020-01-10 17:01:22.115"), {})
        other = EncounterHistory(self.db, 6)
        self.assertEqual(other.counts([2], "101"), {})
        other.record(roster("99", 10))
        self.assertEqual(other.counts([2], "101"), {2: {"ally": 0, "enemy": 1}})

    def test_conflicting_authoritative_roster_skipped_without_double_counting(self):
        self.history.record(roster("81", 10))
        self.history.record(roster("81", 11, reverse=True))
        self.assertEqual(self.history.counts([2, 3], "95"), {})
        self.history.record(roster("81"))
        self.assertEqual(self.history.counts([2], "95"), {})

    def test_backfill_multiple_rosters_incremental_append_and_log_rotation(self):
        path = self.dir / "Gamelog__test.log"
        path.write_text(block(roster()) + block(roster("89", 12, reverse=True)), encoding="utf-8")
        (self.dir / "ignored-file.log").write_text(block(roster("67")), encoding="utf-8")
        self.assertEqual(self.history.import_logs(self.dir), 1)
        with patch("ba_scout.encounters._read_observations", side_effect=AssertionError("rescanned unchanged log")):
            self.assertEqual(self.history.import_logs(self.dir), 0)
        self.assertEqual(self.history.counts([2], "91"), {2: {"ally": 1, "enemy": 1}})
        with path.open("a", encoding="utf-8") as stream:
            stream.write(block(roster("90", 15)))
        self.history.record(roster("90", 15))
        self.assertEqual(self.history.import_logs(self.dir), 1)
        path.unlink()
        self.assertEqual(self.history.import_logs(self.dir), 0)
        self.assertEqual(EncounterHistory(self.db, 1).counts([2], "100"), {2: {"ally": 2, "enemy": 1}})

    def test_invalid_or_truncated_blocks_not_counted(self):
        p = self.dir / "Gamelog__partial.log"
        p.write_text(block(roster()).rstrip("\n") + "\n", encoding="utf-8")
        self.history.import_logs(self.dir)
        self.assertEqual(self.history.counts([2], "99"), {})
        with p.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        self.history.import_logs(self.dir)
        self.assertEqual(self.history.counts([2], "99"), {2: {"ally": 1, "enemy": 0}})

    def test_live_write_with_busy_database_is_recovered(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.history.record(roster()))
        deadline = time.monotonic() + 2
        while not self.history.counts([2], "99") and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.history.counts([2], "99"), {2: {"ally": 1, "enemy": 0}})
