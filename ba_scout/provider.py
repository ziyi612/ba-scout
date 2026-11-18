"""Read public BATrace player pages without executing their JavaScript.

``specs`` contains lifetime specialization *experience*, never match counts or
usage percentages.  A missing team or winner leaves a recent outcome unknown.
This module deliberately makes no extra match-detail requests or HTTP retries.
"""

from __future__ import annotations

from . import __version__

import codecs
import json
import math
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import IncompleteRead
from typing import Any, Callable, Iterator
from urllib.request import Request, urlopen


BASE_URL = "https://dash.batrace.top"
MAX_HTML_BYTES = 4 * 1024 * 1024
USER_AGENT = f"BA-Scout/{__version__} (local public player statistics viewer)"


class PlayerDataError(ValueError):
    """The response cannot be safely identified as the requested player."""


class _PlayerDataNotReady(PlayerDataError):
    """No complete player JSON model has arrived yet."""


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.lower() == "script":
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def _validate_id(player_id: int) -> None:
    if isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0:
        raise ValueError("player_id must be a positive integer")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _integer(value: Any) -> int | None:
    value = _number(value)
    return value if isinstance(value, int) else None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _recent_match_ids(value: Any, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item > 0:
            candidate = str(item)
        elif isinstance(item, str) and re.fullmatch(r"[0-9]+", item):
            candidate = item.lstrip("0")
        else:
            continue
        if candidate and candidate not in result:
            result.append(candidate)
            if len(result) >= limit:
                break
    return result


def _first_match_id(value: Any) -> str | None:
    ids = _recent_match_ids(value, 1)
    return ids[0] if ids else None


def profile_history(profile: dict[str, Any]) -> list[str | int] | None:
    """Distinguish a verified empty history from missing data in old/new profiles."""
    values = profile.get("recent_match_ids")
    ids = _recent_match_ids(values)
    if ids and len(values) == len(ids) and all(str(value) == mid for value, mid in zip(values, ids)):
        return list(values)
    ids = ids or _recent_match_ids([profile.get("last_match_id")])
    if ids or profile.get("recent_history_available") is True:
        return ids
    return None


def _nation_experience(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for nation_id, experience in value.items():
        numeric = _number(experience)
        if isinstance(nation_id, str) and re.fullmatch(r"[0-9]+", nation_id) and numeric is not None and numeric >= 0:
            result[nation_id] = numeric
    return result


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    # Iteration avoids recursion limits on a deeply nested page component tree.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            yield item
            pending.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            pending.extend(reversed(item))


def _rsc_models(html: str) -> dict[str, Any]:
    collector = _ScriptCollector()
    collector.feed(html)
    collector.close()
    decoder = json.JSONDecoder()
    fragments: list[str] = []
    for script in collector.scripts:
        for match in re.finditer(r"self\.__next_f\.push\s*\(\s*", script):
            try:
                payload, _ = decoder.raw_decode(script, match.end())
            except (ValueError, RecursionError):
                continue
            if (
                isinstance(payload, list)
                and len(payload) == 2
                and payload[0] == 1
                and isinstance(payload[1], str)
            ):
                fragments.append(payload[1])
    # Next.js may split one JSON model across several push() calls.
    stream = "".join(fragments)
    records: dict[str, Any] = {}
    for match in re.finditer(r"(?m)^([0-9a-fA-F]+):", stream):
        pos = match.end()
        if pos >= len(stream) or stream[pos] not in "[{":
            continue  # I/module references, text chunks and metadata are not models.
        try:
            value, _ = decoder.raw_decode(stream, pos)
        except (ValueError, RecursionError):
            continue
        records[match.group(1).lower()] = value
    return records


def _rsc_records(html: str) -> list[Any]:
    """Keep the existing match-page parser's ordered model interface."""
    return list(_rsc_models(html).values())


def _model_value(value: Any, models: dict[str, Any]) -> Any:
    """Resolve only known structured model fields, never executable RSC types.

    Flight may put a profile/statistics object into a later model and leave
    a JSON reference in its parent. Missing or cyclic references remain
    unavailable. Callers use this for containers, not nickname text.
    """
    seen = set()
    for _ in range(32):
        if not isinstance(value, str):
            return value
        match = re.fullmatch(r"\$(?:@|L)?([0-9a-fA-F]+)((?::[^:]+)*)", value)
        if match is None or value in seen:
            return None
        seen.add(value)
        value = models.get(match.group(1).lower())
        for prop in match.group(2).split(":")[1:]:
            if isinstance(value, dict):
                value = value.get(prop)
            elif isinstance(value, list) and prop.isascii() and prop.isdecimal() and int(prop) < len(value):
                value = value[int(prop)]
            else:
                return None
    return None


_STAT_NUMBERS = ("winCountRt", "lossCountRt", "leaveCountRt", "fightsCountRt",
                 "unitsKilledCountRt", "unitsLostCountRt", "unitsKilledCostRt",
                 "unitsLostCostRt", "matchDurationTimeSecRt")


def ranked_counts_available(wins: Any, games: Any) -> bool:
    wins, games = _integer(wins), _integer(games)
    return wins is not None and games is not None and 0 <= wins <= games


def _updated_at(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("$D")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _specializations(stats: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    experience = stats.get("combinedSpecsExp")
    if not isinstance(experience, dict):
        return []
    result = []
    for key, value in experience.items():
        numeric = _number(value)
        if not isinstance(key, str) or not re.fullmatch(r"\d+_\d+", key):
            continue
        if numeric is None or numeric <= 0:
            continue
        ids = key.split("_")
        label = (
            " + ".join(names[spec_id] for spec_id in ids)
            if all(spec_id in names for spec_id in ids)
            else "专精组合 " + key
        )
        result.append({"key": key, "label": label, "experience": numeric})
    return sorted(result, key=lambda row: (-row["experience"], row["key"]))


def _recent_matches(
    player_id: int, match_ids: Any, match_map: dict[str, Any]
) -> list[dict[str, Any]]:
    ordered = []
    if isinstance(match_ids, list):
        ordered.extend(str(item) for item in match_ids if str(item) in match_map)
    # Include available records even if the separate ID list was absent.
    remaining = [key for key in match_map if key not in ordered]
    remaining.sort(
        key=lambda key: _number(match_map[key].get("EndTime")) or 0
        if isinstance(match_map[key], dict) else 0,
        reverse=True,
    )
    ordered.extend(remaining)
    result = []
    seen = set()
    for match_id in ordered:
        if match_id in seen:
            continue
        seen.add(match_id)
        match = match_map[match_id]
        if not isinstance(match, dict) or not isinstance(match.get("Data"), dict):
            continue
        player = match["Data"].get(str(player_id))
        if not isinstance(player, dict) or player.get("Id") != player_id:
            continue
        winner = _integer(match.get("WinnerTeam"))
        team = _integer(player.get("TeamId"))
        outcome = "unknown"
        if winner is not None and team is not None and winner >= 0 and team >= 0:
            outcome = "win" if winner == team else "loss"
        old = _number(player.get("OldRating"))
        new = _number(player.get("NewRating"))
        delta = round(new - old, 6) if old is not None and new is not None else None
        result.append({"match_id": match_id, "outcome": outcome, "rating_delta": delta})
    return result


def parse_player_html(html: str, expected_id: int) -> dict[str, Any]:
    """Parse a captured BATrace page; reject a mismatching player identity.

    This only decodes JSON inside Next.js RSC script payloads.  JavaScript and
    HTML content are never executed.  No missing statistic is assumed to be zero.
    ``fetched_at`` and ``fetch_seconds`` remain None for offline parsing.
    """
    _validate_id(expected_id)
    if not isinstance(html, str):
        raise TypeError("html must be text")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise PlayerDataError("BATrace page exceeds the 4 MiB size limit")
    candidates: list[dict[str, Any]] = []
    spec_names: dict[str, str] = {}
    match_map: dict[str, Any] = {}
    models = _rsc_models(html)
    for record in models.values():
        for obj in _walk(record):
            raw_candidate = _model_value(obj.get("playerData"), models)
            if isinstance(raw_candidate, dict):
                candidate = dict(raw_candidate)
                for field in ("info", "statInfo", "last_fights_data"):
                    candidate[field] = _model_value(candidate.get(field), models)
                if isinstance(candidate.get("statInfo"), dict):
                    stats = dict(candidate["statInfo"])
                    candidate["_pending_fields"] = set()
                    for field in ("combinedSpecsExp", "nationExp"):
                        raw_value = stats.get(field)
                        stats[field] = _model_value(raw_value, models)
                        if isinstance(raw_value, str) and raw_value.startswith("$") and not isinstance(stats[field], dict):
                            candidate["_pending_fields"].add(field)
                    candidate["statInfo"] = stats
                if isinstance(candidate.get("info"), dict):
                    candidates.append(candidate)
            names = _model_value(obj.get("specNames"), models)
            if isinstance(names, dict):
                spec_names.update({
                    str(k): v for k, v in names.items()
                    if str(k).isdigit() and isinstance(v, str) and v.strip()
                })
            matches = _model_value(obj.get("matchMap"), models)
            if isinstance(matches, dict) and obj.get("stbid") == expected_id:
                match_map.update({str(k): v for k, v in matches.items()})
    matching = [item for item in candidates if _integer(item["info"].get("id")) == expected_id]
    if not matching:
        if candidates:
            raise PlayerDataError("BATrace returned a different player ID")
        raise _PlayerDataNotReady("BATrace player data was not found in the page")
    # Profile models for this ID may arrive in pieces. Only verified numeric
    # values and resolved containers can replace fields in earlier models.
    info, stats, pending = {}, {}, set()
    history = None
    for candidate in matching:
        model = candidate["info"]
        for key, parse in (("name", _string), ("rating", _number), ("rank", _integer)):
            value = parse(model.get(key))
            if value is not None:
                info[key] = value
        source_ids = candidate.get("last_fights_data")
        if isinstance(source_ids, list) and (not source_ids or _recent_match_ids(source_ids)):
            history = source_ids
        model = candidate.get("statInfo")
        if not isinstance(model, dict) or "playerId" not in model:
            continue
        if _integer(model["playerId"]) != expected_id:
            raise PlayerDataError("BATrace profile and statistics player IDs disagree")
        stats["playerId"] = expected_id
        for key in _STAT_NUMBERS:
            value = _number(model.get(key)) if key.endswith(("CostRt", "SecRt")) else _integer(model.get(key))
            if value is not None and value >= 0:
                stats[key] = value
        if _updated_at(model.get("updatedAt")):
            stats["updatedAt"] = model["updatedAt"]
        pending.update(candidate.get("_pending_fields", ()))
        for key, pattern in (("combinedSpecsExp", r"\d+_\d+"), ("nationExp", r"\d+")):
            values = model.get(key)
            if isinstance(values, dict):
                entries = {k: v for k, v in values.items()
                           if isinstance(k, str) and re.fullmatch(pattern, k) and _number(v) is not None and v >= 0}
                if not values or entries:
                    stats.setdefault(key, {}).update(entries)
    return {
        "profile_version": 4,
        "player_id": expected_id,
        "name": _string(info.get("name")),
        "rating": _number(info.get("rating")),
        "rank": _integer(info.get("rank")),
        "wins": _integer(stats.get("winCountRt")),
        "losses": _integer(stats.get("lossCountRt")),
        "leaves": _integer(stats.get("leaveCountRt")),
        "ranked_games": _integer(stats.get("fightsCountRt")),
        "ranked_kills": _integer(stats.get("unitsKilledCountRt")),
        "ranked_losses_units": _integer(stats.get("unitsLostCountRt")),
        "ranked_killed_value": _number(stats.get("unitsKilledCostRt")),
        "ranked_lost_value": _number(stats.get("unitsLostCostRt")),
        "ranked_duration_seconds": _number(stats.get("matchDurationTimeSecRt")),
        "nation_experience": _nation_experience(stats.get("nationExp")),
        "last_match_id": _first_match_id(history),
        "recent_match_ids": _recent_match_ids(history),
        "recent_history_available": history is not None,
        "stats_updated_at": _updated_at(stats.get("updatedAt")),
        "specs": _specializations(stats, spec_names),
        "recent": _recent_matches(expected_id, history, match_map),
        "source_url": f"{BASE_URL}/player/{expected_id}",
        "fetched_at": None,
        "fetch_seconds": None,
        "core_ready": ranked_counts_available(stats.get("winCountRt"), stats.get("fightsCountRt")),
        "specs_available": "combinedSpecsExp" in stats,
        "stats_pending": bool(pending - stats.keys()),
        "spec_names_available": bool(spec_names),
    }


def fetch_player(player_id: int, timeout: float = 12.0, *,
                 on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Fetch exactly one public player page. HTTP errors (including 429) propagate.

    ``timeout`` is urllib's network-operation timeout; the caller must enforce
    its own overall presentation deadline when fetching players concurrently.
    There are no retry, authentication, disk cache or follow-up match requests.
    Publish validated fields as complete models arrive. Return early once the
    win-rate inputs, specializations and recent list have all been read.
    ``recent_partial`` marks the intentionally unread tail;
    absent recent records in this mode do not mean the player has none.
    A validated identity-only model is returned as ``profile_incomplete`` at
    EOF or a truncated read. It is never a successful core-statistics result.
    """
    _validate_id(player_id)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    started = time.monotonic()
    request = Request(
        f"{BASE_URL}/player/{player_id}",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Encoding": "identity"},
    )
    with urlopen(request, timeout=float(timeout)) as response:
        declared_length = response.headers.get("Content-Length")
        if declared_length and declared_length.isdigit() and int(declared_length) > MAX_HTML_BYTES:
            raise PlayerDataError("BATrace page exceeds the 4 MiB size limit")
        charset = response.headers.get_content_charset() or "utf-8"
        decoder = codecs.getincrementaldecoder(charset)(errors="strict")
        html = ""
        byte_count = 0
        script_tail = ""
        partial = False
        result = None
        published = None
        read_warning = None
        while True:
            try:
                block = response.read1(min(16 * 1024, MAX_HTML_BYTES - byte_count + 1))
            except (TimeoutError, IncompleteRead) as exc:
                # Preserve only complete JSON models already received. An
                # incomplete model, wrong ID or invalid UTF-8 remains an error.
                extra = exc.partial if isinstance(exc, IncompleteRead) else b""
                byte_count += len(extra)
                if byte_count > MAX_HTML_BYTES:
                    raise PlayerDataError("BATrace page exceeds the 4 MiB size limit") from exc
                html += decoder.decode(extra)
                try:
                    result = parse_player_html(html, player_id)
                except _PlayerDataNotReady:
                    raise exc
                partial = True
                read_warning = type(exc).__name__
                break
            if not block:
                html += decoder.decode(b"", final=True)
                result = parse_player_html(html, player_id)
                break
            byte_count += len(block)
            if byte_count > MAX_HTML_BYTES:
                raise PlayerDataError("BATrace page exceeds the 4 MiB size limit")
            decoded = decoder.decode(block)
            html += decoded
            # This only avoids reparsing until a script closes. Readiness itself
            # is determined by parsed JSON identities/objects below, never text.
            boundary = script_tail + decoded
            script_tail = boundary[-8:]
            if "</script>" not in boundary.lower():
                continue
            try:
                candidate = parse_player_html(html, player_id)
            except _PlayerDataNotReady:
                continue
            if (candidate["core_ready"] and candidate["specs_available"] and candidate["spec_names_available"]
                    and candidate["recent_history_available"] and not candidate["stats_pending"]):
                result = candidate
                partial = True
                break
            if on_progress is not None and candidate != published:
                published = candidate
                on_progress({**candidate, "profile_incomplete": True,
                             "fetched_at": datetime.now(timezone.utc).isoformat()})
    result["partial_page"] = partial
    result["recent_partial"] = partial
    result["profile_incomplete"] = not (result["core_ready"] and result["specs_available"])
    if read_warning:
        result["read_warning"] = read_warning
    result["fetched_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result["fetch_seconds"] = round(time.monotonic() - started, 3)
    return result
