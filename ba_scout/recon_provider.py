"""Read observed unit instances from one public BATrace match page.

Counts describe one recorded match, not concurrent strength or long-term habits.
Returned units remain counted. Corroborated transfers count for the original deployer.
No spending, threat, loadout-role or drone classification is inferred.
"""

from __future__ import annotations

import codecs
from collections import Counter
from datetime import datetime, timezone
import math
import re
import time
from typing import Any
from urllib.request import Request, urlopen

from .provider import USER_AGENT, _rsc_records, _walk


BASE_URL = "https://dash.batrace.top"
MAX_HTML_BYTES = 8 * 1024 * 1024
_CATEGORIES = {16: "fixed_wing", 8: "rotorcraft", 2: "infantry", 4: "vehicles"}


class MatchDataError(ValueError):
    """A match response is incomplete, malformed or cannot be identified."""


def _validate_id(match_id: str) -> None:
    if not isinstance(match_id, str) or re.fullmatch(r"[1-9][0-9]*", match_id) is None:
        raise ValueError("match_id must be a positive integer string")


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _timestamp(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value > 0 else None
    except OverflowError:
        return None


def _deployments_by_player(players: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve transfers only when both records agree on the same instance."""
    deployments: dict[str, dict[str, Any]] = {}
    holders: dict[str, list[str]] = {}
    transfers: dict[str, list[tuple[str, Any]]] = {}
    malformed_units = set()
    for player_key, player in players.items():
        if not isinstance(player, dict):
            continue
        units = player.get("UnitData")
        if isinstance(units, dict) and units:
            deployments[player_key] = dict(units)
            for instance_key in units:
                holders.setdefault(instance_key, []).append(player_key)
        elif units is not None and not isinstance(units, dict):
            malformed_units.add(player_key)
        outgoing = player.get("TransferredUnits")
        if isinstance(outgoing, dict):
            for instance_key, transfer in outgoing.items():
                transfers.setdefault(instance_key, []).append((player_key, transfer))

    for instance_key, claims in transfers.items():
        owners = holders.get(instance_key, [])
        if len(claims) != 1 or len(owners) != 1:
            continue
        sender, transfer = claims[0]
        holder = owners[0]
        instance = deployments[holder][instance_key]
        if sender == holder or sender in malformed_units or not isinstance(transfer, dict) or not isinstance(instance, dict):
            continue
        unit = _positive_int(transfer.get("Id"))
        spawned = _timestamp(transfer.get("SpawnTime"))
        if unit is None or spawned is None:
            continue
        if unit == _positive_int(instance.get("Id")) and spawned == _timestamp(instance.get("SpawnTime")):
            deployments.setdefault(sender, {})[instance_key] = deployments[holder].pop(instance_key)

    return deployments


def _deployment_timing(
    instances: dict[str, Any] | None,
    details: dict[int, dict[str, Any]],
    start_time: int | float | None,
    end_time: int | float | None,
) -> dict[str, Any]:
    """Keep actual instances; ordering ties by ID is only for stable display.

    Offsets are relative to the website's StartTime, not an inferred deployment
    phase. Out-of-match timestamps are retained but conservatively disqualify
    the match from opening-frequency denominators.
    """
    result = {
        "available": False,
        "complete": False,
        "start_time": start_time,
        "end_time": end_time,
        "missing_spawn_times": 0,
        "before_start": 0,
        "after_end": 0,
        "missing_unit_ids": 0,
        "spawns": [],
    }
    if instances is None:
        result["reason"] = "未取得非空单位部署记录"
        return result

    bounds_valid = start_time is not None and end_time is not None and end_time >= start_time
    valid_instances = 0
    for instance in instances.values():
        item = instance if isinstance(instance, dict) else {}
        unit_id = _positive_int(item.get("Id"))
        spawned = _timestamp(item.get("SpawnTime"))
        if unit_id is None:
            result["missing_unit_ids"] += 1
        if spawned is None:
            result["missing_spawn_times"] += 1
        offset = spawned - start_time if spawned is not None and start_time is not None else None
        if spawned is not None and start_time is not None and spawned < start_time:
            result["before_start"] += 1
        if spawned is not None and end_time is not None and spawned > end_time:
            result["after_end"] += 1
        if bounds_valid and unit_id is not None and spawned is not None and start_time <= spawned <= end_time:
            valid_instances += 1
        detail = details.get(unit_id, {})
        # Even malformed instances remain represented, with unknown fields null.
        result["spawns"].append({
            "id": unit_id,
            "name": detail.get("name"),
            "type": detail.get("type"),
            "offset_s": offset,
        })
    result["spawns"].sort(key=lambda item: (
        item["offset_s"] is None,
        item["offset_s"] if item["offset_s"] is not None else 0,
        item["id"] is None,
        item["id"] if item["id"] is not None else 0,
    ))
    # An empty mapping here means all observed units were deployed by others.
    result["available"] = valid_instances > 0 or (bounds_valid and not instances)
    result["complete"] = bounds_valid and valid_instances == len(instances)
    reasons = []
    if not bounds_valid:
        reasons.append("对局开始或结束时间缺失或无效")
    if result["missing_spawn_times"]:
        reasons.append("部分部署时间缺失或无效")
    if result["missing_unit_ids"]:
        reasons.append("部分单位ID缺失或无效")
    if result["before_start"] or result["after_end"]:
        reasons.append("存在对局时间范围外的部署记录")
    if reasons:
        result["reason"] = "；".join(reasons)
    return result


def _summarize_player(
    player_id: int,
    instances: dict[str, Any] | None,
    registry: dict[str, Any],
    unit_names: dict[str, str],
    start_time: int | float | None = None,
    end_time: int | float | None = None,
) -> dict[str, Any]:
    empty = {
        "player_id": player_id,
        "available": False,
        "total_spawns": None,
        **{name: None for name in _CATEGORIES.values()},
        "unknown_types": None,
        "classification_complete": False,
        "known_counts": {},
        "top_units": [],
        "all_units": [],
        "air_units": [],
    }
    if instances is None:
        empty["deployment_timing"] = _deployment_timing(instances, {}, start_time, end_time)
        return empty

    counts = {name: 0 for name in _CATEGORIES.values()}
    unknown = 0
    missing_classification = False
    unit_counts: Counter[int] = Counter()
    details: dict[int, dict[str, Any]] = {}
    # Every assigned instance counts once, including refunded units.
    for instance in instances.values():
        unit_id = _positive_int(instance.get("Id")) if isinstance(instance, dict) else None
        item = registry.get(str(unit_id)) if unit_id is not None else None
        item = item if isinstance(item, dict) else {}
        declared_id = item.get("id")
        if declared_id is not None and declared_id != unit_id:
            raise MatchDataError("Unit registry identity disagrees with its key")
        unit_type = item.get("type")
        if isinstance(unit_type, bool) or not isinstance(unit_type, int):
            unit_type = None
            missing_classification = True
        category = _CATEGORIES.get(unit_type)
        if category:
            counts[category] += 1
        else:
            unknown += 1
        if unit_id is not None:
            unit_counts[unit_id] += 1
            details[unit_id] = {
                "id": unit_id,
                "name": _label(item.get("hudName"))
                or _label(unit_names.get(str(unit_id)))
                or _label(item.get("name")),
                "type": unit_type,
            }

    units = [
        {**details[unit_id], "count": count}
        for unit_id, count in sorted(unit_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    return {
        "player_id": player_id,
        "available": True,
        "total_spawns": len(instances),
        # A missing registry/type could conceal additional aircraft, etc.
        # Expose known_counts separately instead of falsely presenting zeros.
        **{name: None if missing_classification else count for name, count in counts.items()},
        "unknown_types": unknown,
        "classification_complete": not missing_classification,
        "known_counts": counts,
        "top_units": units[:5],
        "all_units": units,
        "air_units": [unit for unit in units if unit["type"] in {16, 8}],
        "deployment_timing": _deployment_timing(instances, details, start_time, end_time),
    }


def parse_match_html(html: str, expected_id: str) -> dict[str, Any]:
    """Decode one identified match and summarize its recorded unit instances.

    Missing/empty UnitData without corroborated transfers gives unavailable
    counts. A missing registry/type gives null totals plus the known subset.
    An explicit unsupported type is counted under unknown_types. top_units is
    capped at five; all_units keeps every observed unit, while air_units keeps
    every observed known aviation unit type.
    """
    _validate_id(expected_id)
    if not isinstance(html, str):
        raise TypeError("html must be text")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise MatchDataError("BATrace match page exceeds the 8 MiB size limit")
    candidates = []
    unit_names: dict[str, str] = {}
    for record in _rsc_records(html):
        for obj in _walk(record):
            if "matchId" in obj and "initialMatchData" in obj:
                candidates.append(obj)
            names = obj.get("unitNames")
            if isinstance(names, dict):
                unit_names.update({str(key): value for key, value in names.items() if isinstance(value, str)})
    matching = [
        obj for obj in candidates
        if not isinstance(obj.get("matchId"), bool) and str(obj.get("matchId")) == expected_id
    ]
    if not matching:
        if candidates:
            raise MatchDataError("BATrace returned a different match ID")
        raise MatchDataError("BATrace match data was not found in the page")
    props = matching[0]
    initial = props.get("initialMatchData")
    info = initial.get("matchInfo") if isinstance(initial, dict) else None
    if not isinstance(info, dict) or not isinstance(info.get("Data"), dict):
        raise MatchDataError("BATrace match player data is missing")
    registry = props.get("initialUnitRegistry")
    registry = registry if isinstance(registry, dict) else {}
    start_time = _timestamp(info.get("StartTime"))
    end_time = _timestamp(info.get("EndTime"))
    deployments = _deployments_by_player(info["Data"])
    players = {}
    for player_key, player in info["Data"].items():
        if not isinstance(player_key, str) or re.fullmatch(r"[1-9][0-9]*", player_key) is None:
            raise MatchDataError("BATrace match contains an invalid player key")
        player_id = int(player_key)
        if isinstance(player, dict) and "Id" in player and _positive_int(player["Id"]) != player_id:
            raise MatchDataError("BATrace match player identity disagrees with its key")
        players[player_key] = _summarize_player(player_id, deployments.get(player_key), registry, unit_names, start_time, end_time)
    return {
        "recon_version": 2,
        "match_id": expected_id,
        "source_url": f"{BASE_URL}/match/{expected_id}",
        "fetched_at": None,
        "match_start_time": start_time,
        "match_end_time": end_time,
        "players": players,
    }


def fetch_match(match_id: str, timeout: float = 8.0) -> dict[str, Any]:
    """Make one public request, returning once complete match props arrive.

    There are no retries or follow-up requests. HTTP errors propagate unchanged.
    timeout controls individual network operations; the caller must impose its
    overall presentation deadline independently.
    """
    _validate_id(match_id)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    started = time.monotonic()
    request = Request(
        f"{BASE_URL}/match/{match_id}",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Encoding": "identity"},
    )
    result = None
    with urlopen(request, timeout=float(timeout)) as response:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_HTML_BYTES:
            raise MatchDataError("BATrace match page exceeds the 8 MiB size limit")
        decoder = codecs.getincrementaldecoder(response.headers.get_content_charset() or "utf-8")(errors="strict")
        read = getattr(response, "read1", response.read)
        parts: list[str] = []
        byte_count = 0
        script_tail = ""
        while True:
            chunk = read(min(65536, MAX_HTML_BYTES - byte_count + 1))
            if not chunk:
                parts.append(decoder.decode(b"", final=True))
                break
            byte_count += len(chunk)
            if byte_count > MAX_HTML_BYTES:
                raise MatchDataError("BATrace match page exceeds the 8 MiB size limit")
            decoded = decoder.decode(chunk)
            parts.append(decoded)
            script_ended = "</script>" in script_tail + decoded
            script_tail = (script_tail + decoded)[-8:]
            if script_ended:
                html = "".join(parts)
                if "initialMatchData" in html and "initialUnitRegistry" in html:
                    try:
                        result = parse_match_html(html, match_id)
                    except MatchDataError:
                        # The RSC model may itself span several script pushes.
                        continue
                    break
        if result is None:
            result = parse_match_html("".join(parts), match_id)
    result["fetched_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result["fetch_seconds"] = round(time.monotonic() - started, 3)
    return result
