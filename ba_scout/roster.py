"""Read Broken Arrow's ordinary text logs without modifying game files.

Only explicit match markers and terminated ``Player list`` blocks are used.
The watcher never uploads or retains unrelated log lines.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re


@dataclass(frozen=True)
class Roster:
    fight_id: str
    players: list[dict]
    detected_at: str
    log_path: str
    log_timestamp: str
    source: str = "game_log"
    self_team: str | None = None
    identity_notes: list[str] = field(default_factory=list)


_TIMESTAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2} [^\]]+)\]$")
_PLAYER = re.compile(r"^ID: (\d+), Name: (.*), Team: (Alpha|Bravo)\s*$")
_GAME_ROOM = re.compile(r"^Log: \[NET_ROOM\] GameRoom entered, fight id: (\d+)\b")
_WAITING_ROOM = re.compile(r"^Log: \[NET_ROOM\] WaitingRoom entered, fight id: (\d+)\b")
_FID = re.compile(r"^Log: FID:(\d+)\b")
_FIGHT_FIELD = re.compile(r"\bFightId=(\d+)\b")
_HEADER = "Log: Player list:"
_READ_LIMIT = 4 * 1024 * 1024
_EXIT_COMPANION_MAX_SECONDS = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _key(roster: Roster) -> tuple:
    return (
        roster.fight_id,
        tuple(sorted((p["id"], p["team"]) for p in roster.players)),
    )


class _RosterParser:
    def __init__(self, log_path: str = "") -> None:
        self.log_path = log_path
        self.partial = ""
        self.timestamp = ""
        self.pending_fight: str | None = None
        self.block_fight: str | None = None
        self.block_time = ""
        self.players: list[dict] | None = None
        self.invalid = False
        self.phase = "session"
        self.context_fight: str | None = None
        self.state_timestamp = ""
        self.current_roster: Roster | None = None
        self.latest_roster: Roster | None = None
        self._events: list[dict] = []
        self._ignored_exit_companion_time: str | None = None

    def _is_adjacent_exit_companion(self, line: str) -> bool:
        """Consume one immediate bare exit following an ignored old-fight exit.

        The game writes a structured exit followed by a bare exit, separated by
        a blank line and another timestamp. Any other nonempty line cancels the
        pairing. A one-second timestamp limit prevents suppressing a later real
        exit even when no unrelated log line appeared in between.
        """
        previous = self._ignored_exit_companion_time
        self._ignored_exit_companion_time = None
        if previous is None or line.rstrip() != "Log: [NET_ROOM] GameRoom exited":
            return False
        try:
            elapsed = (datetime.fromisoformat(self.timestamp) - datetime.fromisoformat(previous)).total_seconds()
        except (ValueError, TypeError):
            return False
        return 0 <= elapsed <= _EXIT_COMPANION_MAX_SECONDS

    def _event(self, kind: str, phase: str, fight_id: str | None, timestamp: str | None = None) -> dict:
        return {"type": kind, "phase": phase, "fight_id": fight_id,
                "log_timestamp": self.timestamp if timestamp is None else timestamp,
                "log_path": self.log_path, "detected_at": _now()}

    def _lifecycle(self, phase: str, fight_id: str | None = None) -> None:
        if (phase, fight_id) == (self.phase, self.context_fight):
            return
        self.phase, self.context_fight = phase, fight_id
        self.state_timestamp = self.timestamp
        # A loading/reconnecting room has no confirmed current roster until its
        # own complete list appears, even if it reuses the same fight ID.
        self.current_roster = None
        self._events.append(self._event("lifecycle", phase, fight_id))

    def startup_events(self) -> list[dict]:
        """Only the final state of this file is eligible for startup recovery."""
        event = self._event("lifecycle", self.phase, self.context_fight, self.state_timestamp)
        event["startup"] = True
        output = [event]
        if self.phase == "game_room" and self.current_roster is not None:
            roster = self.current_roster
            event = self._event("roster", "roster", roster.fight_id, roster.log_timestamp)
            event.update(roster=roster, startup=True)
            output.append(event)
        return output

    def _finish(self) -> Roster | None:
        players = self.players
        fight_id = self.block_fight
        valid = (
            players is not None
            and not self.invalid
            and fight_id is not None
            and len(players) == 10
            and len({p["id"] for p in players}) == 10
            and sum(p["team"] == "Alpha" for p in players) == 5
            and sum(p["team"] == "Bravo" for p in players) == 5
        )
        roster = (
            Roster(fight_id, players, _now(), self.log_path, self.block_time)
            if valid else None
        )
        self.players = None
        self.block_fight = None
        self.invalid = False
        return roster

    def _line(self, line: str) -> list[Roster]:
        found: list[Roster] = []
        line = line.rstrip("\r")
        if self.players is not None:
            match = _PLAYER.fullmatch(line)
            if match:
                player_id = int(match[1])
                name = match[2]
                if player_id <= 0 or not name.strip():
                    self.invalid = True
                self.players.append({"id": player_id, "name": name, "team": match[3]})
                return found
            if line.startswith("ID:"):
                self.invalid = True
                return found
            roster = self._finish()
            if roster is not None:
                found.append(roster)
                self.latest_roster = self.current_roster = roster
                self.phase, self.context_fight = "game_room", roster.fight_id
                self.state_timestamp = roster.log_timestamp
                event = self._event("roster", "roster", roster.fight_id, roster.log_timestamp)
                event["roster"] = roster
                self._events.append(event)

        timestamp = _TIMESTAMP.fullmatch(line)
        if timestamp:
            self.timestamp = timestamp[1]
            return found

        if self._ignored_exit_companion_time is not None:
            if not line.strip():
                return found
            if self._is_adjacent_exit_companion(line):
                return found

        if line.rstrip() == _HEADER:
            self.players = []
            self.current_roster = None
            self.block_fight = self.pending_fight
            self.block_time = self.timestamp
            # A new list must have its own explicit match context. Never attach
            # an old match ID to a later unidentified or truncated match block.
            self.pending_fight = None
            return found

        match = _WAITING_ROOM.match(line)
        if match:
            self.pending_fight = None
            self._lifecycle("waiting", match[1])
            return found
        match = _GAME_ROOM.match(line)
        if match:
            self.pending_fight = match[1]
            self._lifecycle("game_room", match[1])
            return found
        match = _FID.match(line)
        if match:
            self.pending_fight = match[1]
            if self.context_fight != match[1]:
                self._lifecycle("loading", match[1])
            return found
        if (
            line.startswith("Log: [NET_ROOM] Network lifecycle |")
            and "Action=Entered" in line
            and ("RoomType=GameRoom" in line or "RoomType=WaitingRoom" in line)
        ):
            match = _FIGHT_FIELD.search(line)
            fight_id = match[1] if match else None
            phase = "game_room" if "RoomType=GameRoom" in line else "waiting"
            self.pending_fight = fight_id if phase == "game_room" else None
            self._lifecycle(phase, fight_id)
            return found

        if line.startswith("Log: [NET_ROOM] Network traffic history |") and "Cause=MatchEnded" in line:
            match = _FIGHT_FIELD.search(line)
            fight_id = match[1] if match else self.context_fight
            # Ignore a delayed end notification belonging to another match.
            if self.context_fight is None or fight_id == self.context_fight:
                self.pending_fight = None
                self._lifecycle("ended", fight_id)
            return found
        if line.startswith("Log: Start loading battle") or line.startswith("Log: Start reconnect to battle"):
            fight_id = self.context_fight if self.phase in {"waiting", "loading"} or "reconnect" in line else None
            self.pending_fight = None
            self._lifecycle("loading", fight_id)
            return found
        if (line.startswith("Log: [NET_ROOM] Network lifecycle |")
                and "Action=Exited" in line and "RoomType=GameRoom" in line):
            match = _FIGHT_FIELD.search(line)
            if match and self.context_fight is not None and match[1] != self.context_fight:
                self._ignored_exit_companion_time = self.timestamp
                return found
            self.pending_fight = None
            self._lifecycle("lobby")
            return found
        if (
            line.startswith("Log: [NET_ROOM] GameRoom exited")
            or line.startswith("Log: Init lobby screen")
            or line.startswith("Log: [LOBBY] Enter to lobby")
        ):
            self.pending_fight = None
            self._lifecycle("lobby")
        return found

    def feed(self, text: str) -> list[Roster]:
        """Consume complete lines, retaining any unfinished UTF-8 text line.

        A roster block ends at its next non-player line, normally a blank line.
        This prevents publishing a ten-row prefix of an invalid eleven-row list.
        """
        combined = self.partial + text
        lines = combined.split("\n")
        self.partial = lines.pop()
        found: list[Roster] = []
        for line in lines:
            found.extend(self._line(line))
        return found

    def feed_events(self, text: str) -> list[dict]:
        self._events = []
        self.feed(text)
        events, self._events = self._events, []
        return events


def parse_latest_roster(text: str, log_path: str = "") -> Roster | None:
    """Return the last complete, explicitly identified 5v5 roster in text.

    A trailing partial block is ignored; it does not erase an earlier complete
    roster. The terminating blank/non-player line must itself be complete.
    """
    found = _RosterParser(log_path).feed(text)
    return found[-1] if found else None


def _latest_log(log_dir: Path) -> Path | None:
    files = list(log_dir.glob("Gamelog__*.log"))
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime_ns, p.name))


def _read_parser(path: Path) -> _RosterParser:
    parser = _RosterParser(str(path))
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with path.open("rb") as handle:
        while data := handle.read(_READ_LIMIT):
            parser.feed_events(decoder.decode(data))
    return parser


def recent_roster(log_dir: str, *, allow_older: bool = False) -> Roster | None:
    """Read the newest available completed roster for an explicit manual test.

    By default only the newest file is eligible. A newer waiting/loading room
    suppresses an earlier match in that file. An ended match in the newest file
    remains available for an explicitly labelled historical test. Cross-file
    fallback is opt-in, never automatic.
    """
    directory = Path(log_dir)
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))
    files = sorted(
        directory.glob("Gamelog__*.log"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    for path in files if allow_older else files[:1]:
        parser = _read_parser(path)
        pending = parser.phase in {"waiting", "loading", "game_room"} and parser.current_roster is None
        if parser.latest_roster is not None and (allow_older or not pending):
            return parser.latest_roster
    return None


class LogWatcher:
    """Incrementally poll the newest game log; no sleeps and no file writes.

    ``poll_events`` exposes ordered lifecycle/roster events. On startup, only
    the latest file's final state and still-active complete roster are restored.
    The legacy ``poll`` API continues to skip existing bytes by default and
    suppress reconnect duplicates. No game log is ever written.
    """

    def __init__(self, log_dir: str, include_existing: bool = False) -> None:
        self.log_dir = Path(log_dir)
        if not self.log_dir.is_dir():
            raise NotADirectoryError(str(self.log_dir))
        self._path: Path | None = None
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._anchor = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._parser = _RosterParser()
        self._seen: set[tuple] = set()
        self._startup_pending: list[dict] = []
        self._context_invalidated = False
        path = _latest_log(self.log_dir)
        if path is not None:
            self._select(path, skip_existing=not include_existing)

    def _select(self, path: Path, skip_existing: bool = False) -> None:
        self._path = path
        self._offset = 0
        self._anchor = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._parser = _RosterParser(str(path))
        with path.open("rb") as handle:
            stat = path.stat()
            self._identity = (stat.st_dev, stat.st_ino)
            if skip_existing:
                # Seed context and an unfinished text/UTF-8 line. Starting
                # between GameRoom and Player list must not lose its fight ID.
                while data := handle.read(_READ_LIMIT):
                    for event in self._parser.feed_events(self._decoder.decode(data)):
                        if event["type"] == "roster":
                            self._seen.add(_key(event["roster"]))
                self._offset = handle.tell()
                handle.seek(max(0, self._offset - 64))
                self._anchor = handle.read(self._offset - handle.tell())
                self._startup_pending = self._parser.startup_events()

    def poll(self) -> list[Roster]:
        """Compatibility API: only new, non-reconnect roster notifications."""
        return [event["roster"] for event in self.poll_events()
                if event["type"] == "roster" and not event.get("startup") and not event.get("reconnected")]

    def poll_events(self) -> list[dict]:
        """Return ordered final-startup or newly written lifecycle/roster events.

        Lifecycle events contain only phase, fight ID, timestamp and log path;
        unrelated log contents (including network fields) are never retained.
        """
        output, self._startup_pending = self._startup_pending, []
        path = _latest_log(self.log_dir)
        if path is None:
            return output
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        reset = path != self._path or identity != self._identity or stat.st_size < self._offset
        if not reset and self._anchor:
            # Also detect truncate-and-regrow between polls, when size alone
            # would miss a rewrite of the file currently being followed.
            with path.open("rb") as handle:
                handle.seek(self._offset - len(self._anchor))
                reset = handle.read(len(self._anchor)) != self._anchor
        if reset:
            # A file created/replaced before the first poll supersedes the
            # constructor's startup snapshot; never surface that older session.
            output = []
            self._select(path)
            output.append(self._parser._event("lifecycle", "session", None))
            self._context_invalidated = True
        with path.open("rb") as handle:
            handle.seek(self._offset)
            data = handle.read(_READ_LIMIT)
            self._offset = handle.tell()
            handle.seek(max(0, self._offset - 64))
            self._anchor = handle.read(self._offset - handle.tell())
        if not data:
            return output
        found = self._parser.feed_events(self._decoder.decode(data))
        for event in found:
            if event["type"] == "lifecycle":
                self._context_invalidated = True
                output.append(event)
                continue
            roster = event["roster"]
            key = _key(roster)
            if key not in self._seen:
                self._seen.add(key)
                output.append(event)
            elif self._context_invalidated:
                # A lifecycle transition has invalidated the display. The
                # event API can restore that same fight on reconnect, while
                # legacy poll() retains its historical de-duplication contract.
                event["reconnected"] = True
                output.append(event)
            self._context_invalidated = False
        return output
