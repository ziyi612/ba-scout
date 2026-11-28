"""A manual elapsed timer and one optional reminder for the Scout overlay."""

from dataclasses import dataclass
import re
import time
import tkinter as tk
from tkinter import ttk

from .dialog_style import configure_dialog
from .theme import AMBER, BG, DANGER, FG, FONT, MUTED


def parse_reminder(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    match = re.fullmatch(r"([0-9]{1,3})[:：]([0-5][0-9])", value)
    if match:
        seconds = int(match[1]) * 60 + int(match[2])
        if seconds:
            return seconds
    raise ValueError("请输入大于 0 的时间，例如 2:30。")


def format_elapsed(seconds: int) -> str:
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes:02}:{remainder:02}"


@dataclass
class MatchTimer:
    started_at: float | None = None
    reminder_seconds: int | None = None
    reminded: bool = False

    def tick(self, now: float) -> tuple[int, bool]:
        if self.started_at is None:
            return 0, False
        elapsed = max(0, int(now - self.started_at))
        due = self.reminder_seconds is not None and elapsed >= self.reminder_seconds and not self.reminded
        if due:
            self.reminded = True
        return elapsed, due


class MatchTimerWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.timer = MatchTimer()
        self.after_token = None
        self.title("对局计时 · 手动")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        configure_dialog(self)

        panel = ttk.Frame(self, style="BA.TFrame", padding=(14, 8, 14, 12))
        panel.pack(fill="both", expand=True)
        self.elapsed = tk.StringVar(self, value=format_elapsed(0))
        tk.Label(panel, textvariable=self.elapsed, bg=BG, fg=FG,
                 font=(FONT, 27, "bold")).pack(fill="x")
        controls = ttk.Frame(panel, style="BA.TFrame")
        controls.pack(pady=(3, 5))
        self.start_button = ttk.Button(controls, text="开始", style="Primary.BA.TButton", command=self.start)
        self.start_button.pack(side="left", padx=(0, 5))
        self.reset_button = ttk.Button(controls, text="重置", style="BA.TButton", command=self.reset, state="disabled")
        self.reset_button.pack(side="left")

        options = ttk.Frame(panel, style="BA.TFrame")
        options.pack(pady=(4, 0))
        ttk.Label(options, text="提醒时间（分:秒，可留空）", style="Muted.BA.TLabel").pack(side="left", padx=(0, 6))
        self.reminder_entry = ttk.Entry(options, width=7, style="BA.TEntry")
        self.reminder_entry.pack(side="left")
        self.status = tk.Label(panel, bg=BG, fg=MUTED, font=(FONT, 10), height=1)
        self.status.pack(fill="x", pady=(4, 0))

    def start(self):
        try:
            reminder = parse_reminder(self.reminder_entry.get())
        except ValueError as exc:
            self.status.configure(text=str(exc), fg=DANGER)
            self.reminder_entry.focus_set()
            return
        self.timer = MatchTimer(time.monotonic(), reminder)
        self.start_button.configure(state="disabled")
        self.reset_button.configure(state="normal")
        self.reminder_entry.configure(state="disabled")
        text = f"将在 {format_elapsed(reminder)} 提醒" if reminder is not None else ""
        self.status.configure(text=text, fg=MUTED)
        self.refresh_elapsed()

    def refresh_elapsed(self):
        elapsed, due = self.timer.tick(time.monotonic())
        self.elapsed.set(format_elapsed(elapsed))
        if due:
            self.status.configure(text=f"已到 {format_elapsed(self.timer.reminder_seconds)}", fg=AMBER)
            try:
                self.bell()
            except tk.TclError:
                pass
        self.after_token = self.after(100, self.refresh_elapsed)

    def cancel_refresh(self):
        if self.after_token is not None:
            self.after_cancel(self.after_token)
            self.after_token = None

    def reset(self):
        self.cancel_refresh()
        self.timer = MatchTimer()
        self.elapsed.set(format_elapsed(0))
        self.status.configure(text="", fg=MUTED)
        self.start_button.configure(state="normal")
        self.reset_button.configure(state="disabled")
        self.reminder_entry.configure(state="normal")

    def destroy(self):
        self.cancel_refresh()
        super().destroy()
