from __future__ import annotations

from . import __version__

import ctypes
import datetime as dt
import json
import logging
import re
import time
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from .engine import ScanController
from .labels import short_combo, combo_country
from .metrics import profile_metrics, tied_top_count
from .provider import fetch_player, profile_history
from .scouting_engine import BattleRecon
from .recon_provider import fetch_match
from .request_gate import RequestGate
from .reporting import build_scout_report
from .data_summary import build_data_summary, summary_sections, summary_text
from .roster import LogWatcher, Roster, parse_latest_roster, recent_roster
from .settings import ROOT, atomic_json, f_path, read_path
from .windows import Hotkeys, WM_HOTKEY, copy_file_to_clipboard, exercise_hotkey, root_handle, set_click_through, user32

BG = "#111827"
SURFACE = "#1b2638"
FG = "#e8eef8"
MUTED = "#9eafc8"
CYAN = "#63dfcd"
YELLOW = "#ffd285"
RED = "#ff9f9f"


def number(value, decimals=0):
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def local_time(value):
    if not value:
        return "未知"
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).astimezone().strftime("%m-%d %H:%M")
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return str(value)[:19]


def record(data):
    return f"{number(data.get('wins'))}胜 / {number(data.get('losses'))}负 / {number(data.get('leaves'))}退"


def overview_fields(state, player_id):
    """Prefer each current field, keeping ratios within one dated profile."""
    data = state.get("data") or {}
    fresh = state.get("partial_data") or {}
    mixed = bool(data.get("from_cache") and fresh.get("player_id") == player_id)
    metrics = profile_metrics(data)
    fields = {"rating": data.get("rating"), "rank": data.get("rank"), "specs": data.get("specs"),
              "win_rate": metrics["win_rate"], "value_exchange": metrics["value_exchange"]}
    if not mixed:
        return fields, []

    current = profile_metrics(fresh)
    cached = []
    for key, inputs in (("rating", ("rating",)), ("rank", ("rank",)),
                        ("win_rate", ("wins", "ranked_games")),
                        ("value_exchange", ("ranked_killed_value", "ranked_lost_value"))):
        if all(fresh.get(item) is not None for item in inputs):
            fields[key] = current[key] if key in current else fresh[key]
        elif fields[key] is not None:
            cached.append(key)
    if fresh.get("specs_available") or fresh.get("specs"):
        fields["specs"] = fresh.get("specs") or []
    elif fields["specs"]:
        cached.append("specs")
    return fields, cached


def prep_slot_status(row):
    decision = row.get("resolution") or {}
    status = decision.get("status") or row.get("status", "missing")
    if status == "rate_limited" and decision.get("retry_at") is not None:
        delay = max(0, decision["retry_at"] - time.monotonic())
        ready = dt.datetime.now() + dt.timedelta(seconds=delay)
        return "限流至\n" + ready.strftime("%H:%M:%S")
    if status == "missing" and row.get("queryable"):
        return "核对昵称"
    return {"missing": "等名字", "searching": "查身份中", "retrying": "补查中",
            "timeout": "查名超时", "rate_limited": "网站限流", "error": "查名失败",
            "incomplete": "待核对", "conflict": "身份冲突", "ambiguous": "重名待确认",
            "unmatched": "未匹配", "invalid": "昵称待校正"}.get(status, "待确认")


def unit_label(unit, compact=False):
    name = unit.get('name') or f"单位 #{unit.get('id', '?')}"
    if compact:
        model = re.match(r"^[A-Za-z][A-Za-z0-9.-]*[0-9][A-Za-z0-9.-]*\b", name)
        if model:
            return model.group(0)
    return name


