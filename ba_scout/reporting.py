"""Build a complete plain-text scouting report without files, GUI or network."""

from __future__ import annotations

from . import __version__

from datetime import datetime, timezone
import math
from typing import Any

from .encounter_tracker import encounter_label
from .labels import short_combo
from .metrics import profile_metrics, tied_top_count


_UNKNOWN = "未取得"
_STATUS = {
    "ok": "本次查询成功", "cached": "有效缓存", "stale": "旧缓存，正在刷新",
    "pending": "等待查询", "partial": "部分结果", "error": "查询失败",
    "timeout": "截止未完成",
}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any, missing: str = _UNKNOWN) -> str:
    return value if isinstance(value, str) and value.strip() else missing


def _identifier(value: Any) -> str:
    if type(value) is int and value > 0:
        return str(value)
    return value if isinstance(value, str) and value.isdecimal() and int(value) > 0 else _UNKNOWN


def _number(value: Any, decimals: int | None = None, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _UNKNOWN
    try:
        if not math.isfinite(value) or value < 0:
            return _UNKNOWN
    except OverflowError:
        return _UNKNOWN
    if decimals is not None:
        return f"{value:,.{decimals}f}{suffix}"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}{suffix}"


def _time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return _UNKNOWN
    return _text(value)


def _get(roster: Any, key: str, default: Any = None) -> Any:
    return roster.get(key, default) if isinstance(roster, dict) else getattr(roster, key, default)


def _state(states: Any, player_id: Any) -> dict:
    states = _dict(states)
    return _dict(states.get(player_id, states.get(str(player_id))))


def _unit_name(unit: dict) -> str:
    # Never use the compact model regex or an ellipsis: names are kept intact.
    return _text(unit.get("name"), "单位名称未取得") + f" [单位ID {_identifier(unit.get('id'))}]"


def _core_lines(player_id: Any, state: dict) -> list[str]:
    data = _dict(state.get("data"))
    status = _STATUS.get(state.get("status"), _UNKNOWN)
    if state.get('status') in {'partial', 'loading'}:
        status = "已取得部分字段" if state.get('status') == 'partial' else "已取得部分字段，仍在查询"
        current = _dict(state.get('partial_data')) or data
        if current.get('wins') is None and current.get('ranked_games') is None and not current.get('specs_available'):
            status += "；本次累计战绩与专精统计暂缺"
    if data.get("from_cache") and state.get("status") in {"error", "timeout", "partial"}:
        status += "；以下沿用旧缓存"
    lines = ["核心数据：" + status]
    if state.get("message"):
        lines.append("  查询说明：" + _text(state.get("message")))
    if data.get("player_id") is not None and data["player_id"] != player_id:
        return lines + ["  数据玩家ID不匹配，本段未采用。"]
    fresh = _dict(state.get('partial_data'))
    if data.get('from_cache') and fresh.get('player_id') == player_id:
        lines.extend([
            f"  本次基础资料：评分 {_number(fresh.get('rating'), 1)}；排名 {_number(fresh.get('rank'))}",
            "  本次基础资料抓取时间：" + _time(fresh.get('fetched_at')),
            "  本次基础资料来源：" + _text(fresh.get('source_url')),
        ])
        if fresh.get('wins') is not None or fresh.get('ranked_games') is not None:
            rate = profile_metrics(fresh)['win_rate']
            lines.append(f"  本次排位：胜 {_number(fresh.get('wins'))} / 总场数 {_number(fresh.get('ranked_games'))}；胜率 {_number(rate, 1, '%')}")
        if fresh.get('specs_available'):
            entries = fresh.get('specs') or []
            lines.append("  本次历史双专精：" + ("，".join(f"{short_combo(item)}（{_number(item.get('experience'))}）"
                                                     for item in entries) or "已返回清单，无正经验组合"))
        lines.append("  原缓存明细")
    if not data:
        return lines + ["  战绩、评分与专精数据未取得。"]
    metrics = profile_metrics(data)
    lines.extend([
        f"  评分 {_number(data.get('rating'), 1)}；排名 {_number(data.get('rank'))}",
        f"  排位 {_number(data.get('ranked_games'))} 场：胜 {_number(data.get('wins'))} / 负 {_number(data.get('losses'))} / 退出 {_number(data.get('leaves'))}",
        f"  胜率 {_number(metrics['win_rate'], 1, '%')}；退局率 {_number(metrics['leave_rate'], 1, '%')}（分母为排位总场数，含退局）",
        f"  价值交换比 {_number(metrics['value_exchange'], 4)}（摧毁价值 {_number(data.get('ranked_killed_value'))} / 损失价值 {_number(data.get('ranked_lost_value'))}）",
        f"  单位数量 K/D {_number(metrics['unit_kd'], 4)}（击杀 {_number(data.get('ranked_kills'))} / 损失 {_number(data.get('ranked_losses_units'))}）",
        f"  排位平均时长 {_number(metrics['average_minutes'], 1, ' 分钟')}",
        "  " + metrics["nation_experience_line"],
        "  抓取时间：" + _time(data.get("fetched_at")) + "；网站统计更新：" + _time(data.get("stats_updated_at")),
        "  来源：" + _text(data.get("source_url")),
        "历史双专精组合经验（经验不是近期使用率或下局出场概率）：",
    ])
    specs = data.get("specs")
    specs = [spec for spec in specs if isinstance(spec, dict)] if isinstance(specs, list) else []
    if not specs:
        lines.append("  未取得正经验组合；不能据此认定没有使用过。")
    else:
        if tied_top_count(specs) > 1:
            lines.append("  最高经验存在并列；列表顺序不代表偏好先后。")
        for spec in specs:
            lines.append(f"  {short_combo(spec)}：经验 {_number(spec.get('experience'))}；完整名称 {_text(spec.get('label'))}；组合ID {_text(spec.get('key'))}")
    return lines


