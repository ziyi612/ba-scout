"""Derived display metrics from explicitly available public profile fields.

All combat metrics use the provider's ranked (Rt) counters. Nation experience
is displayed as an amount, never as a country play rate or win probability.
"""

from __future__ import annotations

import math
from typing import Any


def _nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _ratio(numerator: Any, denominator: Any, *, percentage: bool = False) -> float | None:
    top = _nonnegative(numerator)
    bottom = _nonnegative(denominator)
    if top is None or bottom is None or bottom <= 0:
        return None
    if percentage and top > bottom:
        return None
    result = top / bottom
    if percentage:
        result *= 100.0
    return result if math.isfinite(result) else None


def _nation_line(value: Any) -> str:
    parts = []
    if isinstance(value, dict):
        # Verified against BATrace countryNames: 1 Russia, 2 United States.
        for nation_id, label in (("1", "俄"), ("2", "美")):
            experience = _nonnegative(value.get(nation_id))
            if experience is None:
                continue
            formatted = f"{experience:,.0f}" if experience.is_integer() else f"{experience:,}"
            parts.append(f"{label} {formatted}")
    return "国家经验：" + (" · ".join(parts) if parts else "未取得")


def profile_metrics(data: dict) -> dict:
    """Return safe ratios and an experience label; missing inputs stay unknown.

    Win and leave percentages use all ranked games, including games left early.
    A zero-loss denominator cannot establish a finite exchange ratio or K/D.
    Legacy profile caches without the additional counters remain compatible.
    """
    duration = _nonnegative(data.get("ranked_duration_seconds"))
    games = _nonnegative(data.get("ranked_games"))
    average = None
    if duration is not None and games is not None and games > 0:
        candidate = (duration / 60.0) / games
        if math.isfinite(candidate):
            average = candidate
    return {
        "win_rate": _ratio(data.get("wins"), data.get("ranked_games"), percentage=True),
        "leave_rate": _ratio(data.get("leaves"), data.get("ranked_games"), percentage=True),
        "value_exchange": _ratio(data.get("ranked_killed_value"), data.get("ranked_lost_value")),
        "unit_kd": _ratio(data.get("ranked_kills"), data.get("ranked_losses_units")),
        "average_minutes": average,
        "nation_experience_line": _nation_line(data.get("nation_experience")),
    }


def tied_top_count(specs: Any) -> int:
    """Count positive finite experience entries tied for the largest value.

    This reports a tie only. Experience does not establish play frequency, and
    equal totals do not establish an ordering among specialization preferences.
    """
    if not isinstance(specs, (list, tuple)):
        return 0
    values = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        value = _nonnegative(spec.get("experience"))
        if value is not None and value > 0:
            values.append(value)
    return values.count(max(values)) if values else 0