class ScoutWindow:
    WIDTH = 748
    HEIGHT = 580

    def __init__(self, config, args):
        self.config = dict(config)
        if args.self_test:
            self.config["self_id"] = 990000
        config = self.config
        self.args = args
        self.root = tk.Tk()
        self.root.title("断箭数据查看器 · 便携版")
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.98)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = self.callback_error
        self.closed = False
        self.expanded = True
        self.click_through = False
        self.hotkeys = Hotkeys()
        self.hotkey_ok = (self.hotkeys.register(101, config["hotkey_toggle"]), self.hotkeys.register(102, config["hotkey_interact"]))
        self.states = {}
        self.recon_states = {}
        self.opening_states = {}
        self.opening20_states = {}
        self.opening_view_seconds = 20
        self.stale_history_ids = set()
        self.analysis_mode = "opening"
        self.view_mode = "data"
        self.summary = {}
        self.summary20 = {}
        self.catalog, self.catalog_note = self.load_tactical_catalog()
        self.opening_window_seconds_active = self.config.get("opening_window_seconds", 120)
        self.recon_scan_id = None
        self.core_elapsed = None
        self.enemy_ids = []
        self.players = []
        self.current_roster = None
        self.scan_id = None
        self.started = None
        self.scanning = False
        self.auto_report_attempted_scan_id = None
        self.auto_report_path = None
        self.last_export_path = None
        self.report_scan_id = None
        self.live_phase = None
        self.live_fight_id = None
        self.prep_controller = None
        self.screen_ocr = None
        self.ocr_warmup_future = None
        self.prep_prefetch_token = None
        self.prep_generation = None
        self.prep_last_status_key = None
        self.prep_rows = []
        self.prep_streaming = False
        self.prep_revision = -1
        self.report_due_at = None
        self.prep_initial_report_written = False
        self.prep_dialog = None
        self.is_replay = False
        self.selected_id = None
        self.last_log_error = None
        self.selftest_results = []
        self.cache_dir = ROOT / ("work/ui-test-cache" if args.self_test else "data/cache")
        self.request_gate = RequestGate(min_interval=0.65)
        self.controller = ScanController(str(self.cache_dir), self.fake_fetch if args.self_test else
                                         (lambda player_id, **options: self.request_gate.run(fetch_player, player_id, **options)),
                                         deadline=max(0.1, config["deadline"] - 1.0), concurrency=config["concurrency"],
                                         ttl=config["cache_ttl"], min_profile_version=4, stream_progress=not args.self_test)
        self.recon = BattleRecon(str(ROOT / ("work/ui-scouting-cache-v06" if args.self_test else "data/scouting-matches")),
                                 self.fake_match if args.self_test else (lambda match_id, timeout: self.request_gate.run(fetch_match, str(match_id), timeout=timeout, background=True)), concurrency=2)
        try:
            self.watcher = LogWatcher(config["log_dir"], include_existing=False)
        except (OSError, ValueError) as exc:
            self.watcher = None
            self.last_log_error = str(exc)
        self._build()
        self._position()
        self.root.update_idletasks()
        self.hwnd = root_handle(self.root)
        self.set_passthrough(config["click_through"] and all(self.hotkey_ok))
        if not all(self.hotkey_ok):
            self.status.set("快捷键被占用，已保持鼠标可操作；可在设置中修改")
        elif self.watcher is None:
            self.status.set("日志目录不可用，请按 Ctrl+Alt+M 后打开设置修改目录。")
        self.root.after(60, self.tick)
        self.root.after(400, self.poll_log)
        if args.replay_log:
            self.root.after(500, lambda: self.replay_path(args.replay_log))
        if args.self_test:
            self.root.after(500, self.self_test_begin)
        else:
            self.root.after_idle(self.warmup_ocr)
        logging.info("Application started version=%s; log_dir=%s", __version__, config["log_dir"])

    def _build(self):
        self.row_font = tkfont.Font(root=self.root, family="Microsoft YaHei UI", size=9)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=FG,
                        font=("Microsoft YaHei UI", 9), rowheight=46, borderwidth=0)
        style.configure("Treeview.Heading", background="#26344b", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9), relief="flat")
        style.map("Treeview", background=[("selected", "#304862")], foreground=[("selected", "white")])
        style.map("Treeview.Heading", background=[("active", "#304862")])
        self.outer = tk.Frame(self.root, bg=BG, highlightbackground="#3c526c", highlightthickness=1)
        self.outer.pack(fill="both", expand=True)
        self.bar = tk.Frame(self.outer, bg="#182338", height=45)
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)
        self.brand = tk.Label(self.bar, text="断箭  /  战前情报", bg="#182338", fg=FG,
                              font=("Microsoft YaHei UI", 11, "bold"), padx=12)
        self.brand.pack(side="left")
        self.brand.bind("<ButtonPress-1>", self.drag_start)
        self.brand.bind("<B1-Motion>", self.drag_move)
        self.compact = tk.StringVar(value="等待新对局")
        self.compact_label = tk.Label(self.bar, textvariable=self.compact, bg="#182338", fg=CYAN,
                                      font=("Microsoft YaHei UI", 9), anchor="w")
        self.compact_label.pack(side="left", fill="x", expand=True)
        self.button(self.bar, "退出", self.close).pack(side="right", padx=(3, 8))
        self.fold_button = self.button(self.bar, "收起", self.toggle)
        self.fold_button.pack(side="right", padx=3)
        self.interact_button = self.button(self.bar, "", self.toggle_interact)
        self.interact_button.pack(side="right", padx=3)
        self.body = tk.Frame(self.outer, bg=BG, padx=12)
        self.body.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="监听已开启，准备阶段自动识别姓名并查询；后出现的对手会继续补齐。")
        tk.Label(self.body, textvariable=self.status, bg=BG, fg=FG, anchor="w",
                 font=("Microsoft YaHei UI", 9), wraplength=715, justify="left").pack(fill="x", pady=(12, 7))
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(fill="x", pady=(0, 8))
        self.team_mode = "enemy"
        self.summary_button = self.button(nav, "数据汇总", lambda: self.show_view("summary"))
        self.summary_button.pack(side="left", padx=(0, 6))
        self.enemy_button = self.button(nav, "对手  ·  5", lambda: self.show_players("enemy"))
        self.enemy_button.pack(side="left", before=self.summary_button, padx=(0, 6))
        self.ally_button = self.button(nav, "我方  ·  5", lambda: self.show_players("ally"))
        self.ally_button.pack(side="left")
        self.analysis_button = self.button(nav, "看整场出场", self.show_other_analysis)
        self.analysis_button.pack(side="left", padx=(10, 0))
        self.button(nav, "设置", self.settings_dialog).pack(side="right")
        self.button(nav, "刷新本场", self.refresh).pack(side="right", padx=6)
        self.button(nav, "测试上局", self.replay_latest).pack(side="right")
        windows = tk.Frame(self.body, bg=BG)
        windows.pack(fill="x", pady=(0, 6))
        self.window20_button = self.button(windows, "前20秒", lambda: self.select_opening_window(20))
        self.window20_button.pack(side="left", padx=(0, 6))
        self.window_primary_button = self.button(windows, self.primary_window_label(),
                                                 lambda: self.select_opening_window(self.opening_window_seconds_active))
        self.window_primary_button.pack(side="left")
        self.prep_button = self.button(windows, '准备识别', self.open_prep_dialog)
        self.prep_button.pack(side='right')
        tk.Label(windows, text="均从开局起算；TXT 含两段", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="left", padx=10)
        self.content = tk.Frame(self.body, bg=BG)
        self.content.pack(fill="both", expand=True)
        self.data_panel = tk.Frame(self.content, bg=BG)
        self.summary_panel = tk.Frame(self.content, bg=BG)
        self.summary_heading = tk.StringVar(value="对手开局数据汇总")
        tk.Label(self.summary_panel, textvariable=self.summary_heading, bg=BG, fg=CYAN,
                 font=("Microsoft YaHei UI", 12, "bold"), anchor="w", justify="left", wraplength=700).pack(fill="x")
        self.summary_reason = tk.StringVar(value="类别按固定顺序列出；单位摘要按出现局数排序，全部名称见明细。")
        tk.Label(self.summary_panel, textvariable=self.summary_reason, bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 9), anchor="w", justify="left", wraplength=700).pack(fill="x", pady=(3, 6))
        self.summary_confidence = tk.StringVar(value="")
        tk.Label(self.summary_panel, textvariable=self.summary_confidence, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 8), anchor="w", justify="left", wraplength=700).pack(fill="x", pady=(0, 5))
        summary_body = tk.Frame(self.summary_panel, bg=BG)
        summary_body.pack(fill="both", expand=True)
        summary_scroll = tk.Scrollbar(summary_body)
        summary_scroll.pack(side="right", fill="y")
        self.summary_text = tk.Text(summary_body, bg=SURFACE, fg=FG, relief="flat", wrap="word", height=10,
                                   padx=10, pady=6, spacing3=2, font=("Microsoft YaHei UI", 9),
                                   yscrollcommand=summary_scroll.set, cursor="arrow")
        self.summary_text.pack(side="left", fill="both", expand=True)
        summary_scroll.config(command=self.summary_text.yview)
        self.summary_text.tag_configure("label", foreground=CYAN, font=("Microsoft YaHei UI", 9, "bold"))
        self.summary_text.config(state="disabled")
        columns = ("name", "rating", "record", "spec", "recent", "status")
        self.table = ttk.Treeview(self.data_panel, columns=columns, show="headings", height=5, selectmode="browse")
        for key, label, width in [("name", "玩家", 122), ("rating", "评分", 48), ("record", "排位数据", 96),
                                  ("spec", "历史专精经验", 175), ("recent", "开局记录 · " + self.opening_window_label(), 194), ("status", "状态", 65)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=width, stretch=(key == "spec"), anchor="w")
        self.table.tag_configure("error", foreground=RED)
        self.table.tag_configure("stale", foreground=YELLOW)
        self.table.tag_configure("pending", foreground=MUTED)
        self.table.pack(fill="x")
        self.table.bind("<<TreeviewSelect>>", self.on_select)
        self.table.bind("<Double-1>", self.player_details)
        self.detail_title = tk.StringVar(value="准备阶段将按游戏中的位置逐人显示对手。")
        self.detail_text = tk.StringVar(value="按 Ctrl+Alt+M 允许鼠标操作；按 Ctrl+Alt+B 展开或收起。\n专精经验反映历史积累，不能当作近期使用率或本场卡组。")
        tk.Label(self.data_panel, textvariable=self.detail_title, bg=BG, fg=CYAN, anchor="w",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(fill="x", pady=(11, 3))
        tk.Label(self.data_panel, textvariable=self.detail_text, bg=BG, fg=MUTED, anchor="nw", justify="left",
                 font=("Microsoft YaHei UI", 9), wraplength=710, height=3).pack(fill="x")
        lower = tk.Frame(self.body, bg=BG)
        lower.pack(side="bottom", fill="x", pady=(8, 9), before=self.content)
        self.button(lower, "复制所选来源", self.copy_source).pack(side="right")
        self.export_button = self.button(lower, "导出完整 TXT", self.export_report)
        self.export_button.pack(side="right", padx=(0, 6))
        self.report_button = self.button(lower, "打开战前报告", self.open_report)
        self.report_button.pack(side="left", padx=(0, 6))
        self.report_button.config(state="disabled")
        self.button(lower, "复制数据汇总", self.copy_summary).pack(side="left")
        self.button(lower, "全部单位 / 来源", self.summary_details).pack(side="left", padx=(6, 0))
        self.footer = tk.Label(self.outer, text="", bg="#182338", fg=MUTED,
                               font=("Microsoft YaHei UI", 8), anchor="w", padx=12, pady=6)
        # Reserve the recovery shortcuts before the expandable body takes space.
        self.footer.pack(side="bottom", fill="x", before=self.body)
        self.select_team("enemy")
        self.show_view("data")
        self.update_summary()

    def load_tactical_catalog(self):
        try:
            catalog = json.loads((ROOT / "data/tactical-catalog.json").read_text("utf-8"))
            if catalog.get("schema") != 1 or not isinstance(catalog.get("units"), dict):
                raise ValueError("单位分类库格式不正确")
            log_dir = read_path(self.config["log_dir"])
            manifest_path = next((parent / "appmanifest_1604270.acf" for parent in (log_dir, *log_dir.parents)
                                  if parent.name.lower() == "steamapps" and (parent / "appmanifest_1604270.acf").is_file()), None)
            if manifest_path is None:
                return None, "未能从所选游戏目录核对版本；仅显示原始单位记录。"
            manifest = manifest_path.read_text("utf-8")
            installed = re.search(r'"buildid"\s+"(\d+)"', manifest)
            if not installed or installed.group(1) != str(catalog.get("source", {}).get("build_id")):
                return None, "游戏版本已变化，单位分类库待更新；仅显示原始单位记录。"
            return catalog, ""
        except (OSError, ValueError, TypeError):
            return None, "单位分类库未取得；仅显示原始单位记录。"

    def show_view(self, mode):
        self.view_mode = mode
        self.data_panel.pack_forget()
        self.summary_panel.pack_forget()
        panel = self.summary_panel if mode == "summary" else self.data_panel
        panel.pack(fill="both", expand=True)
        self.summary_button.config(bg="#27616a" if mode == "summary" else "#2a3a50")

    def show_players(self, team):
        self.show_view("data")
        self.select_team(team)

    def show_other_analysis(self):
        self.show_view("data")
        self.toggle_analysis()

    def update_summary(self):
        self.summary = build_data_summary(
            self.current_roster, self.states, self.opening_states, self.recon_states, self.enemy_ids,
            catalog=self.catalog, window_seconds=self.opening_window_seconds_active,
            is_replay=self.is_replay, stale_history_ids=self.stale_history_ids)
        self.summary20 = build_data_summary(
            self.current_roster, self.states, self.opening20_states, self.recon_states, self.enemy_ids,
            catalog=self.catalog, window_seconds=20,
            is_replay=self.is_replay, stale_history_ids=self.stale_history_ids)
        if self.catalog_note:
            self.summary.setdefault("notes", []).append(self.catalog_note)
            self.summary20.setdefault("notes", []).append(self.catalog_note)
        active = self.summary20 if self.opening_view_seconds == 20 else self.summary
        samples = sum((state.get("data") or {}).get("sample_count", 0) > 0 for state in self.selected_opening_states().values())
        self.summary_heading.set(f"对手开局{self.opening_window_label()} · {samples}/{len(self.enemy_ids)}人有有效样本")
        self.window20_button.config(bg="#27616a" if self.opening_view_seconds == 20 else "#2a3a50")
        self.window_primary_button.config(text=self.primary_window_label(),
                                         bg="#27616a" if self.opening_view_seconds != 20 else "#2a3a50")
        if self.opening_window_seconds_active == 20:
            self.window_primary_button.pack_forget()
        elif not self.window_primary_button.winfo_manager():
            self.window_primary_button.pack(side="left", after=self.window20_button)
        stale_ids = sorted(self.stale_history_ids)
        source_label = '准备姓名名单 · 待ID校核' if getattr(self.current_roster, 'source', '') in {'screen_names', 'manual_names'} else '本场名单'
        self.summary_confidence.set(("历史名单回放" if self.is_replay else source_label) +
                                    f" · 0 ≤ 出场时间 < {self.opening_view_seconds}秒 · 各窗口独立分母" +
                                    (f" · {len(stale_ids)}人沿用旧清单" if stale_ids else ""))
        position = self.summary_text.yview()[0]
        self.summary_text.config(state="normal")
        self.summary_text.delete("1.0", "end")
        for title, body in summary_sections(active):
            if "玩家索引" in title:
                body = body.replace("\n", "  ")
            self.summary_text.insert("end", title + "：", "label")
            self.summary_text.insert("end", body + "\n")
        self.summary_text.config(state="disabled")
        self.summary_text.yview_moveto(position)

    def copy_summary(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.combined_summary_text())
        self.status.set("两段开局数据已复制，包含全部单位名称、各自样本数和来源。")

    def combined_summary_text(self):
        result = "【开局前20秒】\n" + summary_text(self.summary20, detailed=True)
        if self.opening_window_seconds_active != 20:
            result += (f"\n【开局前{self.opening_window_seconds_active}秒】\n"
                       "两个窗口均从开局起算，分别统计，不相加。\n" + summary_text(self.summary, detailed=True))
        return result

    def summary_details(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("完整开局统计与来源")
        dialog.configure(bg=BG)
        dialog.attributes("-topmost", True)
        dialog.geometry("720x600")
        pane = tk.Frame(dialog, bg=BG)
        pane.pack(fill="both", expand=True, padx=12, pady=12)
        scroll = tk.Scrollbar(pane)
        scroll.pack(side="right", fill="y")
        text = tk.Text(pane, bg=SURFACE, fg=FG, wrap="word", font=("Microsoft YaHei UI", 10),
                       padx=12, pady=12, yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        scroll.config(command=text.yview)
        text.insert("1.0", self.combined_summary_text())
        text.config(state="disabled")
        self.button(dialog, "关闭", dialog.destroy).pack(pady=(0, 10))
        return dialog

    @staticmethod
    def button(parent, text, command):
        return tk.Button(parent, text=text, command=command, bg="#2a3a50", fg=FG,
                         activebackground="#3a536d", activeforeground="white", relief="flat", bd=0,
                         font=("Microsoft YaHei UI", 9), padx=9, pady=4, cursor="hand2", takefocus=False)

    def _position(self):
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = self.config.get("x")
        if x is None:
            x = max(0, screen_w - self.WIDTH - 30)
        x = max(0, min(int(x), max(0, screen_w - self.WIDTH)))
        y = max(0, min(int(self.config.get("y", 80)), max(0, screen_h - self.HEIGHT)))
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def drag_start(self, event):
        self.drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag_move(self, event):
        if self.click_through:
            return
        x = max(0, min(event.x_root - self.drag_origin[0], self.root.winfo_screenwidth() - self.WIDTH))
        y = max(0, min(event.y_root - self.drag_origin[1], self.root.winfo_screenheight() - 80))
        self.root.geometry(f"+{x}+{y}")

    def set_passthrough(self, enabled):
        if enabled and not all(self.hotkey_ok):
            self.status.set("快捷键注册失败时不能开启穿透，请先在设置中修改快捷键。")
            return
        set_click_through(self.hwnd, enabled)
        self.click_through = bool(enabled)
        self.interact_button.config(text="鼠标穿透" if enabled else "可操作")
        self.footer.config(text=f"{self.config['hotkey_toggle']} 展开/收起    {self.config['hotkey_interact']} 鼠标交互    ·    {'穿透中，点击会落到游戏' if enabled else '可点击按钮，可拖动标题'}")

    def toggle_interact(self):
        self.set_passthrough(not self.click_through)

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.body.pack(fill="both", expand=True, after=self.footer)
            self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}")
            self.fold_button.config(text="收起")
        else:
            self.body.pack_forget()
            self.root.geometry(f"{self.WIDTH}x78")
            self.fold_button.config(text="展开")
        self.root.update_idletasks()

    def select_team(self, mode):
        self.team_mode = mode
        self.enemy_button.config(bg="#296b65" if mode == "enemy" else "#2a3a50")
        self.ally_button.config(bg="#296b65" if mode == "ally" else "#2a3a50")
        self.render_rows()

    def opening_window_label(self):
        seconds = int(self.opening_view_seconds)
        return f"前{seconds // 60}分" if seconds % 60 == 0 else f"前{seconds}秒"

    def primary_window_label(self):
        seconds = int(self.opening_window_seconds_active)
        return f"前{seconds // 60}分钟" if seconds % 60 == 0 else f"前{seconds}秒"

    def selected_opening_states(self):
        return self.opening20_states if self.opening_view_seconds == 20 else self.opening_states

    def select_opening_window(self, seconds):
        self.opening_view_seconds = seconds
        self.analysis_mode = "opening"
        self.analysis_button.config(text="看整场出场")
        self.table.heading("recent", text="开局记录 · " + self.opening_window_label())
        self.update_summary()
        self.summary_text.yview_moveto(0)
        self.render_rows()

    def toggle_analysis(self):
        self.analysis_mode = "recent" if self.analysis_mode == "opening" else "opening"
        self.analysis_button.config(text="看开局记录" if self.analysis_mode == "recent" else "看整场出场")
        self.table.heading("recent", text="最近1场出场" if self.analysis_mode == "recent" else "开局记录 · " + self.opening_window_label())
        self.render_rows()

    def opening_row(self, player_id):
        if player_id not in self.enemy_ids:
            return "优先分析对手"
        state = self.selected_opening_states().get(player_id, {})
        data = state.get("data") or {}
        count = data.get("sample_count", 0)
        requested = data.get("requested_count", self.config.get("opening_history_limit", 5))
        if not count:
            return "开局记录未取得" if state.get("finished") else "等待开局样本"
        label = "1场记录" if count == 1 else f"样本{count}/{requested}局"
        units = data.get("units") or []
        if not units:
            return label + "\n窗口内无出场记录"
        fragments = []
        budget = int(self.table.column("recent", "width")) - 12
        for unit in units[:2]:
            name = unit_label(unit)
            suffix = f" {unit['matches_with_unit']}/{count}局"
            if self.row_font.measure(name + suffix) > budget:
                name = unit_label(unit, True)
            shortened = False
            while len(name) > 1 and self.row_font.measure(name + ("…" if shortened else "") + suffix) > budget:
                name = name[:-1]
                shortened = True
            fragments.append(name + ("…" if shortened else "") + suffix)
        return "\n".join(fragments)

    def opening_detail_lines(self, player_id, seconds=None):
        seconds = self.opening_view_seconds if seconds is None else seconds
        states = self.opening20_states if seconds == 20 else self.opening_states
        state = states.get(player_id, {})
        data = state.get("data") or {}
        samples = data.get("sample_count", 0)
        requested = data.get("requested_count", self.config.get("opening_history_limit", 5))
        seconds = data.get("window_seconds", seconds)
        heading = f"开局前{seconds}秒出场统计"
        lines = [heading, f"口径：网站对局开始后 0 ≤ 出场时间 < {seconds} 秒。", f"有效样本 {samples}/{requested} 局；出现局数 / 有效局数，不是下一局出场概率。"]
        if player_id not in self.enemy_ids:
            lines.append("自动查询优先分析对手。")
        elif not samples:
            lines.append(state.get("message") or "等待带出场时间的真实对局记录。")
        else:
            lines.append("缺失时间、未取得或失败的对局不进分母；同秒出场不推断购买顺序。")
            if samples == 1:
                lines.append("当前取得1个有效样本。")
            for unit in data.get("units") or []:
                first = unit.get("first_offset_median")
                first_text = f"；首次出场中位 {number(first, 1)} 秒" if first is not None else ""
                lines.append(f"  {unit_label(unit)}：{unit['matches_with_unit']}/{samples} 局出现，累计 {unit['total_spawns']} 次{first_text}")
            if not data.get("units"):
                lines.append("这些有效样本在所选时间窗内没有出场记录。")
            lines.append("样本来源：")
            for match in data.get("matches") or []:
                lines.append(f"  {local_time(match.get('match_end_time'))} · {match.get('source_url') or match.get('match_id')}")
        if state.get("message"):
            lines.append("进度：" + state["message"])
        skipped = data.get("unusable_matches") or []
        if skipped:
            lines.append("未计入有效样本：")
            for match in skipped:
                lines.append(f"  对局 {match.get('match_id')}：{match.get('reason') or '数据未取得'}")
        return lines

    def render_rows(self):
        previous = str(self.selected_id) if self.selected_id else None
        existing = set(self.table.get_children())
        own = next((p["team"] for p in self.players if p["id"] == self.config["self_id"]),
                   getattr(self.current_roster, 'self_team', None))
        if own is None:
            selected = [p for p in self.players if p["team"] == ("Alpha" if self.team_mode == "enemy" else "Bravo")]
        else:
            selected = [p for p in self.players if (p["team"] != own) == (self.team_mode == "enemy")]
        self.enemy_button.config(text="Alpha · 阵营待核对" if own is None and self.players else "对手  ·  5")
        self.ally_button.config(text="Bravo · 阵营待核对" if own is None and self.players else "我方  ·  5")
        source = getattr(self.current_roster, "source", None)
        prep_slots = (self.team_mode == "enemy" and
                      (source in {"screen_names", "manual_names"} or
                       self.current_roster is None and getattr(self, "live_phase", None) == "waiting"))
        if prep_slots:
            by_slot = {p["index"]: p for p in selected if "index" in p}
            rows = [by_slot.get(index) for index in range(5)]
        else:
            rows = selected
        row_info = {r["index"]: r for r in getattr(self, "prep_rows", []) if r.get("team") == "Bravo"}
        items = []
        for index, player in enumerate(rows):
            if player is None:
                status = prep_slot_status(row_info.get(index, {}))
                items.append((f"slot-{index}", (f"对手 {index + 1}", "—", "—", "—", "—", status), "pending"))
                continue
            state = self.states.get(player["id"], {})
            status = state.get("status", "pending")
            data = state.get("data") or {}
            fields, cached = overview_fields(state, player["id"])
            labels = {"pending": "查战绩中", "loading": "正在补充", "ok": "本次查询", "cached": "缓存",
                      "stale": "旧缓存", "partial": "部分资料", "error": "查询失败",
                      "timeout": "查询超时", "rate_limited": "网站限流"}
            name = player["name"] + (" · 我" if player["id"] == self.config["self_id"] else "")
            combinations = fields["specs"] or []
            tied = tied_top_count(combinations)
            spec_rows = []
            for s in combinations[:2]:
                country = combo_country(s.get("key"))
                prefix = country[0] + " " if country else ""
                suffix = "（并列）" if tied > 1 and s.get("experience") == combinations[0].get("experience") else ""
                spec_rows.append(prefix + short_combo(s) + suffix)
            spec = "\n".join(spec_rows) or "—"
            if spec_rows and "specs" in cached:
                spec += "*"
            win = number(fields["win_rate"], 1) + "%" if fields["win_rate"] is not None else "—"
            exchange = number(fields["value_exchange"], 2)
            performance = (f"胜率 {win}{'*' if 'win_rate' in cached else ''}\n"
                           f"交换 {exchange}{'*' if 'value_exchange' in cached else ''}") if data else "—"
            recon = self.recon_states.get(player["id"], {})
            sample = recon.get("data") or {}
            if recon.get("status") == "ok":
                recent_text = f"固定翼 {number(sample.get('fixed_wing'))} / 旋翼 {number(sample.get('rotorcraft'))}"
                units = sample.get("air_units") or sample.get("top_units") or []
                if units:
                    recent_text += "\n" + ", ".join(f"{unit_label(u,True)}×{u['count']}" for u in units[:2])
            elif player["id"] not in self.enemy_ids:
                recent_text = "优先查对手"
            else:
                recent_text = {"error": "样本未取得", "timeout": "预算内未取得"}.get(recon.get("status"), "等待近期样本")
            if self.analysis_mode == "opening":
                recent_text = self.opening_row(player["id"])
            if data.get("from_cache") and status in ("error", "timeout", "partial"):
                labels[status] = {"error": "旧缓存/失败", "timeout": "旧缓存/超时", "partial": "旧缓存/统计缺"}[status]
            tag = "error" if status in ("error", "timeout") else "stale" if status in ("stale", "partial") else "pending" if status == "pending" else ""
            label = "*旧缓存" if cached else labels.get(status, status)
            rating = number(fields["rating"]) + ("*" if "rating" in cached else "")
            items.append((str(player["id"]), (name, rating, performance, spec, recent_text, label), tag))
        desired = {iid for iid, _, _ in items}
        for iid in existing - desired:
            self.table.delete(iid)
        for index, (iid, values, tag) in enumerate(items):
            if iid in existing:
                self.table.item(iid, values=values, tags=(tag,))
                if self.table.index(iid) != index:
                    self.table.move(iid, "", index)
            else:
                self.table.insert("", index, iid=iid, values=values, tags=(tag,))
        if previous in desired:
            self.table.selection_set(previous)
        elif selected:
            self.table.selection_set(str(selected[0]["id"]))
        self.on_select()

    def on_select(self, event=None):
        selection = self.table.selection()
        if not selection:
            return
        if str(selection[0]).startswith("slot-"):
            self.selected_id = None
            index = int(selection[0][5:])
            row = next((r for r in self.prep_rows if r.get("team") == "Bravo" and r.get("index") == index), {})
            self.detail_title.set(f"对手 {index + 1} · {prep_slot_status(row)}")
            self.detail_text.set((row.get("resolution") or {}).get("reason") or "等待本局该位置出现可确认的昵称。")
            return
        self.selected_id = int(selection[0])
        player = next(p for p in self.players if p["id"] == self.selected_id)
        state = self.states.get(self.selected_id, {})
        data = state.get("data") or {}
        self.detail_title.set(f"{player['name']}  ·  ID {player['id']}  ·  {player['team']}")
        if not data:
            self.detail_text.set(state.get("message") or "等待公开数据返回。超过 20 秒会明确标出，其他玩家不受影响。")
            return
        metrics = profile_metrics(data)
        line1 = f"排位 {number(data.get('ranked_games'))} 场 · 退局率 {number(metrics.get('leave_rate'),1)}% · {metrics['nation_experience_line']}"
        recon = self.recon_states.get(self.selected_id, {})
        sample = recon.get("data") or {}
        if recon.get("status") == "ok":
            line2 = f"近1场 {local_time(recon.get('match_end_time'))}：步兵 {number(sample.get('infantry'))} / 车辆 {number(sample.get('vehicles'))} / 固定翼 {number(sample.get('fixed_wing'))} / 旋翼 {number(sample.get('rotorcraft'))} 次"
            units = sample.get("air_units") or sample.get("top_units") or []
            line3 = "出场记录：" + "，".join(f"{unit_label(u,True)}×{u['count']}" for u in units[:3])
        else:
            line2 = "最近1场单位：" + (recon.get("message") or ("在20秒预算内补充查询" if self.selected_id in self.enemy_ids else "自动查询优先覆盖对手"))
            line3 = "固定翼含无人机；旋翼含运输/倾转机；返场实例计入出场，不代表同时在场。"
        if state.get("status") in ("stale", "error", "timeout", "partial"):
            line3 = ("旧缓存 · " if data.get('from_cache') else "") + state.get("message", "")
        if data.get('core_ready') is False:
            line1 = f"评分 {number(data.get('rating'))} · 排名 {number(data.get('rank'))} · 部分累计字段暂缺"
        if self.analysis_mode == "opening" and self.selected_id in self.enemy_ids:
            opening = self.selected_opening_states().get(self.selected_id, {})
            summary = opening.get("data") or {}
            count = summary.get("sample_count", 0)
            requested = summary.get("requested_count", self.config.get("opening_history_limit", 5))
            line1 = f"开局{self.opening_window_label()} · 有效 {count}/{requested} 局 · " + ("1场记录" if count == 1 else "按出现局数统计")
            picks = summary.get("units") or []
            line2 = "；".join(f"{unit_label(u,True)}：{u['matches_with_unit']}/{count}局" for u in picks[:3]) if count else (opening.get("message") or "等待有时间记录的开局样本；与核心查询共用20秒截止。")
            if count and not picks:
                line2 = "有效样本在所选时间窗内没有出场记录。"
            line3 = "双击查看全部开局单位、首次出场时间和来源；上方按钮可切回整场出场。"
        _, cached = overview_fields(state, self.selected_id)
        if cached:
            line3 = "* 标记的数据沿用 " + local_time(data.get("fetched_at")) + " 的缓存。"
        self.detail_text.set("\n".join([line1, line2, line3]))

    def player_details(self, event=None):
        selection = self.table.selection()
        if not selection or str(selection[0]).startswith("slot-"):
            return
        player_id = int(selection[0])
        player = next(p for p in self.players if p["id"] == player_id)
        state = self.states.get(player_id, {})
        data = state.get("data") or {}
        dialog = tk.Toplevel(self.root)
        dialog.title(f"玩家详情 · {player['name']}")
        dialog.configure(bg=BG)
        dialog.attributes("-topmost", True)
        dialog.geometry("690x570")
        dialog.transient(self.root)
        pane = tk.Frame(dialog, bg=BG)
        pane.pack(fill="both", expand=True, padx=14, pady=14)
        scroll = tk.Scrollbar(pane)
        scroll.pack(side="right", fill="y")
        text = tk.Text(pane, bg=SURFACE, fg=FG, insertbackground=FG, font=("Microsoft YaHei UI", 10),
                       relief="flat", padx=12, pady=12, wrap="word", yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        scroll.config(command=text.yview)
        fields, cached = overview_fields(state, player_id)
        lines = [f"{player['name']}   ·   ID {player_id}   ·   {player['team']}", "",
                 f"当前评分：{number(fields['rating'], 1)}    排名：{number(fields['rank'])}",
                 f"排位累计：{record(data)}    网站统计场数：{number(data.get('ranked_games'))}",
                 f"本地抓取：{local_time(data.get('fetched_at'))}", f"网站统计更新：{local_time(data.get('stats_updated_at'))}",
                 f"状态：{state.get('message') or '等待查询'}", ""]
        fresh = state.get("partial_data") or {}
        if fresh.get("player_id") == player_id and data.get("from_cache"):
            names = {"rating": "评分", "rank": "排名", "win_rate": "胜率", "value_exchange": "交换比", "specs": "专精"}
            lines.insert(3, "本次基础资料时间 " + local_time(fresh.get("fetched_at")) +
                         " · 原始累计记录时间 " + local_time(data.get("fetched_at")))
            if cached:
                lines.insert(4, "速览沿用缓存的字段：" + "、".join(names[key] for key in cached))
        opening_lines = self.opening_detail_lines(player_id, 20) + [""]
        if self.opening_window_seconds_active != 20:
            opening_lines += self.opening_detail_lines(player_id, self.opening_window_seconds_active) + [""]
        lines[2:2] = opening_lines
        metrics = profile_metrics(data)
        lines.extend([f"排位总胜率：{number(fields['win_rate'],1)}%（胜场 / 排位总场数，含退局）",
                      f"排位价值交换比：{number(fields['value_exchange'],2)}（摧毁价值 / 损失价值）",
                      f"排位单位数量 K/D：{number(metrics['unit_kd'],2)}（击杀个数 / 损失个数）",
                      f"排位退局率：{number(metrics['leave_rate'],1)}%（仅记录离开，不推断原因）",
                      metrics['nation_experience_line'], "国家经验不是国家使用率或胜率。", "",
                      "最近1场出场记录（一次出场实例计1，不代表同时在场数量）"])
        recon = self.recon_states.get(player_id, {})
        sample = recon.get("data") or {}
        if recon.get("status") == "ok":
            lines.extend([f"对局来源：{recon.get('source_url')}", f"该局结束时间：{local_time(recon.get('match_end_time'))}",
                          "固定翼含无人机，旋翼含运输机/倾转机；车辆不等同于坦克。",
                          "返场实例仍计入，不重复统计从队友接收的单位。",
                          f"固定翼 {number(sample.get('fixed_wing'))} / 旋翼 {number(sample.get('rotorcraft'))} / 步兵 {number(sample.get('infantry'))} / 车辆 {number(sample.get('vehicles'))} / 其他或未知 {number(sample.get('unknown_types'))}"])
            if not sample.get("classification_complete", True):
                lines.append("有单位分类缺失，无法确认完整分类次数；已知单位仍列在下面。")
            for unit in sample.get("all_units") or sample.get("top_units") or []:
                lines.append(f"  {unit_label(unit)} × {unit['count']}")
        else:
            lines.append(recon.get("message") or "尚未取得；自动额外查询优先覆盖对手。")
        lines.extend(["", "历史双专精组合经验", "同经验为并列，列表次序不代表更常用；经验不是近期使用率。", ""])
        for spec in fields["specs"] or []:
            lines.append(f"{short_combo(spec)}：{number(spec.get('experience'))}  [{spec.get('label', spec.get('key'))}]")
        if not fields["specs"]:
            lines.append("暂无已取得的正经验组合。")
        lines.extend(["", "近期记录：快速查询未加载完整近期对局。" if data.get("recent_partial") else "近期记录以主窗口已取得数据为准。",
                      "", f"公开来源：https://dash.batrace.top/player/{player_id}"])
        text.insert("1.0", "\n".join(lines))
        text.config(state="disabled")
        self.button(dialog, "关闭", dialog.destroy).pack(pady=(0, 12))
        return dialog

    def copy_source(self):
        if not self.selected_id:
            self.status.set("请先在结果中选择一名玩家。")
            return
        url = f"https://dash.batrace.top/player/{self.selected_id}"
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.status.set("已复制所选玩家的 BATrace 公开来源链接。")

    def export_report(self, *, copy_file=True):
        if self.current_roster is None:
            self.status.set("尚无完整名单；名单写入游戏日志或手动历史测试后可导出。")
            return
        now = dt.datetime.now().astimezone()
        snapshot = dict(roster=asdict(self.current_roster), states=self.states,
                        opening_states=self.opening_states, recon_states=self.recon_states,
                        enemy_ids=self.enemy_ids, opening_window_seconds=self.opening_window_seconds_active,
                        generated_at=now.isoformat(timespec="seconds"), is_replay=self.is_replay, data_summary=self.summary,
                        opening20_states=self.opening20_states, data_summary20=self.summary20)
        fight_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.current_roster.fight_id))[:80]
        try:
            target_dir = f_path(ROOT / ("work/ui-test-exports" if self.args.self_test else "exports"))
            target_dir.mkdir(parents=True, exist_ok=True)
            path = f_path(target_dir / f"战前报告_20秒及{self.opening_window_seconds_active}秒_{fight_id}_{now:%Y%m%d_%H%M%S_%f}.txt")
            report = build_scout_report(**snapshot)
            with path.open("x", encoding="utf-8-sig", newline="\r\n") as output:
                output.write(report)
        except Exception as exc:
            logging.exception("Report export failed")
            self.status.set(f"TXT 导出失败：{exc}")
            return
        try:
            atomic_json(ROOT / "work" / "report-snapshots" / (path.stem + ".json"), snapshot)
        except Exception:
            logging.warning("Report saved; diagnostic snapshot could not be written", exc_info=True)
        self.last_export_path = path
        self.report_scan_id = self.scan_id
        if copy_file:
            try:
                # Tk text is rendered on demand. A clipboard reader may be
                # waiting for WM_RENDERFORMAT while holding the clipboard.
                self.root.update()
                if self.closed:
                    return path
                copy_file_to_clipboard(path, self.hwnd)
                self.status.set("完整 TXT 已保存到 exports 并复制为文件；到微信文件传输助手按 Ctrl+V 即可粘贴。")
            except Exception:
                logging.exception("Report saved but file clipboard failed")
                self.status.set("完整 TXT 已保存到工具目录 exports；剪贴板被占用，可从该目录拖到微信发送。")
        else:
            self.status.set("完整 TXT 已保存到工具目录 exports。")
        logging.info("Report exported fight=%s path=%s", fight_id, path)
        return path

    def maybe_auto_report(self):
        if self.closed or self.current_roster is None or not self.players or self.scan_id is None:
            return
        finished = not self.scanning and all(
            self.opening_states.get(pid, {}).get("finished") and
            self.opening20_states.get(pid, {}).get("finished") for pid in self.enemy_ids)
        if self.auto_report_attempted_scan_id == self.scan_id:
            if not (getattr(self, 'prep_streaming', False) and finished and
                    not getattr(self, 'auto_report_finalized', True)):
                return
        elapsed = time.monotonic() - self.started if self.started is not None else 0
        due = getattr(self, 'report_due_at', None)
        if not finished and (time.monotonic() < due if due is not None else elapsed < self.config["deadline"]):
            return
        self.auto_report_attempted_scan_id = self.scan_id
        self.auto_report_finalized = finished
        try:
            path = self.export_report(copy_file=False)
            if path:
                self.auto_report_path = path
                if getattr(self, 'prep_streaming', False):
                    self.prep_initial_report_written = True
                self.status.set("战前报告已自动保存 · " + ("历史名单回放" if self.is_replay else "本局名单") +
                                f" {self.current_roster.fight_id} · 点击“打开战前报告”查看完整数据")
                logging.info("Automatic report ready scan=%s fight=%s path=%s", self.scan_id, self.current_roster.fight_id, path)
        except Exception as exc:
            logging.exception("Automatic report failed")
            self.status.set("自动生成报告失败，可点“打开战前报告”重试：" + str(exc)[:100])

    def open_report(self):
        if self.current_roster is None or not self.players:
            self.status.set("本局名单尚未取得，暂不能生成本局战前报告。")
            return
        path = self.auto_report_path if self.report_scan_id == self.scan_id else None
        if path is None or not path.is_file():
            path = self.export_report(copy_file=False)
        if not path:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"战前报告 · {'历史回放' if self.is_replay else '本局'} {self.current_roster.fight_id}")
        dialog.geometry("800x680")
        dialog.configure(bg=BG)
        dialog.attributes("-topmost", True)
        pane = tk.Frame(dialog, bg=BG)
        pane.pack(fill="both", expand=True, padx=12, pady=12)
        scroll = tk.Scrollbar(pane)
        scroll.pack(side="right", fill="y")
        content = tk.Text(pane, bg=SURFACE, fg=FG, wrap="word", font=("Microsoft YaHei UI", 10),
                          padx=12, pady=12, yscrollcommand=scroll.set)
        content.pack(fill="both", expand=True)
        scroll.config(command=content.yview)
        content.insert("1.0", path.read_text(encoding="utf-8-sig"))
        content.config(state="disabled")
        self.button(dialog, "关闭报告", dialog.destroy).pack(pady=(0, 10))
        return dialog

    def start_scan(self, roster, replay=False, force=False, *, budget_seconds=None, started_at=None, prefetch_token=None):
        new_match = self.current_roster is None or self.current_roster.fight_id != roster.fight_id
        self.current_roster = roster
        self.is_replay = replay
        self.states = {}
        self.recon_states = {}
        self.opening_states = {}
        self.opening20_states = {}
        if new_match:
            self.opening_view_seconds = 20
            self.analysis_mode = "opening"
            self.analysis_button.config(text="看整场出场")
        if roster.source not in {"screen_names", "manual_names"}:
            self.prep_streaming = False
            self.prep_rows = []
        self.stale_history_ids = set()
        self.core_elapsed = None
        self.auto_report_attempted_scan_id = None
        self.auto_report_path = None
        self.last_export_path = None
        self.report_scan_id = None
        self.report_button.config(state="normal")
        self.players = roster.players
        own_team = next((p['team'] for p in roster.players if p['id'] == self.config['self_id']),
                        getattr(roster, 'self_team', None))
        self.enemy_ids = [p['id'] for p in roster.players if own_team is not None and p['team'] != own_team]
        self.opening_window_seconds_active = self.config.get("opening_window_seconds", 120)
        if self.opening_view_seconds != 20:
            self.opening_view_seconds = self.opening_window_seconds_active
        self.table.heading("recent", text="最近1场出场" if self.analysis_mode == "recent" else "开局记录 · " + self.opening_window_label())
        available = self.config['deadline'] - 1.0 if budget_seconds is None else min(self.config['deadline'] - 1.0, budget_seconds)
        self.recon_scan_id = self.recon.start(self.enemy_ids, deadline=max(0.1, available),
                                              window_seconds=self.config.get("opening_window_seconds", 120),
                                              history_limit=self.config.get("opening_history_limit", 5))
        self.selected_id = None
        scan_options = {}
        if budget_seconds is not None:
            scan_options['deadline_seconds'] = max(0.1, available)
        if prefetch_token is not None:
            scan_options['prefetch_token'] = prefetch_token
        self.scan_id = self.controller.start(roster, self.config["self_id"], force=force, **scan_options)
        self.prep_prefetch_token = None
        self.started = time.monotonic() if started_at is None else started_at
        self.report_due_at = self.started + self.config['deadline']
        self.scanning = True
        self.team_mode = "enemy"
        self.select_team("enemy")
        if not self.expanded:
            self.toggle()
        prefix = "历史回放测试" if replay else "本场查询"
        self.status.set(f"{prefix} · 对局 {roster.fight_id} · 对手优先，20 秒内给出已取得的结果")
        if own_team is None:
            self.status.set(f"{prefix} · 名单中没有自己的 ID；暂按 Alpha/Bravo 展示，请在设置中核对 ID。")
        self.compact.set(f"{prefix}  0/{len(roster.players)}")
        self.detail_title.set("正在查询公开战绩")
        self.detail_text.set("专精组合按历史经验排序；不会据此推断本场卡组。")
        self.show_view("data")
        self.update_summary()
        if getattr(roster, 'source', '') in {'screen_names', 'manual_names'}:
            self.status.set(f'准备阶段 · 已匹配 {len(self.enemy_ids)}/5 名对手 · 姓名来源，等待游戏 ID 名单核对')
        if new_match:
            self.summary_text.yview_moveto(0)
        logging.info("Scan requested fight=%s replay=%s players=%s", roster.fight_id, replay, len(roster.players))

    def poll_log(self):
        if self.closed:
            return
        try:
            if not self.args.self_test:
                if not Path(self.config["log_dir"]).is_dir():
                    raise FileNotFoundError("游戏日志目录不存在，请在设置中修改。")
                if self.watcher is None:
                    self.watcher = LogWatcher(self.config["log_dir"], include_existing=False)
                for event in self.watcher.poll_events():
                    if event['type'] == 'lifecycle':
                        self.handle_lifecycle(event)
                    elif event['type'] == 'roster':
                        if getattr(self, 'prep_controller', None) is not None:
                            if getattr(self, 'prep_generation', None) is not None:
                                self.record_prep_event('cancelled', {'reason': 'authoritative_roster'})
                            self.prep_controller.cancel()
                            self.prep_generation = None
                        self.live_phase = 'game_room'
                        self.live_fight_id = event['roster'].fight_id
                        self.start_scan(event['roster'])
            self.last_log_error = None
        except Exception as exc:
            message = str(exc)
            if message != self.last_log_error:
                logging.warning("Log monitor: %s", message)
                self.status.set("日志监听提示：" + message[:140])
                self.last_log_error = message
        self.root.after(500, self.poll_log)

    def handle_lifecycle(self, event):
        """Invalidate the displayed match before any new roster is available."""
        # The early screen roster belongs to this same fight. Loading is not a
        # new match; retain its useful data until the authoritative IDs arrive.
        preserve_early = (not event.get('manual') and event['phase'] in {'loading', 'game_room'} and
                          self.current_roster is not None and
                          getattr(self.current_roster, 'source', '') in {'screen_names', 'manual_names'} and
                          event.get('fight_id') == self.current_roster.fight_id)
        previous_fight_id = self.live_fight_id
        self.live_phase = event['phase']
        self.live_fight_id = event.get('fight_id')
        if preserve_early:
            if getattr(self, 'prep_streaming', False) and self.prep_controller is not None:
                self.record_prep_event('cancelled', {'reason': event['phase']})
                self.prep_controller.cancel()
                self.prep_generation = None
            self.status.set(f'本局 {self.live_fight_id}正在加载 · 保留准备阶段数据，等待正式 ID 名单核对')
            return
        if getattr(self, 'prep_controller', None) is not None:
            if getattr(self, 'prep_generation', None) is not None:
                self.record_prep_event('cancelled', {'reason': event['phase'], 'fight_id': previous_fight_id})
            self.prep_controller.cancel()
        self.prep_generation = None
        self.prep_last_status_key = None
        self.prep_rows = []
        self.prep_streaming = False
        self.prep_revision = -1
        self.report_due_at = None
        self.prep_initial_report_written = False
        dialog = getattr(self, 'prep_dialog', None)
        if dialog is not None and dialog.exists():
            dialog.update_rows([])
        self.controller.cancel()
        self.prep_prefetch_token = None
        self.recon.cancel()
        # Invalidate IDs as well as cancelling workers: queued events may still
        # arrive after cancel and must never repopulate a previous match.
        self.scan_id = self.recon_scan_id = None
        self.started = self.core_elapsed = None
        self.scanning = False
        self.current_roster = None
        self.players = []
        self.enemy_ids = []
        self.selected_id = None
        self.states = {}
        self.recon_states = {}
        self.opening_states = {}
        self.opening20_states = {}
        self.stale_history_ids = set()
        self.is_replay = False
        self.auto_report_attempted_scan_id = None
        self.auto_report_path = self.last_export_path = None
        self.report_scan_id = None
        self.report_button.config(state='disabled')
        self.render_rows()
        self.update_summary()
        fight = f" {self.live_fight_id}" if self.live_fight_id else ''
        messages = {
            'waiting': f'已识别新局{fight} · 等待本局名单写入游戏日志',
            'loading': f'本局{fight}正在加载 · 等待本局名单写入游戏日志',
            'game_room': f'已进入本局{fight} · 等待完整十人名单',
            'ended': f'对局{fight}已结束 · 等待新局',
            'lobby': '当前已回到大厅 · 等待新局',
            'session': '已开始监听本次游戏日志 · 等待本局名单',
        }
        message = messages.get(self.live_phase, '等待本局名单')
        self.status.set(message)
        self.compact.set('等待本局名单' if self.live_phase in {'waiting', 'loading', 'game_room'} else '等待新对局')
        self.detail_title.set('当前没有已确认的本局名单')
        self.detail_text.set('名单取得后自动查询；历史报告保存在 exports 文件夹。')
        self.summary_heading.set('等待本局数据')
        self.summary_confidence.set(message)
        logging.info('Match lifecycle phase=%s fight=%s startup=%s; display cleared',
                     self.live_phase, self.live_fight_id, bool(event.get('startup')))
        if event['phase'] == 'waiting' and not event.get('manual') and not self.args.self_test:
            self.restart_prep_if_waiting()

    def warmup_ocr(self):
        """Move OCR startup into idle time without capturing or blocking Tk."""
        if self.closed or self.args.self_test or not self.config.get('prep_enabled', True):
            return
        if getattr(self, 'ocr_warmup_future', None) is not None:
            return
        try:
            self.ensure_prep_tools()
            self.ocr_warmup_future = self.screen_ocr.submit_warmup(timeout=8)
        except Exception:
            logging.warning('OCR background warmup unavailable; normal capture can retry', exc_info=True)

    def ensure_prep_tools(self):
        if self.screen_ocr is None:
            from .screen_ocr import ScreenOCR
            self.screen_ocr = ScreenOCR(temp_dir=f_path(ROOT / 'work/prep-capture'))
        if self.prep_controller is None:
            from .prep_engine import PrepController
            self.prep_controller = PrepController(self.screen_ocr, self.request_gate,
                                                  total_budget=self.config['deadline'],
                                                  lookup_budget=min(12.0, self.config['deadline'] * 0.6))

    def open_prep_dialog(self):
        self.set_passthrough(False)
        if self.prep_dialog is not None and self.prep_dialog.exists():
            self.prep_dialog.root.lift()
            return self.prep_dialog.root
        from .prep_dialog import PrepDialog
        self.prep_dialog = PrepDialog(self)
        return self.prep_dialog.root

    def restart_prep_if_waiting(self):
        if self.live_phase != 'waiting':
            return
        if not self.config.get('prep_enabled', True):
            logging.info('Preparation skipped fight=%s reason=disabled', self.live_fight_id)
            return
        if not self.expanded:
            self.toggle()
        self.analysis_mode = "opening"
        self.opening_view_seconds = 20
        self.analysis_button.config(text="看整场出场")
        self.table.heading("recent", text="开局记录 · 前20秒")
        self.show_view("data")
        self.select_team("enemy")
        if not self.config.get('prep_regions'):
            self.show_prep_state('已进入准备阶段 · 请点“准备识别”设置双方姓名位置；也可输入名字立即查询',
                                 '准备阶段 · 尚未设置姓名区域', '准备识别需要设置姓名区域')
            logging.info('Preparation skipped fight=%s reason=missing_regions', self.live_fight_id)
            return
        try:
            self.ensure_prep_tools()
            if getattr(self, 'prep_prefetch_token', None) is not None:
                self.controller.cancel()
                self.prep_prefetch_token = None
            self.prep_controller.cancel()
            self.prep_generation = self.prep_controller.start(self.live_fight_id, self.config['prep_regions'])
            self.prep_streaming = True
            self.prep_revision = -1
            self.prep_last_status_key = None
            self.record_prep_event('started')
            self.show_prep_state('准备阶段 · 正在读取游戏画面上的双方名字；取得连续两帧稳定昵称后自动查询。',
                                 '准备阶段 · 正在读取姓名', '准备阶段 · 正在读取姓名')
            ScoutWindow.raise_overlay(self)
        except Exception as exc:
            self.show_prep_state('准备识别不可用：' + str(exc)[:130] + '；可点“准备识别”手动输入名字',
                                 '准备阶段 · 识别不可用', '准备识别未启动')
            logging.warning('Preparation unavailable fight=%s error_type=%s', self.live_fight_id, type(exc).__name__)

    def show_prep_state(self, message, compact, heading):
        """Make pending/failed preparation observable even without any player IDs."""
        self.status.set(message)
        self.compact.set(compact)
        self.summary_heading.set(heading)
        self.summary_confidence.set(message)
        if self.current_roster is None:
            self.detail_title.set(heading)
            self.detail_text.set(message)
        self.summary_text.config(state='normal')
        self.summary_text.delete('1.0', 'end')
        self.summary_text.insert('end', message + '\n\n')
        self.summary_text.insert('end', '准备名单尚未完成确认；未识别的人不会按 0 战绩显示。\n'
                                 f'可按 {self.config.get("hotkey_interact", "CTRL+ALT+M")} 允许操作，再点“准备识别”查看或校正昵称。')
        self.summary_text.config(state='disabled')

    def raise_overlay(self):
        """Reassert the floating window without activating it or the game."""
        hwnd = getattr(self, 'hwnd', 0)
        if not hwnd:
            return
        # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW.
        ok = bool(user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x53))
        logging.info('Preparation overlay topmost requested ok=%s visible=%s',
                     ok, bool(user32.IsWindowVisible(hwnd)))

    def apply_prep_snapshot(self, event):
        """Append newly confirmed opponents without restarting earlier work."""
        if self.live_phase != 'waiting' or str(event['fight_id']) != str(self.live_fight_id):
            return
        revision = event.get('revision')
        if type(revision) is not int or revision <= getattr(self, 'prep_revision', -1):
            return
        self.prep_revision = revision
        self.prep_streaming = True
        players = event.get('players') or []
        self.prep_rows = event.get('rows') or []
        if self.prep_dialog is not None and self.prep_dialog.exists():
            self.prep_dialog.update_rows(self.prep_rows)
        if any(p['id'] == self.config['self_id'] and p['team'] != 'Alpha' for p in players):
            players = []
        old_ids = {p['id'] for p in self.players}
        allowed = {p['id'] for p in players}
        same_stream = (self.current_roster is not None and
                       self.current_roster.fight_id == str(event['fight_id']) and
                       self.current_roster.source == 'screen_names')
        if not players:
            if same_stream:
                self.controller.cancel()
                self.recon.cancel()
                self.scan_id = self.recon_scan_id = None
                self.current_roster = None
                self.players = self.enemy_ids = []
                self.selected_id = None
                self.states = {}
                self.recon_states = {}
                self.opening_states = {}
                self.opening20_states = {}
                self.scanning = False
                self.auto_report_path = self.last_export_path = None
                self.report_scan_id = None
                self.report_button.config(state='disabled')
            self.render_rows()
            self.show_prep_state('准备阶段 · 持续读取尚未确认的对手；已变化或有歧义的身份不会沿用。',
                                 '准备阶段 · 等待明确身份', '准备名单 · 自动补识别中')
            return
        notes = ['名单来自准备画面姓名识别及公开昵称搜索；尚未由游戏数字 ID 名单校核。',
                 '敌我分组来自设置的姓名区域；不按名字、分数或网站排名猜阵营。',
                 f'姓名成功匹配 {len(players)}/10 人，对手 {sum(p["team"] == "Bravo" for p in players)}/5 人；未匹配的人未计入统计。']
        for row in self.prep_rows:
            decision = row.get('resolution') or {}
            if decision.get('status') not in {'resolved', 'manual-confirmed'}:
                notes.append(f"{'对手' if row.get('team') == 'Bravo' else '我方'}第 {row.get('index', 0)+1} 位：{decision.get('reason') or '等待稳定姓名后自动补查'}")
        roster = Roster(str(event['fight_id']), players, dt.datetime.now().astimezone().isoformat(), '', '',
                        source='screen_names', self_team='Alpha', identity_notes=notes)
        now = time.monotonic()
        remaining = min(self.config['deadline'] - 1, max(0.1, event['deadline'] - now - 0.2))
        if not same_stream:
            self.start_scan(roster, budget_seconds=remaining, started_at=event['started'])
        else:
            self.current_roster = roster
            self.players = players
            self.enemy_ids = [p['id'] for p in players if p['team'] == 'Bravo']
            for field in ('states', 'recon_states', 'opening_states', 'opening20_states'):
                values = getattr(self, field)
                setattr(self, field, {pid: value for pid, value in values.items() if pid in allowed})
            self.stale_history_ids.intersection_update(allowed)
            if self.selected_id not in allowed:
                self.selected_id = None
            self.scan_id = self.controller.update_roster(
                roster, self.config['self_id'], deadline_seconds=remaining,
                player_deadlines=event.get('player_deadlines'))
            self.recon_scan_id = self.recon.update_players(
                self.enemy_ids, deadline=remaining, player_deadlines=event.get('player_deadlines'))
            if allowed - old_ids:
                self.scanning = True
                self.core_elapsed = None
            self.render_rows()
            self.update_summary()
        if allowed != old_ids or not same_stream:
            self.auto_report_attempted_scan_id = None
            self.auto_report_path = self.last_export_path = None
            self.report_scan_id = None
            initial_due = event.get('initial_deadline') or event['deadline']
            self.report_due_at = (max(initial_due, now + remaining)
                                  if getattr(self, 'prep_initial_report_written', False) else initial_due)
        self.status.set(f'准备阶段 · 已匹配 {len(self.enemy_ids)}/5 名对手 · '
                        + ('姓名匹配完成，战绩与开局记录陆续更新' if event.get('final') else '正在自动补齐其余对手'))
        self.compact.set(f'准备对手 {len(self.enemy_ids)}/5 · 自动补齐中')
        ScoutWindow.raise_overlay(self)
        logging.info('Preparation roster updated fight=%s revision=%s enemies=%s added=%s removed=%s final=%s',
                     event['fight_id'], revision, len(self.enemy_ids), len(allowed-old_ids), len(old_ids-allowed), event.get('final'))

    def record_prep_event(self, stage, event=None):
        """Log bounded codes/counts only; never raw OCR names or candidates."""
        event = event or {}
        statuses = {'waiting', 'stabilizing', 'searching', 'capture_error', 'error', 'names_unavailable', 'request_limit'}
        captures = {'invalid_geometry', 'not_found', 'ambiguous_window', 'minimized', 'not_visible',
                    'not_foreground', 'capture_failed', 'unsupported_dimensions', 'occluded',
                    'window_changed', 'black_frame', 'timeout', 'worker_error', 'busy', 'closed', 'invalid_input'}
        status = event.get('status') if event.get('status') in statuses else None
        capture = event.get('capture_status') if event.get('capture_status') in captures else None
        counts = []
        for field, pattern in (('queryable_bravo', r'当前可查询 (\d)/5'), ('stable_bravo', r'已稳定 (\d)/5')):
            value = event.get(field)
            if type(value) is not int or not 0 <= value <= 5:
                match = re.search(pattern, str(event.get('message', '')))
                value = int(match.group(1)) if match else None
            counts.append(value)
        result_counts = {}
        for row in event.get('rows') or []:
            decision = (row.get('resolution') or {}).get('status')
            if decision not in {'resolved', 'manual-confirmed', 'missing', 'ambiguous', 'not_found', 'unmatched', 'incomplete',
                                'invalid', 'error', 'timeout', 'conflict', 'searching', 'retrying', 'rate_limited'}:
                decision = 'other'
            result_counts[decision] = result_counts.get(decision, 0) + 1
        players = event.get('players') or []
        key = (self.prep_generation, status, capture, *counts)
        if stage == 'status' and key == getattr(self, 'prep_last_status_key', None):
            return
        if stage == 'status':
            self.prep_last_status_key = key
        reason = event.get('reason')
        if reason not in {'waiting', 'loading', 'game_room', 'ended', 'lobby', 'session',
                          'authoritative_roster', 'no_players', 'own_id_in_enemy', 'deadline', 'close'}:
            reason = None
        logging.info('Preparation %s fight=%s generation=%s status=%s capture=%s queryable=%s stable=%s '
                     'players=%s enemies=%s rows=%s reason=%s', stage,
                     event.get('fight_id', self.live_fight_id), self.prep_generation, status, capture,
                     counts[0], counts[1], len(players), sum(p.get('team') == 'Bravo' for p in players),
                     json.dumps(result_counts, sort_keys=True), reason)

    def start_prep_manual(self, rows):
        self.ensure_prep_tools()
        phase, fight = self.live_phase, self.live_fight_id
        self.handle_lifecycle(dict(phase=phase or 'session', fight_id=fight, manual=True))
        self.prep_generation = self.prep_controller.start(fight or '未绑定对局', {}, manual_rows=rows)
        self.prep_last_status_key = None
        self.record_prep_event('started')
        self.show_prep_state('准备名单 · 正在搜索玩家 ID；已确认的同名选择将按你的选择查询',
                             '准备阶段 · 查询手动名单', '准备名单 · 正在查询身份')

    def poll_prep(self):
        for event in self.prep_controller.poll_events():
            if event.get('generation') != self.prep_generation:
                continue
            if event['type'] == 'identity_ready':
                # This row is exact, but other rows may still expose duplicate
                # identities. Prefetch privately; only resolved commits a roster.
                if event.get('team') != 'Bravo' or event.get('deadline', 0) <= time.monotonic():
                    continue
                token = getattr(self, 'prep_prefetch_token', None)
                try:
                    if token is None:
                        token = self.controller.begin_prefetch(str(event['fight_id']), event['deadline'])
                        self.prep_prefetch_token = token
                    queued = self.controller.prefetch(token, event['player_id'])
                except (ValueError, RuntimeError):
                    logging.warning('Preparation profile prefetch unavailable; final roster can query normally', exc_info=True)
                    continue
                if queued:
                    logging.info('Preparation profile prefetch fight=%s generation=%s row=%s elapsed=%.3f',
                                 event['fight_id'], event['generation'], event.get('index'),
                                 time.monotonic() - event['started'])
                continue
            if event['type'] == 'status':
                self.record_prep_event('status', event)
                message = '准备识别 · ' + event.get('message', event.get('status', '正在识别'))
                if event.get('progressive') and self.current_roster is not None and self.current_roster.source == 'screen_names':
                    self.status.set(message)
                    continue
                if event.get('status') == 'names_unavailable':
                    self.show_prep_state('画面显示 Player name 占位文字；出现真实名字后自动继续。',
                                         '等待真实对手昵称', '敌方姓名暂未显示')
                elif event.get('status') == 'capture_error':
                    reason = {'not_foreground': '请切回游戏', 'occluded': '游戏画面被其他窗口遮挡',
                              'minimized': '游戏已最小化', 'not_found': '未找到游戏窗口',
                              'black_frame': '画面未能读取', 'timeout': '画面读取超时'}.get(event.get('capture_status'), '画面暂不可用')
                    self.show_prep_state(message + '；将继续尝试，也可点“准备识别”手动输入。',
                                         '准备识别 · ' + reason, '准备识别 · ' + reason)
                elif event.get('status') == 'error':
                    self.show_prep_state(message, '准备阶段 · 识别出错待校正', '准备识别未完成')
                elif event.get('status') == 'request_limit':
                    self.show_prep_state(message + ' 已完成的身份继续查战绩，其余姓名保留待确认。',
                                         '准备阶段 · 姓名搜索已达上限', '准备名单 · 部分待确认')
                elif event.get('capture_status') == 'invalid_geometry':
                    self.show_prep_state(message + '；请点“准备识别”重新设置姓名区域。',
                                         '准备阶段 · 姓名区域需校正', '姓名区域校准无效')
                else:
                    queryable = re.search(r'当前可查询 (\d)/5', message)
                    compact = ('准备姓名 ' + queryable.group(1) + '/5 · 等待稳定' if queryable else
                               '准备阶段 · 正在查询身份' if event.get('status') == 'searching' else
                               '准备阶段 · 识别名单中')
                    heading = '准备阶段 · 等待稳定姓名' if event.get('status') in {'waiting', 'stabilizing'} else '准备阶段 · 正在查询身份'
                    self.show_prep_state(message, compact, heading)
                continue
            if event['type'] != 'resolved':
                continue
            self.record_prep_event('finished', event)
            if event.get('progressive'):
                ScoutWindow.apply_prep_snapshot(self, event)
                continue
            self.prep_rows = event.get('rows', [])
            if self.prep_dialog is not None and self.prep_dialog.exists():
                self.prep_dialog.update_rows(self.prep_rows)
            players = event.get('players') or []
            if not players:
                if getattr(self, 'prep_prefetch_token', None) is not None:
                    self.controller.cancel()
                    self.prep_prefetch_token = None
                reasons = list(dict.fromkeys(str((row.get('resolution') or {}).get('reason') or '')
                                             for row in self.prep_rows))
                detail = '；'.join(reason[:140] for reason in reasons if reason)[:420]
                self.show_prep_state('准备名单尚未匹配到明确玩家 · 点“准备识别”校正姓名或选择同名 ID'
                                     + ('\n原因：' + detail if detail else ''),
                                     '准备阶段 · 名单待校正', '准备名单未匹配到明确玩家')
                self.record_prep_event('not_started', {'reason': 'no_players'})
                continue
            if any(p['id'] == self.config['self_id'] and p['team'] != 'Alpha' for p in players):
                if getattr(self, 'prep_prefetch_token', None) is not None:
                    self.controller.cancel()
                    self.prep_prefetch_token = None
                self.show_prep_state('自己的 ID 出现在对手区域 · 请点“准备识别”核对双方分组后重试',
                                     '准备阶段 · 阵营待校正', '准备名单的双方分组需要校正')
                self.record_prep_event('not_started', {'reason': 'own_id_in_enemy'})
                continue
            now = time.monotonic()
            remaining = event['deadline'] - now - 0.2
            if remaining <= 0:
                if getattr(self, 'prep_prefetch_token', None) is not None:
                    self.controller.cancel()
                    self.prep_prefetch_token = None
                self.show_prep_state('本次姓名识别已到截止时间 · 请点“准备识别”校正后重试',
                                     '准备阶段 · 姓名识别已截止', '准备名单识别已截止')
                self.record_prep_event('not_started', {'reason': 'deadline'})
                continue
            notes = ['名单来自准备画面姓名识别/手动校正及公开昵称搜索；尚未由游戏数字 ID 名单校核。',
                     '敌我分组来自你设置的姓名区域或手动分组；不按名字、分数或网站排名猜阵营。',
                     f'姓名成功匹配 {len(players)}/10 人，对手 {sum(p["team"] == "Bravo" for p in players)}/5 人；未匹配的人未计入统计。']
            for row in self.prep_rows:
                decision = row.get('resolution') or {}
                if decision.get('status') not in {'resolved', 'manual-confirmed'}:
                    notes.append(f"{'对手' if row.get('team') == 'Bravo' else '我方'}第 {row.get('index', 0)+1} 位：{decision.get('reason') or row.get('reason') or '姓名或身份未确认'}")
            roster = Roster(str(event['fight_id']), players, dt.datetime.now().astimezone().isoformat(), '', '',
                            source='screen_names' if event.get('source') == 'screen' else 'manual_names',
                            self_team='Alpha', identity_notes=notes)
            self.start_scan(roster, budget_seconds=remaining, started_at=event['started'],
                            prefetch_token=getattr(self, 'prep_prefetch_token', None))
            logging.info('Preparation names ready fight=%s players=%s remaining=%.3f', roster.fight_id, len(players), remaining)

    def tick(self):
        if self.closed:
            return
        warmup = getattr(self, 'ocr_warmup_future', None)
        if warmup is not None and warmup.done():
            self.ocr_warmup_future = None
            try:
                result = warmup.result()
                logging.info('OCR background warmup status=%s elapsed=%s', result.get('status'), result.get('client_elapsed_seconds'))
            except Exception:
                logging.warning('OCR background warmup did not finish', exc_info=True)
        if getattr(self, 'prep_controller', None) is not None:
            self.poll_prep()
        for hotkey in self.hotkeys.poll():
            if hotkey == 101:
                self.toggle()
            elif hotkey == 102:
                self.toggle_interact()
        changed = False
        if getattr(self, 'history_scan_id', None) != self.recon_scan_id:
            self.history_scan_id = self.recon_scan_id
            self.submitted_histories = {}
        for event in self.controller.poll_events():
            if event.get("scan_id") != self.scan_id:
                continue
            if event["type"] in ("player", "player_progress"):
                if (getattr(self, 'prep_streaming', False) and
                        event['player_id'] not in {p['id'] for p in self.players}):
                    continue
                self.states[event["player_id"]] = event
                profile = event.get("partial_data") or event.get("data") or {}
                if event['player_id'] in self.enemy_ids:
                    history = profile_history(profile)
                    if history is not None and (history or event["type"] == 'player'):
                        if event.get("status") == "stale" or profile.get('from_cache') or profile.get('history_from_cache'):
                            self.stale_history_ids.add(event["player_id"])
                        else:
                            self.stale_history_ids.discard(event["player_id"])
                        signature = tuple(map(str, history))
                        if self.submitted_histories.get(event["player_id"]) != signature:
                            self.recon.submit_history(event["player_id"], history)
                            self.submitted_histories[event["player_id"]] = signature
                    elif (event['type'] == 'player' and event.get('status') != 'stale'
                          and event['player_id'] not in self.submitted_histories):
                        reason = event.get('message') if event.get('status') in ('error', 'timeout', 'rate_limited') else None
                        self.recon.mark_unavailable(event['player_id'], reason or '本次玩家资料未返回近期对局清单')
                changed = True
            elif event["type"] == "finished":
                self.scanning = (getattr(self, 'prep_streaming', False) and
                                 any(self.states.get(p['id'], {}).get('status') not in
                                     {'ok', 'cached', 'partial', 'error', 'timeout', 'rate_limited'} for p in self.players))
                changed = True
                self.core_elapsed = event['elapsed']
                completed = sum(s.get("status") in ("ok", "cached") for s in self.states.values())
                available = sum(bool(s.get("data")) for s in self.states.values())
                prefix = "历史回放测试" if self.is_replay else "本场查询"
                self.status.set(f"{prefix} · 对局 {self.current_roster.fight_id} · {event['elapsed']:.1f} 秒 · 有效 {completed}/{len(self.players)}，含旧缓存可查看 {available} 人")
                self.compact.set(f"{'历史测试' if self.is_replay else '本场'}  {completed}/{len(self.players)} 已查到")
                logging.info("Scan finished fight=%s elapsed=%.3f complete=%s total=%s", self.current_roster.fight_id, event["elapsed"], completed, len(self.players))
        for event in self.recon.poll_events():
            if event.get('scan_id') != self.recon_scan_id:
                continue
            if event.get('type') in {'recon', 'opening'} and event.get('player_id') not in self.enemy_ids:
                continue
            if event.get('type') == 'recon':
                self.recon_states[event['player_id']] = event
                changed = True
            elif event.get('type') == 'opening':
                self.opening_states[event['player_id']] = event
                early = (event.get('windows') or {}).get('20')
                # Never label an old 120-second aggregate as a 20-second result.
                if early is not None:
                    self.opening20_states[event['player_id']] = early
                else:
                    self.opening20_states.pop(event['player_id'], None)
                changed = True
        if changed:
            self.render_rows()
            self.update_summary()
            if not self.scanning and self.core_elapsed is not None:
                completed = sum(s.get('status') in ('ok', 'cached') for s in self.states.values())
                recent_count = sum(s.get('status') == 'ok' for s in self.recon_states.values())
                opening_count = sum((s.get('data') or {}).get('sample_count', 0) > 0 for s in self.opening_states.values())
                early_count = sum((s.get('data') or {}).get('sample_count', 0) > 0 for s in self.opening20_states.values())
                prefix = '历史回放测试' if self.is_replay else '本场查询'
                self.status.set(f"{prefix} · {self.current_roster.fight_id} · 核心 {completed}/{len(self.players)}（{self.core_elapsed:.1f}s）· 20秒 {early_count}/{len(self.enemy_ids)}人 · {self.primary_window_label()} {opening_count}/{len(self.enemy_ids)}人")
                self.compact.set(f"核心 {completed}/{len(self.players)} · 20s {early_count}/{len(self.enemy_ids)} · {self.opening_window_seconds_active}s {opening_count}/{len(self.enemy_ids)}")
        if self.scanning and self.started is not None:
            completed = sum(s.get("status") in ("ok", "cached") for s in self.states.values())
            elapsed = min(self.config["deadline"], time.monotonic() - self.started)
            self.compact.set(f"{'历史测试' if self.is_replay else '查询中'}  {completed}/{len(self.players)}  ·  {elapsed:.0f}/20s")
        if getattr(self, 'prep_streaming', False) and self.live_phase == 'waiting' and self.current_roster is not None:
            completed = sum(self.states.get(pid, {}).get('status') in ('ok', 'cached') for pid in self.enemy_ids)
            self.compact.set(f'准备对手 {len(self.enemy_ids)}/5 · 战绩 {completed}/5 · 自动补齐')
        self.maybe_auto_report()
        self.root.after(60, self.tick)

    def replay_latest(self):
        try:
            roster = recent_roster(self.config["log_dir"])
            if roster is None:
                self.status.set("最新日志没有可用的完整名单；新局等待期间不会改用更早日志的上一局。")
                return
            self.start_scan(roster, replay=True)
        except Exception as exc:
            self.status.set("历史测试失败：" + str(exc)[:150])

    def replay_path(self, path):
        try:
            path = read_path(path)
            roster = parse_latest_roster(path.read_text(encoding="utf-8", errors="replace"), str(path))
            if not roster:
                raise ValueError("指定日志中没有完整名单")
            self.start_scan(roster, replay=True)
        except Exception as exc:
            self.status.set("历史测试失败：" + str(exc)[:150])

    def refresh(self):
        if self.scanning:
            self.status.set("本轮仍在查询，请等待本轮结束后刷新。")
        elif self.current_roster:
            self.start_scan(self.current_roster, replay=self.is_replay, force=True)
        else:
            self.status.set("尚未读取本局完整名单；名单写入游戏日志后会自动查询。")

    def settings_dialog(self):
        self.set_passthrough(False)
        dialog = tk.Toplevel(self.root)
        dialog.title("断箭战前情报 · 设置")
        dialog.configure(bg=BG)
        dialog.attributes("-topmost", True)
        dialog.resizable(False, False)
        entries = {}
        for row, (key, title) in enumerate([( "self_id", "自己的数字玩家 ID"), ("log_dir", "游戏日志目录（GameLogs）"),
                                           ("hotkey_toggle", "展开 / 收起快捷键"), ("hotkey_interact", "切换鼠标操作快捷键"),
                                           ("opening_window_seconds", "第二观察窗（10–300秒；另有固定20秒）"), ("opening_history_limit", "每人样本上限（1–5局）")]):
            tk.Label(dialog, text=title, bg=BG, fg=FG, font=("Microsoft YaHei UI", 9)).grid(row=row, column=0, sticky="w", padx=12, pady=9)
            value = tk.StringVar(value=str(self.config[key]))
            tk.Entry(dialog, textvariable=value, width=55, font=("Microsoft YaHei UI", 9)).grid(row=row, column=1, padx=12, pady=9)
            entries[key] = value
        note = tk.StringVar(value="请填写你自己的数字玩家 ID。配置、缓存、截图和报告保存在软件目录内。")
        tk.Label(dialog, textvariable=note, bg=BG, fg=YELLOW, wraplength=600).grid(row=6, column=0, columnspan=2, padx=12, pady=10)

        def save():
            from .windows import parse_hotkey
            try:
                own_id = int(entries["self_id"].get())
                if own_id <= 0:
                    raise ValueError("玩家 ID 必须为正整数")
                directory = read_path(entries["log_dir"].get())
                if not directory.is_dir():
                    raise ValueError("游戏日志目录不存在")
                toggle = entries["hotkey_toggle"].get().upper().replace(" ", "")
                interact = entries["hotkey_interact"].get().upper().replace(" ", "")
                parse_hotkey(toggle)
                parse_hotkey(interact)
                if toggle == interact:
                    raise ValueError("两个快捷键不能相同")
                window_seconds = int(entries["opening_window_seconds"].get())
                history_limit = int(entries["opening_history_limit"].get())
                if not 10 <= window_seconds <= 300 or not 1 <= history_limit <= 5:
                    raise ValueError("开局时间窗应为10–300秒，每人样本上限应为1–5局")
                self.hotkeys.close()
                self.hotkeys = Hotkeys()
                self.hotkey_ok = (self.hotkeys.register(101, toggle), self.hotkeys.register(102, interact))
                self.config.update(self_id=own_id, log_dir=str(directory), hotkey_toggle=toggle, hotkey_interact=interact,
                                   opening_window_seconds=window_seconds, opening_history_limit=history_limit)
                self.watcher = LogWatcher(str(directory), include_existing=False)
                atomic_json(ROOT / "config.json", self.config)
                self.set_passthrough(False)
                self.table.heading("recent", text="最近1场出场" if self.analysis_mode == "recent" else "开局记录 · " + self.opening_window_label())
                self.render_rows()
                dialog.destroy()
                self.status.set("设置已保存，开局时间窗从下次查询生效。" + ("有快捷键被其他程序占用，请重新设置。" if not all(self.hotkey_ok) else ""))
            except Exception as exc:
                note.set(str(exc))

        self.button(dialog, "保存", save).grid(row=7, column=1, sticky="e", padx=12, pady=12)
        dialog.transient(self.root)

    def callback_error(self, exc_type, exc, tb):
        logging.error("UI callback failure", exc_info=(exc_type, exc, tb))
        self.status.set("操作失败，详情见工具目录 data/runtime.log：" + str(exc)[:100])

    @staticmethod
    def fake_fetch(player_id, timeout=12):
        time.sleep(0.08)
        return {"player_id": player_id, "name": f"测试玩家{player_id}", "rating": 2200 + player_id % 100,
                "rank": 1200, "wins": 120, "losses": 90, "leaves": 3, "ranked_games": 213,
                "profile_version": 4, "ranked_kills": 1000, "ranked_losses_units": 800,
                "ranked_killed_value": 120000, "ranked_lost_value": 100000, "ranked_duration_seconds": 600000,
                "nation_experience": {"1":30000,"2":40000}, "last_match_id":"999999991", "recent_match_ids":["999999991", "999999992"],
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "stats_updated_at": "2026-10-25T06:08:36+00:00",
                "specs": [{"key": "5_6", "label": "空降师 + 斯特瑞克旅", "experience": 87654},
                          {"key": "3_4", "label": "装甲旅 + 海军陆战队", "experience": 54321}],
                "recent": [{"match_id": 1, "outcome": "unknown", "rating_delta": 12}],
                "source_url": f"https://dash.batrace.top/player/{player_id}"}

    def fake_match(self, match_id, timeout=8):
        time.sleep(.05)
        unit = {"id":285,"name":"F-16C Fighting Falcon","count":2,"type":16}
        return {"recon_version": 2, "match_id":str(match_id), "source_url":f"https://dash.batrace.top/match/{match_id}",
                "fetched_at":dt.datetime.now(dt.timezone.utc).isoformat(), "match_start_time":1000,"match_end_time":2000,
                "players":{str(p['id']):{"player_id":p['id'],"available":True,"classification_complete":True,
                            "total_spawns":20,"fixed_wing":2,"rotorcraft":3,"infantry":7,"vehicles":8,"unknown_types":0,
                            "top_units":[unit],"all_units":[unit],"air_units":[unit],
                            "deployment_timing":{"available":True,"complete":True,"start_time":1000,"end_time":2000,
                                "missing_spawn_times":0,"before_start":0,"after_end":0,"missing_unit_ids":0,
                                "spawns":[{"id":285,"name":"F-16C Fighting Falcon","type":16,"offset_s":3},
                                          {"id":285,"name":"F-16C Fighting Falcon","type":16,"offset_s":40}]}} for p in self.players}}

    def self_test_begin(self):
        from .windows import GWL_EXSTYLE, TRANSPARENT, NOACTIVATE, TOPMOST
        def check(name, condition):
            self.selftest_results.append({"name": name, "passed": bool(condition)})
        self._test_check = check
        check("hotkeys_registered", all(self.hotkey_ok))
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        check("native_topmost", bool(style & TOPMOST))
        check("native_clickthrough_noactivate", bool(style & TRANSPARENT and style & NOACTIVATE))
        check("visible_window", bool(user32.IsWindowVisible(self.hwnd)))
        from ctypes import wintypes as w
        point = w.POINT(self.root.winfo_x() + 300, self.root.winfo_y() + 200)
        hit = user32.WindowFromPoint(point)
        check("clickthrough_native_hit_test", user32.GetAncestor(hit, 2) != self.hwnd)
        exercise_hotkey(self.config["hotkey_interact"])
        self.root.after(450, self.self_test_step2)

    def self_test_step2(self):
        self._test_check("hotkey_dispatch_interact", not self.click_through)
        from ctypes import wintypes as w
        point = w.POINT(self.root.winfo_x() + 300, self.root.winfo_y() + 200)
        hit = user32.WindowFromPoint(point)
        self._test_check("interactive_native_hit_test", user32.GetAncestor(hit, 2) == self.hwnd)
        players = [{"id": 990000 + i, "name": "界面测试 · 玩家" + str(i + 1),
                    "team": "Alpha" if i < 5 else "Bravo"} for i in range(10)]
        roster = Roster(fight_id="UI-DEMO", players=players, detected_at=dt.datetime.now().isoformat(), log_path="", log_timestamp="")
        self.start_scan(roster, replay=True, force=True)
        self.root.after(1200, self.self_test_step3)

    def self_test_step3(self):
        prep_dialog = self.open_prep_dialog()
        self.root.update_idletasks()
        self._test_check('prep_editor_openable', prep_dialog.winfo_ismapped())
        self._test_check('prep_ten_name_fields', len(self.prep_dialog.entries) == 10)
        prep_dialog.destroy()
        self._test_check('prep_button_visible', self.prep_button.winfo_ismapped())
        self._test_check("automatic_report_created", self.auto_report_path is not None and self.auto_report_path.is_file())
        self._test_check("report_button_enabled", str(self.report_button.cget('state')) == 'normal')
        report_dialog = self.open_report()
        self.root.update_idletasks()
        self._test_check("automatic_report_viewer_openable", report_dialog is not None and report_dialog.winfo_ismapped())
        if report_dialog is not None:
            report_dialog.destroy()
        self._test_check("report_actions_fit_window", all(
            child.winfo_ismapped() and child.winfo_width() >= child.winfo_reqwidth()
            for child in self.report_button.master.winfo_children()))
        self._test_check("enemy_table_is_default_view", self.view_mode == "data" and self.data_panel.winfo_ismapped())
        self.summary_button.invoke()
        self.root.update_idletasks()
        self._test_check("summary_view_available", self.summary_panel.winfo_ismapped())
        self._test_check("summary_catalog_or_missing_notice",
                         (self.catalog is not None and not self.catalog_note) or
                         (self.catalog is None and bool(self.catalog_note)))
        self._test_check("default_window_is_20s", self.opening_view_seconds == 20 and "前20秒" in self.summary_heading.get())
        self._test_check("both_windows_have_samples", len(self.opening20_states) == 5 and all(
            s["data"]["sample_count"] == 2 and s["data"]["window_seconds"] == 20 for s in self.opening20_states.values()))
        self._test_check("20s_counts_differ_from_120s", all(
            self.opening20_states[pid]["data"]["units"][0]["total_spawns"] == 2 and
            self.opening_states[pid]["data"]["units"][0]["total_spawns"] == 4 for pid in self.enemy_ids))
        self.window_primary_button.invoke()
        self._test_check("window_button_shows_120s", self.opening_view_seconds == 120 and "< 120秒" in self.summary_confidence.get())
        self.window20_button.invoke()
        self._test_check("window_button_restores_20s", self.opening_view_seconds == 20 and "< 20秒" in self.summary_confidence.get())
        self.copy_summary()
        copied = self.root.clipboard_get()
        self._test_check("copy_contains_both_windows", "【开局前20秒】" in copied and "【开局前120秒】" in copied)
        self._test_check("summary_has_no_tactical_keys", not any(key in self.summary for key in ("deck", "threats", "phases", "recommendation")))
        summary_dialog = self.summary_details()
        self.root.update_idletasks()
        self._test_check("full_data_summary_openable", summary_dialog.winfo_ismapped())
        summary_dialog.destroy()
        self.show_players("enemy")
        self.root.update_idletasks()
        self._test_check("player_data_view_preserved", self.data_panel.winfo_ismapped())
        self._test_check("ten_results_rendered", len(self.states) == 10 and not self.scanning)
        self._test_check("five_enemy_rows", len(self.table.get_children()) == 5)
        self._test_check("abbreviated_specializations", '空降+骑兵' in self.table.item(self.table.get_children()[0], 'values')[3])
        self._test_check("recent_unit_sample_rendered", len(self.recon_states) == 5 and all(s.get('status') == 'ok' for s in self.recon_states.values()))
        self._test_check("opening_sample_denominator", len(self.opening_states) == 5 and all((s.get('data') or {}).get('sample_count') == 2 for s in self.opening_states.values()))
        self._test_check("opening_frequency_rendered", "2/2" in self.table.item(self.table.get_children()[0], 'values')[4])
        self._test_check("opening_frequency_fits_column", all(self.row_font.measure(self.table.item(row, 'values')[4].split("\n")[-1]) <= int(self.table.column("recent", "width")) - 8 for row in self.table.get_children()))
        self.toggle_analysis()
        self._test_check("whole_match_toggle_preserved", "固定翼 2" in self.table.item(self.table.get_children()[0], 'values')[4])
        self.toggle_analysis()
        self.select_team("ally")
        self._test_check("own_team_switch", str(self.config["self_id"]) in self.table.get_children())
        self.select_team("enemy")
        details = self.player_details()
        self.root.update_idletasks()
        self._test_check("full_player_details_openable", details is not None and details.winfo_ismapped())
        if details is not None:
            details.destroy()
        exported = self.export_report()
        self._test_check("complete_txt_export_exists", exported is not None and exported.is_file())
        if exported:
            exported_text = exported.read_text(encoding="utf-8-sig")
            names = [unit_label(unit) for state in self.opening_states.values()
                     for unit in (state.get("data") or {}).get("units", [])]
            self._test_check("txt_preserves_full_unit_names", bool(names) and all(name in exported_text for name in names))
            self._test_check("txt_copied_as_file_attachment", bool(user32.IsClipboardFormatAvailable(15)))
            self._test_check("txt_has_no_advice", not any(token in exported_text for token in ("选卡组：", "开局先做", "优先威胁", "应对：", "带卡核对")))
            self._test_check("txt_contains_two_windows", "前20秒" in exported_text and "前120秒" in exported_text)
        original_id = self.config["self_id"]
        self.config["self_id"] = 1
        self.render_rows()
        self._test_check("unknown_self_id_no_enemy_claim", "Alpha" in self.enemy_button.cget("text") and len(self.table.get_children()) == 5)
        self.config["self_id"] = original_id
        self.render_rows()
        self.show_view("summary")
        exercise_hotkey(self.config["hotkey_toggle"])
        self.root.after(400, self.self_test_step4)

    def self_test_step4(self):
        self._test_check("collapse_hotkey", not self.expanded and self.root.winfo_height() <= 80)
        exercise_hotkey(self.config["hotkey_toggle"])
        self.root.after(400, self.self_test_finish)

    def self_test_finish(self):
        self._test_check("expand_hotkey", self.expanded and self.root.winfo_height() >= 500)
        self._test_check("recovery_shortcuts_visible", self.footer.winfo_ismapped() and self.footer.winfo_height() >= 24 and self.footer.winfo_y() + self.footer.winfo_height() <= self.root.winfo_height())
        self.status.set("模拟数据 · 界面自动测试完成（不代表真实玩家战绩）")
        self.compact.set("界面测试完成")
        output = {"passed": all(x["passed"] for x in self.selftest_results), "checks": self.selftest_results,
                  "hwnd": self.hwnd, "geometry": self.root.geometry(), "pid": __import__("os").getpid()}
        atomic_json(f_path(self.args.test_output), output)
        self.root.after(max(300, int(self.args.test_hold * 1000)), self.close)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.config.update(x=self.root.winfo_x(), y=self.root.winfo_y())
        if not self.args.self_test:
            try:
                atomic_json(ROOT / "config.json", self.config)
            except Exception:
                logging.exception("Could not save config")
        self.controller.close()
        self.recon.close()
        if self.prep_controller is not None:
            if self.prep_generation is not None:
                self.record_prep_event('cancelled', {'reason': 'close'})
            self.prep_controller.close()
        if self.screen_ocr is not None:
            self.screen_ocr.close()
        self.hotkeys.close()
        logging.info("Application stopped")
        self.root.destroy()