def _window_state(state: dict, expected_window: Any) -> dict:
    """Reject a mislabeled window without modifying the supplied snapshot."""
    data = _dict(state.get("data"))
    actual = data.get("window_seconds")
    if data and (isinstance(actual, bool) or actual != expected_window):
        return {**state, "data": {}, "status": "error",
                "message": f"收到的时间窗为 {_number(actual)} 秒，与本栏 {_number(expected_window)} 秒不一致；该记录未采用。"}
    return state


def _opening_lines(state: dict, is_enemy: bool, default_window: Any, *, window_title: str | None = None) -> list[str]:
    data = _dict(state.get("data"))
    count, requested = data.get("sample_count"), data.get("requested_count")
    window = data.get("window_seconds", default_window)
    lines = [f"{window_title or '开局记录'}：网站开始时间后 0 ≤ 部署时间 < {_number(window)} 秒；有效样本 {_number(count)}/{_number(requested)} 局"]
    if state.get("message"):
        lines.append("  查询说明：" + _text(state.get("message")))
    if type(count) is not int or count <= 0:
        lines.append("  未取得有效开局样本。" if state or is_enemy else "  未查询开局记录；自动查询优先覆盖对手。")
    else:
        if count == 1:
            lines.append("  当前取得1场记录。")
        units = data.get("units")
        if not isinstance(units, list):
            lines.append("  单位明细未取得。")
        elif not units:
            lines.append("  完整观察窗口内没有部署记录；这些有效局仍计入分母。")
        else:
            for unit in units:
                if not isinstance(unit, dict):
                    continue
                lines.append(
                    f"  {_unit_name(unit)}：出现 {_number(unit.get('matches_with_unit'))}/{count} 局；"
                    f"总部署 {_number(unit.get('total_spawns'))} 次；"
                    f"各出现局首次出场时间中位数 {_number(unit.get('first_offset_median'), 1, ' 秒')}"
                )
        lines.append("  样本来源：")
        matches = data.get("matches")
        matches = matches if isinstance(matches, list) else []
        if not matches:
            lines.append("    来源明细未取得。")
        for match in matches:
            if isinstance(match, dict):
                lines.append(f"    对局 {_identifier(match.get('match_id'))}；结束 {_time(match.get('match_end_time'))}；{_text(match.get('source_url'))}")
    skipped = data.get("unusable_matches")
    if isinstance(skipped, list) and skipped:
        lines.append("  未计入分母：")
        for match in skipped:
            if isinstance(match, dict):
                source = "；" + match["source_url"] if isinstance(match.get("source_url"), str) else ""
                lines.append(f"    对局 {_identifier(match.get('match_id'))}：{_text(match.get('reason'))}{source}")
    return lines


