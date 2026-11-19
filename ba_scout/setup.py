"""First-run configuration for the portable application.

Only user confirmation supplies an identity.  Steam discovery reads registry
locations and their library manifests; it never searches entire drives.
"""
from __future__ import annotations

from pathlib import Path
import re
import copy
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .dialog_style import FONT, configure_dialog
from .roster import recent_roster
from .settings import ROOT, DEFAULTS, atomic_json, read_path


def configuration_ready(config: dict) -> bool:
    try:
        validate_configuration(config)
        return True
    except (OSError, TypeError, ValueError):
        return False


def validate_configuration(config: dict) -> dict:
    result = copy.deepcopy(DEFAULTS)
    result.update(copy.deepcopy({key: value for key, value in config.items() if key in DEFAULTS}))
    raw_id = result.get("self_id")
    text = str(raw_id).strip()
    if isinstance(raw_id, bool) or not text.isascii() or not text.isdecimal() or int(text) <= 0:
        raise ValueError("请手填正整数玩家 ID，或从最近名单明确选择自己。")
    directory = read_path(result.get("log_dir", ""))
    if not directory.is_dir():
        raise ValueError("游戏日志目录不存在，请点击“浏览…”重新选择。")
    if directory.name.lower() != "gamelogs" and not next(directory.glob("Gamelog__*.log"), None):
        raise ValueError("请选择游戏的 GameLogs 文件夹，而不是软件目录或 Steam 根目录。")
    result.update(self_id=int(text), log_dir=str(directory))
    return result


def load_player_choices(log_dir: str) -> list[dict]:
    """Return candidates without choosing a player or changing configuration."""
    directory = read_path(log_dir)
    if not directory.is_dir():
        raise ValueError("请先选择存在的 GameLogs 目录。")
    roster = recent_roster(str(directory))
    if roster is None:
        return []
    return [{"id": player["id"], "name": player["name"], "team": player["team"]}
            for player in roster.players]


def discover_log_dirs() -> list[str]:
    """Read only a few registered Steam libraries and this game's manifest."""
    try:
        import winreg
    except ImportError:
        return []
    roots = set()
    locations = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath", winreg.KEY_WOW64_64KEY),
    ]
    for hive, key_name, value_name, view in locations:
        try:
            with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                value = winreg.QueryValueEx(key, value_name)[0]
            if isinstance(value, str) and value.strip():
                roots.add(read_path(value))
        except (OSError, TypeError, ValueError):
            continue
    libraries = set(roots)
    for root in roots:
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text("utf-8", errors="replace")
            for value in re.findall(r'"path"\s*"([^"\r\n]+)"', text):
                libraries.add(read_path(value.replace("\\\\", "\\")))
        except (OSError, ValueError):
            continue
    found = set()
    for library in libraries:
        try:
            text = (library / "steamapps" / "appmanifest_1604270.acf").read_text("utf-8", errors="replace")
            match = re.search(r'"installdir"\s*"([^"\r\n]+)"', text)
            if not match:
                continue
            common = (library / "steamapps" / "common").resolve()
            directory = (common / match.group(1) / "GameLogs").resolve()
            if directory.is_relative_to(common) and directory.is_dir():
                found.add(str(directory))
        except (OSError, ValueError):
            continue
    return sorted(found, key=str.casefold)


