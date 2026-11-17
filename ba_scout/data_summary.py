"""Pure descriptive summaries of observed opening and whole-match data.

No tactical advisor is imported or invoked.  Categories have a fixed display
order and use a union of match IDs within each player.  Player denominators
are never pooled, and model names are never shortened.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Any


_CATEGORIES = (
    ("infantry", "步兵"), ("assault_infantry", "突击步兵"), ("atgm_infantry", "反坦导弹步兵"),
    ("sniper", "狙击步兵"), ("special_forces", "特种步兵"), ("recon", "侦察角色"),
    ("tank", "坦克角色"), ("ifv", "步兵战车"), ("apc", "装甲输送车"),
    ("lsv_vehicle", "LSV 目录角色"), ("air_defense", "防空角色"),
    ("transport_rotor", "运输旋翼"), ("attack_rotor", "攻击旋翼"),
    ("multirole_rotor", "多用途旋翼（任务未知）"), ("recon_rotor", "侦察旋翼"),
    ("multirole_plane", "多用途固定翼（挂载未知）"), ("attack_plane", "攻击固定翼"),
    ("bomber", "轰炸机角色"), ("strategic_bomber", "战略轰炸机角色"),
    ("transport_plane", "运输固定翼"), ("drone", "无人机（任务未知）"),
    ("artillery", "炮兵角色"), ("mortar", "迫击炮"), ("mlrs", "多管火箭炮"),
    ("supply", "补给能力关联（实装未知）"), ("cargo_truck", "货运载体（装载未知）"),
    ("hovercraft", "气垫船"), ("unclassified", "角色分类未取得"),
)
_ROLE_KEYS = {key for key, _ in _CATEGORIES if key != "unclassified"}
_OVERVIEW = {"infantry", "tank", "air_defense", "transport_rotor", "attack_rotor", "multirole_plane", "artillery", "unclassified"}
_STATUS = {"ok": "已取得", "cached": "有效缓存", "stale": "旧缓存", "pending": "等待查询", "partial": "部分结果", "error": "查询失败", "timeout": "截止未完成"}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _seq(value: Any) -> list | tuple | set:
    return value if isinstance(value, (list, tuple, set)) else ()


def _id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            number = int(value)
            return number if number > 0 else None
        except ValueError:
            pass
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _text(value: Any, fallback: str = "未取得") -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _copy(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _state(states: Any, pid: int) -> dict:
    values = _dict(states)
    return _dict(values.get(pid, values.get(str(pid))))


def _fmt(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "未取得"
    return f"{number:g}"


def _time(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return "未取得"
    return _text(value)


def _unit_label(unit: dict) -> str:
    return f"{unit['name']} [单位ID {unit.get('id') or '未取得'}]"


def _recent(event: dict, pid: int) -> dict:
    data = _dict(event.get("data"))
    invalid = event.get("type") not in (None, "recon") or (event.get("player_id") is not None and _id(event.get("player_id")) != pid)
    invalid = invalid or (data.get("player_id") is not None and _id(data.get("player_id")) != pid)
    result = {
        "status": _text(event.get("status"), "pending"), "message": _text(event.get("message"), ""),
        "match_id": str(_id(event.get("match_id"))) if _id(event.get("match_id")) else None,
        "source_url": _text(event.get("source_url"), ""), "match_end_time": _copy(event.get("match_end_time"), None),
        "fetched_at": _text(event.get("fetched_at"), ""), "from_cache": event.get("from_cache") is True,
        "available": bool(data) and not invalid and data.get("available") is not False,
        "all_units": [], "list_complete": False, "classification_complete": data.get("classification_complete") is True,
    }
    if invalid:
        result.update(status="error", message="整场记录的类型或玩家身份不一致，未采用。")
    if not result["available"]:
        return result
    for key in ("total_spawns", "fixed_wing", "rotorcraft", "infantry", "vehicles", "unknown_types"):
        result[key] = _number(data.get(key))
    full = data.get("all_units")
    result["list_complete"] = isinstance(full, list)
    values = full if isinstance(full, list) else data.get("top_units")
    for raw in _seq(values):
        item = _dict(raw)
        uid = _id(item.get("id"))
        result["all_units"].append({"id": uid, "name": _text(item.get("name"), f"单位 #{uid or '未取得'}"), "count": _number(item.get("count"))})
    return result


def build_data_summary(roster, states, opening_states, recon_states, enemy_ids, *, catalog=None, window_seconds=120, is_replay=False, stale_history_ids=()) -> dict:
    """Return only descriptive data, using explicitly confirmed enemy IDs.

    Opening frequency requires coherent ``matches`` and per-unit ``match_ids``.
    Missing classification is labelled as incomplete, rather than counted as
    absence.  ``states`` contributes only the visible profile query status.
    """
    catalog = _dict(catalog)
    catalog_units = _dict(catalog.get("units")) if catalog.get("schema") == 1 else {}
    raw_roster = roster if isinstance(roster, dict) else vars(roster) if roster is not None and hasattr(roster, "__dict__") else {}
    roster_players = {_id(p.get("id")): p for p in _seq(raw_roster.get("players")) if isinstance(p, dict) and _id(p.get("id")) is not None}
    confirmed = {_id(pid) for pid in _seq(enemy_ids)} & set(roster_players)
    confirmed.discard(None)
    stale = {_id(pid) for pid in _seq(stale_history_ids)}
    notes = [
        "类别按固定显示顺序排列。",
        "类别局数按同一玩家的比赛ID取并集；同场出现多个同类型号只计1局。不同玩家各用各的有效样本分母。",
        "同一型号可以具有多个目录角色，类别之间不能相加作为总局数。",
        "单位明细按出现局数降序；并列按总出场次数、完整名称、单位ID排列。这是数值排序。",
        "未列出的类别只表示当前有效样本中没有已归类记录；未取得的数据和未知分类不计为没有出场。",
        "开局窗口包含增援；出场时间不等于抵达时间，同秒记录不证明购买先后。",
        "历史实例的挂载、载员和实际任务未知；多用途固定翼不自动等同空优或反辐射配置。",
        "这些是对手名单的历史记录，未确认谁与你同路，未配对历史地图与出生侧。",
    ]
    if raw_roster.get('source') in {'screen_names', 'manual_names'}:
        notes[0:0] = [str(note) for note in raw_roster.get('identity_notes', [])]
    if is_replay:
        notes.append("当前为历史名单回放；资料可能包含该场及之后记录，不能视为当时战前资料。")
    if not confirmed:
        notes.append("敌我身份或名单未确认，未猜测哪些玩家是对手。")
    elif len({roster_players[pid].get("team") for pid in confirmed}) != 1:
        confirmed.clear()
        notes.append("显式对手名单跨越多个阵营，未合并为对手数据。")
    if not catalog_units:
        notes.append("单位角色目录未取得；单位全名和逐局计数仍保留，角色归类留空。")
    players, category_matches = [], {}
    for pid in sorted(confirmed):
        person = roster_players[pid]
        event = _state(opening_states, pid)
        data = _dict(event.get("data"))
        issues = []
        wrong_event = event.get("type") not in (None, "opening") or (event.get("player_id") is not None and _id(event.get("player_id")) != pid)
        if wrong_event:
            data = {}
            issues.append("开局事件类型或玩家身份不一致，未采用。")
        matches = {}
        for raw in _seq(data.get("matches")):
            item = _dict(raw)
            mid = _id(item.get("match_id"))
            if mid:
                matches[str(mid)] = {"match_id": str(mid), "source_url": _text(item.get("source_url"), ""), "match_end_time": _copy(item.get("match_end_time"), None)}
        count = data.get("sample_count")
        count = count if type(count) is int and count >= 0 else 0
        actual_window = _number(data.get("window_seconds", window_seconds))
        coherent = count == len(matches) and actual_window is not None and actual_window > 0
        if not coherent:
            issues.append("有效样本数、来源或时间窗不一致，未计算频率。")
        usable_count = count if coherent else None
        requested = data.get("requested_count")
        requested = requested if type(requested) is int and requested >= count else None
        raw_units = data.get("units")
        classification_complete = isinstance(raw_units, list)
        units, unit_ids_seen = [], set()
        player_roles = {}
        for raw in _seq(raw_units):
            item = _dict(raw)
            uid = _id(item.get("id"))
            if uid in unit_ids_seen and uid is not None:
                issues.append(f"单位 {uid} 在聚合明细中重复，保留首次记录，未累加。")
                continue
            unit_ids_seen.add(uid)
            mids = {_id(mid) for mid in _seq(item.get("match_ids"))}
            valid_ids = {str(mid) for mid in mids if mid is not None}
            frequency_valid = coherent and count > 0 and bool(mids) and None not in mids and valid_ids <= set(matches) and type(item.get("matches_with_unit")) is int and item["matches_with_unit"] == len(valid_ids)
            entry = _dict(catalog_units.get(str(uid)))
            entry = entry if _id(entry.get("id")) == uid and uid is not None else {}
            raw_roles = {role for role in _seq(entry.get("roles")) if isinstance(role, str)}
            roles = raw_roles & _ROLE_KEYS
            if not roles or not frequency_valid:
                classification_complete = False
            unit = {"id": uid, "name": _text(item.get("name"), _text(entry.get("name"), f"单位 #{uid or '未取得'}")),
                    "matches_with_unit": len(valid_ids) if frequency_valid else None, "sample_count": usable_count,
                    "total_spawns": _number(item.get("total_spawns")), "first_offset_median": _number(item.get("first_offset_median")),
                    "match_ids": sorted(valid_ids, key=int), "frequency_valid": frequency_valid, "roles": [key for key, _ in _CATEGORIES if key in roles]}
            units.append(unit)
            if not frequency_valid:
                issues.append(f"单位 {uid or '未取得'} 缺少一致的逐局归属；保留名称与原始次数，频率显示未取得。")
                continue
            for role in roles or {"unclassified"}:
                player_roles.setdefault(role, set()).update(valid_ids)
        units.sort(key=lambda item: (-(item["matches_with_unit"] if item["matches_with_unit"] is not None else -1), -(item["total_spawns"] if item["total_spawns"] is not None else -1), item["name"].casefold(), item["id"] or 0))
        profile = _state(states, pid)
        player = {"player_id": pid, "name": _text(person.get("name"), f"玩家 {pid}"), "team": _text(person.get("team")),
                  "status": "error" if wrong_event else _text(event.get("status"), "pending"), "message": _text(event.get("message"), ""),
                  "finished": event.get("finished") is True, "profile_status": _text(profile.get("status"), "pending"),
                  "stale_history": pid in stale, "sample_count": usable_count, "requested_count": requested,
                  "window_seconds": actual_window, "classification_complete": classification_complete,
                  "opening_units_available": isinstance(raw_units, list),
                  "units": units, "matches": list(matches.values()), "unusable_matches": _copy(data.get("unusable_matches"), []),
                  "issues": list(dict.fromkeys(issues)), "recent": _recent(_state(recon_states, pid), pid)}
        players.append(player)
        category_matches[pid] = player_roles
    categories = []
    for key, label in _CATEGORIES:
        if not any(category_matches[p["player_id"]].get(key) for p in players):
            continue
        rows = []
        for player in players:
            mids = category_matches[player["player_id"]].get(key, set())
            denominator = player["sample_count"]
            rows.append({"player_id": player["player_id"], "name": player["name"],
                         "matches_with_category": len(mids) if denominator else None,
                         "sample_count": denominator, "known_only": not player["classification_complete"],
                         "match_ids": sorted(mids, key=int), "stale_history": player["stale_history"],
                         "window_seconds": player["window_seconds"]})
        categories.append({"key": key, "label": label, "players": rows})
    if any(p["stale_history"] for p in players):
        notes.append("标为旧清单的玩家使用了先前取得的最近比赛编号；实际最新比赛可能已经变化。")
    windows = {p["window_seconds"] for p in players if p["sample_count"]}
    if len(windows) > 1:
        notes.append("各玩家观察窗口不同，已逐人标明，未合并频率。")
    source = _dict(catalog.get("source"))
    for field in (source.get("build_binding"), _dict(source.get("enum_source")).get("scope")):
        if isinstance(field, str) and field.strip():
            notes.append("目录核验范围：" + field)
    return {"schema": 1, "title": "开局与整场数据汇总", "context": {"fight_id": _text(str(raw_roster.get("fight_id")), "未取得") if raw_roster.get("fight_id") is not None else "未取得", "window_seconds": _number(window_seconds), "is_replay": bool(is_replay), "source": raw_roster.get('source', 'game_log'), "confirmed_enemy_ids": sorted(confirmed)},
            "categories": categories, "players": players, "notes": list(dict.fromkeys(notes)), "catalog_source": _copy(source, {})}


def _fraction(row: dict, key: str) -> str:
    count, total = row.get(key), row.get("sample_count")
    if count is None or not total:
        return "有效样本未取得"
    prefix = "至少已知 " if row.get("known_only") else ""
    return f"{prefix}{count}/{total}局"


def _opening_unit_line(unit: dict) -> str:
    frequency = _fraction(unit, "matches_with_unit") if unit.get("frequency_valid") else "逐局频率未取得"
    return f"{_unit_label(unit)}：{frequency}；总出场 {_fmt(unit.get('total_spawns'))} 次；各出现局首次出场中位 {_fmt(unit.get('first_offset_median'))} 秒"


def summary_sections(summary: dict) -> list[tuple[str, str]]:
    """Fixed-category overview plus each player's first three opening models."""
    context = _dict(summary.get("context"))
    mode = "历史名单回放" if context.get("is_replay") else "准备姓名名单（待游戏ID校核）" if context.get('source') in {'screen_names', 'manual_names'} else "当前名单"
    sections = [("范围", f"{mode} · 对局 {context.get('fight_id', '未取得')}；开局与整场分开，逐人使用有效样本分母。")]
    players = list(_seq(summary.get("players")))
    markers = {player["player_id"]: "①②③④⑤⑥⑦⑧⑨⑩"[index] if index < 10 else f"[{index + 1}]" for index, player in enumerate(players)}
    if players:
        sections.append(("玩家索引（完整名称）", "\n".join(markers[p["player_id"]] + p["name"] + ("（旧清单）" if p.get("stale_history") else "") for p in players)))
    categories = [item for item in _seq(summary.get("categories")) if item.get("key") in _OVERVIEW]
    if categories:
        lines = []
        for category in categories:
            lines.append(category["label"] + "：" + "  ".join(markers.get(row["player_id"], row["name"]) + " " + _fraction(row, "matches_with_category") for row in category["players"]))
        sections.append(("固定类别概览（部分分类，完整见详情）", "\n".join(lines)))
    else:
        sections.append(("类别记录", "当前没有已取得的类别出场记录；未取得数据不表示没有出场。"))
    for player in players:
        units = player["units"]
        shown = min(3, len(units))
        extent = f"摘要前{shown}/共{len(units)}种" if player.get("opening_units_available") else "单位明细未取得"
        title = f"{markers[player['player_id']]}{player['name']} · 开局前{_fmt(player.get('window_seconds'))}秒 · {extent}"
        lines = [f"有效样本 {_fmt(player.get('sample_count'))}/{_fmt(player.get('requested_count'))} 局" + ("；旧清单" if player.get("stale_history") else "")]
        if units:
            lines.extend(_opening_unit_line(unit) for unit in units[:3])
            lines.append("按出现局数作数值排序；完整名单见详情/TXT。")
        elif not player.get("opening_units_available"):
            lines.append("开局单位明细未取得。")
        elif player.get("sample_count"):
            lines.append("这些有效样本的观察窗口内无单位出场记录。")
        else:
            lines.append(player.get("message") or "尚未取得有效开局样本。")
        recent = player["recent"]
        if recent["available"]:
            if recent["list_complete"]:
                lines.append(f"另有最近1场整场记录：{len(recent['all_units'])}种型号；与以上开局记录分开。")
            elif recent["all_units"]:
                lines.append(f"最近1场整场记录仅取得部分摘要：{len(recent['all_units'])}种型号；完整列表未取得。")
            else:
                lines.append("最近1场整场单位明细未取得。")
        sections.append((title, "\n".join(lines)))
    return sections