def _window_overview(summary: Any, opening_states: Any, roster: Any, enemy_ids: Any,
                     window_seconds: Any, title: str) -> list[str]:
    """Describe one window without repeating whole-match or core records."""
    lines = [title, f"观察区间：0 <= t < {_number(window_seconds)} 秒（t 为网站开始时间后的部署时间）。"]
    summary = _dict(summary)
    summary_window = _dict(summary.get("context")).get("window_seconds")
    valid_summary = bool(summary) and not isinstance(summary_window, bool) and summary_window == window_seconds
    if valid_summary:
        from .data_summary import summary_sections
        for heading, body in summary_sections(summary):
            if heading.startswith(("玩家索引", "固定类别概览", "类别记录")):
                lines.extend([heading, body])
    elif summary:
        lines.append(f"类别概览时间窗 {_number(summary_window)} 秒与本栏不一致，未采用。")
    else:
        lines.append("类别概览未取得。")
    lines.append("各玩家样本与缺失（本窗口独立计算）：")
    summary_players = summary.get("players") if valid_summary else None
    if isinstance(summary_players, list):
        rows = [p for p in summary_players if isinstance(p, dict)]
        for player in rows:
            actual = player.get("window_seconds")
            if isinstance(actual, bool) or actual != window_seconds:
                lines.append(f"  {_text(player.get('name'))}：时间窗不一致，样本未采用。")
                continue
            line = f"  {_text(player.get('name'))}：有效 {_number(player.get('sample_count'))}/{_number(player.get('requested_count'))} 局"
            skipped = player.get("unusable_matches")
            if isinstance(skipped, list):
                line += f"；未计入 {len(skipped)} 局"
            if player.get("stale_history"):
                line += "；旧比赛清单"
                if player.get("history_fetched_at"):
                    line += "，清单采集时间 " + _time(player["history_fetched_at"])
            lines.append(line)
            if player.get("message"):
                lines.append("    " + _text(player.get("message")))
    else:
        players = _get(roster, "players", [])
        enemies = set(enemy_ids or ())
        rows = [p for p in players if isinstance(p, dict) and p.get("id") in enemies] if isinstance(players, list) else []
        for player in rows:
            state = _window_state(_state(opening_states, player.get("id")), window_seconds)
            data = _dict(state.get("data"))
            line = f"  {_text(player.get('name'))}：有效 {_number(data.get('sample_count'))}/{_number(data.get('requested_count'))} 局"
            skipped = data.get("unusable_matches")
            if isinstance(skipped, list):
                line += f"；未计入 {len(skipped)} 局"
            lines.append(line)
            if state.get("message"):
                lines.append("    " + _text(state.get("message")))
    if not rows:
        lines.append("  对手名单或本窗口样本概览未取得。")
    return lines