def _configuration_dialog(config: dict) -> dict | None:
    root = tk.Tk()
    root.title("断箭数据查看器 · 便携版首次配置")
    root.resizable(False, False)
    configure_dialog(root)
    result = None
    candidates = discover_log_dirs()
    initial_dir = config.get("log_dir") or (candidates[0] if len(candidates) == 1 else "")
    directory = tk.StringVar(value=initial_dir)
    identity = tk.StringVar(value=str(config["self_id"]) if type(config.get("self_id")) is int and config["self_id"] > 0 else "")
    selected = tk.StringVar(value="")
    status = tk.StringVar(value="请确认日志目录，再手填 ID 或读取名单并选择你自己。名单不会自动选择。")
    choices = {}
    frame = ttk.Frame(root, style="BA.TFrame", padding=(22, 18))
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, style="Title.BA.TLabel", text="第一次使用：选择游戏日志目录与本人身份").grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))
    ttk.Label(frame, text="GameLogs 目录", style="BA.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12))
    path_box = ttk.Combobox(frame, textvariable=directory, values=candidates, width=46, font=FONT, style="BA.TCombobox")
    path_box.grid(row=1, column=1, sticky="ew")

    def browse():
        chosen = filedialog.askdirectory(parent=root, title="选择断箭的 GameLogs 目录", mustexist=True, initialdir=directory.get() or str(ROOT))
        if chosen:
            directory.set(chosen)
            selected.set("")
            roster_box.configure(values=[])
            choices.clear()

    ttk.Button(frame, text="浏览…", command=browse, style="BA.TButton").grid(row=1, column=2, padx=(10, 0), sticky="ew")
    ttk.Label(frame, text="自己的数字玩家 ID", style="BA.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=12)
    ttk.Entry(frame, textvariable=identity, width=22, font=FONT, style="BA.TEntry").grid(row=2, column=1, sticky="w")
    ttk.Label(frame, text="最近名单（请明确选自己）", style="BA.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 12))
    roster_box = ttk.Combobox(frame, textvariable=selected, state="readonly", width=46, font=FONT, style="BA.TCombobox")
    roster_box.grid(row=3, column=1, sticky="ew")

    def read_roster():
        try:
            records = load_player_choices(directory.get())
            choices.clear()
            choices.update({f"{item['name']}  ·  ID {item['id']}  ·  {item['team']}": item["id"] for item in records})
            roster_box.configure(values=list(choices))
            selected.set("")
            status.set("已读取最近名单，请从下拉列表明确选择你自己；也可手填 ID。" if records else "尚未找到完整名单。可先进入一场游戏准备界面后再读取，或手填已知的本人 ID。")
        except (OSError, ValueError) as exc:
            status.set(str(exc))

    def select_identity(_event=None):
        player_id = choices.get(selected.get())
        if player_id is not None:
            identity.set(str(player_id))
            status.set("已按你选择的名单条目填写 ID。请核对后点击“保存并启动”。")

    roster_box.bind("<<ComboboxSelected>>", select_identity)
    ttk.Button(frame, text="读取最近名单", command=read_roster, style="BA.TButton").grid(row=3, column=2, padx=(10, 0), sticky="ew")
    ttk.Label(frame, textvariable=status, wraplength=720, style="BA.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(16, 6))
    ttk.Label(frame, text="软件读取游戏日志与准备界面姓名；配置、缓存、截图与 TXT 均保存在软件文件夹内。", wraplength=720,
              style="Muted.BA.TLabel").grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 16))

    def save():
        nonlocal result
        try:
            candidate = validate_configuration({**config, "self_id": identity.get(), "log_dir": directory.get()})
            atomic_json(ROOT / "config.json", candidate)
        except (OSError, TypeError, ValueError) as exc:
            messagebox.showerror("配置未保存", str(exc), parent=root)
            return
        result = candidate
        root.destroy()

    buttons = ttk.Frame(frame, style="BA.TFrame")
    buttons.grid(row=6, column=0, columnspan=3, sticky="e")
    ttk.Button(buttons, text="退出", command=root.destroy, style="BA.TButton").pack(side="left", padx=6)
    ttk.Button(buttons, text="保存并启动", command=save, style="Primary.BA.TButton").pack(side="left")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result


def ensure_configured(config: dict) -> dict | None:
    """Return confirmed configuration, or None after first-run cancellation."""
    if configuration_ready(config):
        return copy.deepcopy(config)
    return _configuration_dialog(copy.deepcopy(config))