def summary_text(summary: dict, detailed: bool = True) -> str:
    """Render facts only; detailed output retains every model and source."""
    if not detailed:
        return "\n\n".join(title + "\n" + body for title, body in summary_sections(summary)) + "\n"
    context = _dict(summary.get("context"))
    mode = '历史名单回放' if context.get('is_replay') else '准备姓名名单（待游戏ID校核）' if context.get('source') in {'screen_names', 'manual_names'} else '当前名单'
    lines = [_text(summary.get("title")), f"名单对局：{context.get('fight_id', '未取得')}；模式：{mode}", "", "类别出场记录（固定类别顺序，各玩家独立分母）"]
    if not summary.get("categories"):
        lines.append("尚无已取得的类别记录。")
    for category in _seq(summary.get("categories")):
        lines.append(category["label"])
        for row in category["players"]:
            lines.append(f"  {row['name']} [ID {row['player_id']}]：{_fraction(row, 'matches_with_category')}；窗口 {_fmt(row.get('window_seconds'))} 秒" + ("；旧清单" if row.get("stale_history") else ""))
            if row["match_ids"]:
                lines.append("    出现的对局ID：" + "、".join(row["match_ids"]))
    for player in _seq(summary.get("players")):
        lines += ["", f"【{player['name']}】ID {player['player_id']} · {player['team']}",
                  f"开局记录：0 ≤ 出场时间 < {_fmt(player.get('window_seconds'))} 秒；有效样本 {_fmt(player.get('sample_count'))}/{_fmt(player.get('requested_count'))} 局",
                  "状态：" + _STATUS.get(player["status"], player["status"]) + ("；使用旧比赛清单" if player.get("stale_history") else "")]
        if player.get("message"):
            lines.append("查询说明：" + player["message"])
        lines.append(f"开局全部单位：共 {len(player['units'])} 种；按出现局数作数值排序。" if player.get("opening_units_available") else "开局单位明细未取得。")
        lines.extend("  " + _opening_unit_line(unit) for unit in player["units"])
        if not player["units"] and player.get("opening_units_available"):
            lines.append("  有效样本窗口内没有单位出场记录。" if player.get("sample_count") else "  尚未取得有效开局样本。")
        for unit in player["units"]:
            if unit["match_ids"]:
                lines.append(f"  {_unit_label(unit)} 出现的对局：" + "、".join(unit["match_ids"]))
        lines.append("开局样本来源：")
        for match in player["matches"]:
            lines.append(f"  对局 {match['match_id']}；结束 {_time(match.get('match_end_time'))}；{match.get('source_url') or '来源链接未取得'}")
        for match in _seq(player.get("unusable_matches")):
            if isinstance(match, dict):
                lines.append(f"  未计入样本：{match.get('match_id', '未取得')}；{_text(match.get('reason'))}")
        lines.extend("记录说明：" + issue for issue in player["issues"])
        recent = player["recent"]
        lines += ["", "最近1场整场记录（与开局分开，不计入上述开局频率）"]
        if not recent["available"]:
            lines.append("  " + (recent.get("message") or "整场记录未取得。"))
        else:
            lines += [f"  对局 {recent.get('match_id') or '未取得'}；结束 {_time(recent.get('match_end_time'))}；{'来自缓存' if recent.get('from_cache') else _STATUS.get(recent.get('status'), '未取得')}",
                      f"  总出场 {_fmt(recent.get('total_spawns'))} 次；固定翼 {_fmt(recent.get('fixed_wing'))} / 旋翼 {_fmt(recent.get('rotorcraft'))} / 步兵 {_fmt(recent.get('infantry'))} / 车辆 {_fmt(recent.get('vehicles'))} / 未知类别 {_fmt(recent.get('unknown_types'))}",
                      f"  来源：{recent.get('source_url') or '未取得'}；抓取：{_time(recent.get('fetched_at'))}"]
            if not recent["list_complete"]:
                lines.append("  完整型号列表未取得，以下仅为现有部分摘要。")
            for unit in recent["all_units"]:
                lines.append(f"  {_unit_label(unit)} × {_fmt(unit.get('count'))}")
    lines += ["", "统计口径与数据范围"] + list(_seq(summary.get("notes")))
    source = _dict(summary.get("catalog_source"))
    if source:
        lines += ["角色目录来源：" + _text(source.get("db_path")), "目录记录的本机构建：" + _text(str(source.get("build_id")), "未取得")]
    return "\n".join(lines) + "\n"