def _recon_lines(player_id: Any, state: dict, is_enemy: bool) -> list[str]:
    data = _dict(state.get("data"))
    lines = ["最近1场整场出场记录："]
    if data.get("player_id") is not None and data["player_id"] != player_id:
        return lines + ["  数据玩家ID不匹配，本段未采用。"]
    if not data or data.get("available") is False:
        return lines + ["  " + _text(state.get("message"), "未取得。" if state or is_enemy else "未查询；自动查询优先覆盖对手。")]
    status = "对局缓存" if state.get("from_cache") else _STATUS.get(state.get("status"), _UNKNOWN)
    lines.extend([
        f"  状态：{status}；对局 {_identifier(state.get('match_id'))}；结束 {_time(state.get('match_end_time'))}",
        "  来源：" + _text(state.get("source_url")) + "；抓取时间：" + _time(state.get("fetched_at")),
        f"  总部署 {_number(data.get('total_spawns'))} 次；固定翼 {_number(data.get('fixed_wing'))} / 旋翼 {_number(data.get('rotorcraft'))} / 步兵 {_number(data.get('infantry'))} / 车辆 {_number(data.get('vehicles'))} / 其他或未知 {_number(data.get('unknown_types'))}",
    ])
    if data.get("classification_complete") is False:
        lines.append("  分类不完整，缺失分类不能记为0；以下保留已知单位。")
    units = data.get("all_units")
    if not isinstance(units, list):
        lines.append("  完整单位列表未取得；以下若有内容，仅是已取得的部分摘要。")
        units = data.get("top_units") if isinstance(data.get("top_units"), list) else []
    if not units:
        lines.append("  未取得具体单位型号记录。")
    for unit in units:
        if isinstance(unit, dict):
            lines.append(f"  {_unit_name(unit)} × {_number(unit.get('count'))}")
    return lines


