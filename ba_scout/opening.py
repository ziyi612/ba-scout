"""Opening deployment frequencies from complete observed match timelines.

The window is [0, window_seconds) relative to BATrace StartTime. A match must
cover the entire window. A complete timeline with no deployments in that
window still enters the denominator. Same-second instances have no known
within-second ordering.
"""

from __future__ import annotations

from collections import Counter
import math
import re
from statistics import median
from typing import Any, Iterable


def _finite(value: Any, *, positive: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _match_id(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) else None


def _timeline(envelope: dict[str, Any], player_id: int) -> tuple[list[dict[str, Any]] | None, str | None]:
    version = envelope.get("recon_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 2:
        return None, "旧缓存未保存部署时间"
    players = envelope.get("players")
    player = players.get(str(player_id)) if isinstance(players, dict) else None
    if not isinstance(player, dict):
        return None, "对局未包含该玩家"
    if player.get("available") is False:
        return None, "该玩家的对局记录不可用"
    if type(player.get("player_id")) is not int or player["player_id"] != player_id:
        return None, "玩家身份与对局记录不一致"
    timing = player.get("deployment_timing")
    if not isinstance(timing, dict):
        return None, "未取得部署时间"
    if timing.get("available") is not True or timing.get("complete") is not True:
        reason = timing.get("reason")
        return None, reason if isinstance(reason, str) and reason.strip() else "部署时间记录不完整"
    start, end = timing.get("start_time"), timing.get("end_time")
    if not _finite(start, positive=True) or not _finite(end, positive=True) or end < start:
        return None, "对局开始或结束时间无效"
    if envelope.get("match_start_time") != start or envelope.get("match_end_time") != end:
        return None, "部署时间与对局时间不一致"
    for key in ("missing_spawn_times", "before_start", "after_end", "missing_unit_ids"):
        if type(timing.get(key)) is not int or timing[key] != 0:
            return None, "部署记录含缺失或越界数据"
    spawns = timing.get("spawns")
    total_spawns = player.get("total_spawns")
    known_empty = type(total_spawns) is int and total_spawns == 0
    if not isinstance(spawns, list) or (not spawns and not known_empty):
        return None, "未取得非空单位部署记录"
    for item in spawns:
        if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
            return None, "部分单位ID缺失或无效"
        if not _finite(item.get("offset_s")) or item["offset_s"] > end - start:
            return None, "部分部署时间缺失或无效"
    return spawns, None


def aggregate_openings(
    player_id: int,
    match_envelopes: Iterable[dict[str, Any]],
    requested_match_ids: Iterable[str | int] | None = None,
    window_seconds: int | float = 120,
) -> dict[str, Any]:
    """Aggregate one player's deployment window across unique valid matches.

    requested_match_ids, when supplied, defines the requested sample set; absent
    envelopes are reported as unusable. Duplicate IDs count once, preferring a
    usable duplicate over an incomplete one. mean_count divides total spawns by
    ALL usable matches. first_offset_median is the median of the first observed
    offset in each match containing that unit, not of every unit instance.
    """
    if type(player_id) is not int or player_id <= 0:
        raise ValueError("player_id must be a positive integer")
    if not _finite(window_seconds, positive=True):
        raise ValueError("window_seconds must be a positive finite number")

    requested = None
    if requested_match_ids is not None:
        requested = []
        if isinstance(requested_match_ids, (str, bytes)):
            raise ValueError("requested_match_ids must be an iterable of match IDs")
        for value in requested_match_ids:
            normalized = _match_id(value)
            if normalized is None:
                raise ValueError("requested_match_ids contains an invalid match ID")
            if normalized not in requested:
                requested.append(normalized)

    selected: dict[str, tuple[dict[str, Any], list[dict[str, Any]] | None, str | None]] = {}
    for envelope in match_envelopes:
        if not isinstance(envelope, dict):
            continue
        match_id = _match_id(envelope.get("match_id"))
        if match_id is None or (requested is not None and match_id not in requested):
            continue
        spawns, reason = _timeline(envelope, player_id)
        if spawns is not None and envelope["match_end_time"] - envelope["match_start_time"] < window_seconds:
            spawns, reason = None, "对局提前结束，未覆盖完整开局观察窗口"
        previous = selected.get(match_id)
        if previous is None or (previous[1] is None and spawns is not None):
            selected[match_id] = (envelope, spawns, reason)

    match_ids = requested if requested is not None else list(selected)
    result: dict[str, Any] = {
        "sample_count": 0,
        "requested_count": len(match_ids),
        "window_seconds": window_seconds,
        "units": [],
        "matches": [],
        "unusable_matches": [],
    }
    combined: dict[int, dict[str, Any]] = {}
    for match_id in match_ids:
        value = selected.get(match_id)
        if value is None:
            result["unusable_matches"].append({"match_id": match_id, "reason": "未取得对局数据"})
            continue
        envelope, spawns, reason = value
        if spawns is None:
            result["unusable_matches"].append({"match_id": match_id, "reason": reason})
            continue
        result["sample_count"] += 1
        result["matches"].append({
            "match_id": match_id,
            "source_url": envelope.get("source_url"),
            "match_end_time": envelope.get("match_end_time"),
        })
        counts: Counter[int] = Counter()
        first: dict[int, int | float] = {}
        for spawn in spawns:
            if not 0 <= spawn["offset_s"] < window_seconds:
                continue
            unit_id = spawn["id"]
            counts[unit_id] += 1
            first[unit_id] = min(first.get(unit_id, spawn["offset_s"]), spawn["offset_s"])
            if unit_id not in combined:
                name = spawn.get("name")
                unit_type = spawn.get("type")
                combined[unit_id] = {
                    "id": unit_id,
                    "name": name if isinstance(name, str) and name.strip() else None,
                    "type": unit_type if type(unit_type) is int else None,
                    "matches_with_unit": 0,
                    "total_spawns": 0,
                    "match_ids": [],
                    "_first_offsets": [],
                }
        for unit_id, count in counts.items():
            unit = combined[unit_id]
            unit["matches_with_unit"] += 1
            unit["total_spawns"] += count
            unit["match_ids"].append(match_id)
            unit["_first_offsets"].append(first[unit_id])
    for unit in combined.values():
        unit["mean_count"] = unit["total_spawns"] / result["sample_count"]
        unit["first_offset_median"] = median(unit.pop("_first_offsets"))
    result["units"] = sorted(combined.values(), key=lambda unit: (
        -unit["matches_with_unit"], -unit["total_spawns"], unit["first_offset_median"], unit["id"],
    ))
    return result
