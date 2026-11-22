"""Personal encounter history from confirmed, local 5v5 game-log rosters."""
from __future__ import annotations

import codecs
from contextlib import closing
from datetime import datetime
import logging
from pathlib import Path
import sqlite3
import threading

from .roster import Roster, _RosterParser

_LOG_PATTERN = "Gamelog__*.log"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS fights (
    owner INTEGER NOT NULL, fight TEXT NOT NULL, first_seen TEXT NOT NULL,
    conflicted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (owner, fight)
);
CREATE TABLE IF NOT EXISTS players (
    owner INTEGER NOT NULL, fight TEXT NOT NULL, player INTEGER NOT NULL,
    ally INTEGER NOT NULL, PRIMARY KEY (owner, fight, player)
);
CREATE INDEX IF NOT EXISTS players_by_id ON players(owner, player, fight);
CREATE TABLE IF NOT EXISTS imported_files (
    owner INTEGER NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
    modified INTEGER NOT NULL, PRIMARY KEY (owner, path)
);
"""


def _log_time(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return stamp.isoformat(timespec="microseconds")


def _observation(roster, owner):
    """Require an authoritative, dated match containing the configured account."""
    if roster.source != "game_log" or not Path(roster.log_path).match(_LOG_PATTERN):
        return None
    fight = str(roster.fight_id)
    seen = _log_time(roster.log_timestamp)
    if not seen or not fight.isascii() or not fight.isdigit() or int(fight) <= 0:
        return None
    lineup = {p.get("id"): p.get("team") for p in roster.players}
    if (len(roster.players) != 10 or len(lineup) != 10 or owner not in lineup
            or any(type(pid) is not int or not 0 < pid <= 2**63-1 for pid in lineup)
            or list(lineup.values()).count("Alpha") != 5 or list(lineup.values()).count("Bravo") != 5):
        return None
    teammates = tuple(sorted((pid, int(team == lineup[owner])) for pid, team in lineup.items() if pid != owner))
    return (str(int(fight)), seen, teammates)


def _read_observations(path, owner):
    parser = _RosterParser(str(path))
    observations = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with path.open("rb") as stream:
        while chunk := stream.read(256 * 1024):
            for event in parser.feed_events(decoder.decode(chunk)):
                if event["type"] == "roster":
                    observation = _observation(event["roster"], owner)
                    if observation:
                        observations.append(observation)
    return observations


class EncounterHistory:
    """Each owner has separate history. Connections belong to the calling thread."""

    def __init__(self, database: Path, self_id: int):
        if type(self_id) is not int or not 0 < self_id <= 2**63-1:
            raise ValueError("A positive numeric player ID is required for encounter history")
        self.database = Path(database)
        self.owner = self_id
        self._writer = threading.Lock()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    def _connect(self, timeout=2.0):
        return sqlite3.connect(self.database, timeout=timeout)

    def _save(self, db, observation):
        fight, seen, teammates = observation
        key = (self.owner, fight)
        cursor = db.execute("INSERT OR IGNORE INTO fights(owner, fight, first_seen) VALUES (?,?,?)", (*key, seen))
        if cursor.rowcount:
            db.executemany("INSERT INTO players VALUES (?,?,?,?)",
                           ((*key, pid, ally) for pid, ally in teammates))
            return
        previous = tuple(db.execute("SELECT player, ally FROM players WHERE owner=? AND fight=? ORDER BY player", key))
        db.execute("UPDATE fights SET first_seen=min(first_seen,?), conflicted=max(conflicted,?) WHERE owner=? AND fight=?",
                   (seen, int(previous != teammates), *key))

    def _persist(self, observation, timeout):
        with closing(self._connect(timeout=timeout)) as db, db:
            self._save(db, observation)

    def _persist_in_background(self, observation):
        try:
            with self._writer:
                self._persist(observation, 3.0)
        except (OSError, sqlite3.Error):
            logging.exception("Could not record encounter; the game log can be imported on the next start")

    def record(self, roster: Roster, *, replay=False):
        """Store one live roster. Reprocessing a fight can never add another encounter."""
        observation = None if replay else _observation(roster, self.owner)
        if observation is None:
            return False
        if self._writer.acquire(blocking=False):
            try:
                try:
                    self._persist(observation, 0.05)
                    return True
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode not in (sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY):
                        raise
            finally:
                self._writer.release()
        threading.Thread(target=self._persist_in_background, args=(observation,), daemon=True,
                         name="encounter-history-write").start()
        return True

    def import_logs(self, log_dir):
        """Backfill all matches, parsing only files changed since this owner's last import.

        This may scan many old logs on first use and belongs on a background worker.
        A file changing during the scan is intentionally not marked fully imported.
        """
        directory = Path(log_dir)
        if not directory.is_dir():
            return 0
        parsed = 0
        with closing(self._connect()) as db:
            saved = {path: (size, modified) for path, size, modified in db.execute(
                "SELECT path, size, modified FROM imported_files WHERE owner=?", (self.owner,))}
            for path in sorted(directory.glob(_LOG_PATTERN), reverse=True):
                try:
                    key = str(path.resolve())
                    stat = path.stat()
                    fingerprint = (stat.st_size, stat.st_mtime_ns)
                    if fingerprint == saved.get(key):
                        continue
                    observations = _read_observations(path, self.owner)
                    after = path.stat()
                except OSError as exc:
                    logging.warning("Skipped unreadable encounter log %s: %s", path.name, exc)
                    continue
                with self._writer, db:
                    for observation in observations:
                        self._save(db, observation)
                    if fingerprint == (after.st_size, after.st_mtime_ns):
                        db.execute("INSERT OR REPLACE INTO imported_files VALUES (?,?,?,?)",
                                   (self.owner, key, *fingerprint))
                parsed += 1
        return parsed

    def counts(self, player_ids, fight_id, before=None):
        """Count distinct earlier fights per confirmed player ID and previous side.

        before, when supplied, is the game-log time of the current/replayed match.
        Unknown dates in a supplied cutoff produce no claims about past meetings.
        """
        ids = sorted({pid for pid in player_ids if type(pid) is int and 0 < pid <= 2**63-1 and pid != self.owner})
        if not ids:
            return {}
        cutoff = datetime.now().isoformat(timespec="microseconds") if before is None else _log_time(before)
        if not cutoff:
            return {}
        fight = str(fight_id or "")
        if fight.isascii() and fight.isdigit():
            fight = str(int(fight))
        placeholders = ",".join("?" for _ in ids)
        query = f"""SELECT p.player, sum(p.ally), count(*) - sum(p.ally)
                    FROM players p JOIN fights f ON p.owner=f.owner AND p.fight=f.fight
                    WHERE p.owner=? AND p.player IN ({placeholders}) AND p.fight<>? AND f.conflicted=0
                      AND f.first_seen < min(?, coalesce(
                          (SELECT first_seen FROM fights WHERE owner=? AND fight=?), ?))
                    GROUP BY p.player"""
        with closing(self._connect(timeout=0.05)) as db:
            return {pid: {"ally": allies, "enemy": enemies}
                    for pid, allies, enemies in db.execute(query, (self.owner, *ids, fight, cutoff, self.owner, fight, cutoff))}