def build_scout_report(
    roster: Any,
    states: dict,
    opening_states: dict,
    recon_states: dict,
    enemy_ids: Any,
    opening_window_seconds: int | float = 120,
    generated_at: datetime | str | None = None,
    *,
    is_replay: bool | None = None,
    advice: dict | None = None,
    encounters: dict | None = None,
    data_summary: dict | None = None,
    opening20_states: dict | None = None,
    data_summary20: dict | None = None,
) -> str:
    """Return full UTF-8-ready text for the current UI snapshot, without I/O.

    Accept the Roster dataclass or an equivalent dict. State maps may use integer
    or string player keys. Pass is_replay explicitly from UI; omitted mode is
    marked unknown, never guessed from a historic-looking fight ID. No names or
    unit lists are truncated. A fixed generated_at makes the output repeatable.
    Supplying either 20-second argument enables two separately labelled windows;
    the second uses opening_window_seconds (default 120). Omitting both keeps
    legacy single-window output. Advice is accepted only for old snapshots and
    is never rendered.
    """
    generated = generated_at if generated_at is not None else datetime.now(timezone.utc)
    mode = "历史阵容回放（不是当前正在进行的对局）" if is_replay is True else "当前阵容查询" if is_replay is False else "记录模式未标注（当前/历史未核实）"
    name_source = _get(roster, 'source') in {'screen_names', 'manual_names'}
    if name_source:
        mode = '准备画面姓名匹配（待游戏数字 ID 名单校核）' if _get(roster, 'source') == 'screen_names' else '手动姓名名单（身份及当前对局待游戏 ID 校核）'
    lines = [
        "断箭战前查询 · 完整文本报告",
        "生成时间：" + _time(generated),
        "模式：" + mode,
        "历史名单只用于识别玩家；网站资料以各自抓取时间为准，并非名单时刻的网站快照。" if is_replay is True else "网站资料以各自抓取时间为准。",
        f"阵容对局ID：{_identifier(_get(roster, 'fight_id'))}；名单日志时间：{_text(_get(roster, 'log_timestamp'))}",
        "说明：战绩为网站累计资料；开局和整场记录来自已结束的历史对局，不是本场实时出兵。",
        "开局比例=出现局数/有效局数，不是下一局概率；短局、缺时间或失败记录不进分母。同秒不推断购买先后。",
        "整场次数按出场实例统计，含返场，不重复加入接收单位，不代表同时在场数量。国家经验不是国家使用率。",
        "缺失字段显示“未取得”；比值分母为0时也不推算有限比值。所有单位名称原样完整列出。",
    ]
    if name_source:
        lines[3:3] = [str(note) for note in _get(roster, 'identity_notes', [])]
    # Legacy snapshots may contain advice. It is intentionally ignored.
    dual_windows = opening20_states is not None or data_summary20 is not None
    if dual_windows:
        first_title = "前20秒概览"
        second_title = f"前{_number(opening_window_seconds)}秒概览（设置窗口）"
        relation = (
            f"前{_number(opening_window_seconds)}秒包含前20秒，不是20–{_number(opening_window_seconds)}秒区间。"
            if opening_window_seconds > 20 else
            "设置窗口与固定前20秒窗口范围相同。" if opening_window_seconds == 20 else
            "设置窗口短于20秒，是固定前20秒窗口的一部分。"
        )
        overview = [f"断箭数据汇总 {__version__} · 双时间窗", f"阵容对局ID：{_identifier(_get(roster, 'fight_id'))}；模式：{mode}",
                    "生成时间：" + _time(generated),
                    "两窗共享同批对局查询，各自使用有效样本与缺失记录；没有新增一套对局请求。", relation, ""]
        overview.extend(_window_overview(data_summary20, opening20_states, roster, enemy_ids, 20, first_title))
        overview.append("")
        overview.extend(_window_overview(data_summary, opening_states, roster, enemy_ids, opening_window_seconds, second_title))
        lines[0:0] = overview + ["", "=" * 36, "完整资料与原始依据", "=" * 36, ""]
    elif data_summary:
        from .data_summary import summary_text
        brief = summary_text(data_summary, detailed=False).splitlines()
        brief[1:1] = [f"阵容对局ID：{_identifier(_get(roster, 'fight_id'))}；模式：{mode}",
                      "生成时间：" + _time(generated)]
        lines[0:0] = brief + ["", "=" * 36, "完整资料与原始依据", "=" * 36, ""]
    players = _get(roster, "players", [])
    players = [p for p in players if isinstance(p, dict)] if isinstance(players, list) else []
    if not players:
        return "\n".join(lines + ["", "未取得阵容名单，暂无可导出的玩家结果。", ""])
    enemies = set(enemy_ids or ())
    teams = {p.get("team") for p in players}
    enemy_teams = {p.get("team") for p in players if p.get("id") in enemies}
    grouping_known = (len(teams) == 2 or _get(roster, 'self_team') in {'Alpha', 'Bravo'}) and len(enemy_teams) == 1 and all(
        (p.get("id") in enemies) == (p.get("team") in enemy_teams) for p in players
    )
    if grouping_known:
        groups = [("对手", [p for p in players if p.get("id") in enemies]),
                  ("我方", [p for p in players if p.get("id") not in enemies])]
    else:
        groups = [(f"{team or '未知阵营'}（敌我未判定）", [p for p in players if p.get("team") == team])
                  for team in sorted(teams, key=lambda value: str(value))]
    for group, members in groups:
        lines.extend(["", "=" * 36, f"{group} · {len(members)} 人", "=" * 36])
        for player in members:
            player_id = player.get("id")
            lines.extend(["", f"【{_text(player.get('name'))}】ID {_identifier(player_id)} · {_text(player.get('team'))}"])
            hits = _state(encounters, player_id)
            if hits:
                label = encounter_label(hits, detailed=True)
                if label:
                    lines.append("本机历史对局 · " + label)
            lines.extend(_core_lines(player_id, _state(states, player_id)))
            if dual_windows:
                lines.extend(_opening_lines(_window_state(_state(opening20_states, player_id), 20), player_id in enemies,
                                            20, window_title="前20秒全部单位"))
                lines.extend(_opening_lines(_window_state(_state(opening_states, player_id), opening_window_seconds),
                                            player_id in enemies, opening_window_seconds,
                                            window_title=f"前{_number(opening_window_seconds)}秒全部单位（设置窗口）"))
            else:
                lines.extend(_opening_lines(_state(opening_states, player_id), player_id in enemies, opening_window_seconds))
            lines.extend(_recon_lines(player_id, _state(recon_states, player_id), player_id in enemies))
    return "\n".join(lines) + "\n"
