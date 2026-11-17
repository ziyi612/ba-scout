"""Display-only specialisation names; never rewrite provider/cache records.

IDs and full names are the verified BATrace mapping supplied for this build.
Short names are the user's exact requested wording.  Experience values play
no part in naming, nationality, or any tactical conclusion.
"""

from __future__ import annotations

import re
from typing import Any


# Integer specialisation ID -> exact user-requested display name.
SPEC_NAMES: dict[int, str] = {
    3: "马润",
    4: "装甲",
    5: "空降",
    6: "骑兵",
    13: "特种",
    16: "波波",
    7: "vdv",
    8: "近坦",
    9: "摩托化",
    10: "海步",
    12: "国",
    15: "机械化",
}

SPEC_FULL_NAMES: dict[int, str] = {
    3: "海军陆战队远征队",
    4: "装甲旅级战斗队",
    5: "空降师",
    6: "斯特瑞克旅",
    13: "美国特种作战司令部",
    16: "波罗的海营",
    7: "近卫空中突击旅",
    8: "近卫坦克师",
    9: "摩托化步兵旅",
    10: "海军步兵师",
    12: "国民近卫军",
    15: "机械化步兵旅",
}

# Full source name -> display alias.  These are deliberately exact mappings,
# not fuzzy matches; similarly named future specialisations stay unknown.
SPEC_ALIASES: dict[str, str] = {
    full_name: SPEC_NAMES[spec_id]
    for spec_id, full_name in SPEC_FULL_NAMES.items()
}

SPEC_COUNTRIES: dict[int, str] = {
    3: "美国",
    4: "美国",
    5: "美国",
    6: "美国",
    13: "美国",
    16: "美国",
    7: "俄罗斯",
    8: "俄罗斯",
    9: "俄罗斯",
    10: "俄罗斯",
    12: "俄罗斯",
    15: "俄罗斯",
}

# Singular spelling is also supplied for callers displaying one spec.
SPEC_COUNTRY = SPEC_COUNTRIES


def _parse_key(key: Any) -> tuple[int, int] | None:
    if not isinstance(key, str):
        return None
    normalized = key.strip()
    if not re.fullmatch(r"[1-9][0-9]*_[1-9][0-9]*", normalized):
        return None
    left, right = (int(part) for part in normalized.split("_"))
    if left == right:
        return None
    return left, right


def short_combo(spec: dict[str, Any] | None) -> str:
    """Return exact short names for two known IDs, in source-key order.

    If either ID is unknown or the key is malformed, preserve the supplied
    nonempty ``label`` verbatim.  A missing label falls back to the source key
    marked as a combination ID, or an explicit unknown placeholder.  The input
    dictionary and its label are never changed.
    """
    if not isinstance(spec, dict):
        return "未知专精"
    key = spec.get("key")
    pair = _parse_key(key)
    if pair and all(spec_id in SPEC_NAMES for spec_id in pair):
        return "+".join(SPEC_NAMES[spec_id] for spec_id in pair)
    label = spec.get("label")
    if isinstance(label, str) and label.strip():
        return label
    if isinstance(key, str) and key.strip():
        return f"组合 {key.strip()}"
    return "未知专精"


def combo_country(key: Any) -> str | None:
    """Return a country only for two distinct known IDs of that country."""
    pair = _parse_key(key)
    if pair is None:
        return None
    left_country = SPEC_COUNTRIES.get(pair[0])
    right_country = SPEC_COUNTRIES.get(pair[1])
    if left_country is not None and left_country == right_country:
        return left_country
    return None
