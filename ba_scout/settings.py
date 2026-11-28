from __future__ import annotations

import json
import copy
import os
import sys
import tempfile
from pathlib import Path

ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent)
def default_prep_regions():
    """Read an identity-free, normalized template; each caller owns its copy."""
    try:
        from .prep_roster import extract_prep_roster
        regions = json.loads((ROOT / 'data/prep-layout-default.json').read_text('utf-8-sig'))
        parsed = extract_prep_roster([], regions, image_size=(1920, 1080))
        if not parsed['geometry_valid']:
            return {}
        return copy.deepcopy(regions)
    except (OSError, ValueError, TypeError, KeyError):
        return {}


DEFAULTS = {
    "self_id": 0,
    "log_dir": "",
    "deadline": 20.0,
    "cache_ttl": 600.0,
    "opening_window_seconds": 120,
    "opening_history_limit": 5,
    "concurrency": 5,
    "click_through": True,
    "hotkey_toggle": "CTRL+ALT+B",
    "hotkey_interact": "CTRL+ALT+M",
    "x": None,
    "y": 80,
    "prep_enabled": True,
    "update_check_on_start": True,
    "prep_regions": default_prep_regions(),
}


def read_path(value: str | Path) -> Path:
    """Resolve an explicitly selected absolute read path on any drive."""
    if not str(value).strip():
        raise ValueError("请选择完整目录或文件路径。")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError("需要绝对路径。")
    return raw.resolve()


def app_path(value: str | Path) -> Path:
    """Only permit application writes inside the executable/source folder."""
    path = read_path(value)
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("程序文件只能写入软件所在目录，请选择软件目录内的路径。")
    return path


f_path = app_path  # Compatibility name; no drive letter is hardcoded.


def prepare_runtime_paths() -> Path:
    directory = app_path(ROOT / "tmp")
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(directory)
    os.environ["TMP"] = str(directory)
    tempfile.tempdir = str(directory)
    return directory


def atomic_json(path: Path, data: object) -> None:
    path = f_path(path)
    if path == ROOT.resolve():
        raise ValueError("请指定软件目录中的文件名。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_settings() -> dict:
    result = copy.deepcopy(DEFAULTS)
    path = ROOT / "config.json"
    if path.exists():
        incoming = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(incoming, dict):
            raise ValueError("config.json 必须是 JSON 对象。")
        result.update({k: v for k, v in incoming.items() if k in DEFAULTS})
    try:
        result["self_id"] = max(0, int(result["self_id"])) if not isinstance(result["self_id"], bool) else 0
    except (TypeError, ValueError, OverflowError):
        result["self_id"] = 0
    try:
        result["log_dir"] = str(read_path(result["log_dir"])) if result["log_dir"] else ""
    except (TypeError, ValueError, OSError):
        result["log_dir"] = ""
    # A fixed upper bound preserves the user's deadline even after manual edits.
    result["deadline"] = min(20.0, max(0.1, float(result["deadline"])))
    result["concurrency"] = min(5, max(1, int(result["concurrency"])))
    result["cache_ttl"] = max(0, float(result["cache_ttl"]))
    result["opening_window_seconds"] = min(300, max(10, int(result["opening_window_seconds"])))
    result["opening_history_limit"] = min(5, max(1, int(result["opening_history_limit"])))
    result['prep_enabled'] = bool(result['prep_enabled'])
    if not isinstance(result.get('prep_regions'), dict):
        result['prep_regions'] = {}
    return result
