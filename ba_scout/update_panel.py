"""Display update status and keep release I/O off the Tk event loop."""
from __future__ import annotations

import logging
from pathlib import Path
from queue import Empty, SimpleQueue
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

from . import __version__
from .dialog_style import configure_dialog
from .updater import UpdateError, check_update, install_supported, launch_install, stage_update


RELEASES_PAGE = "https://github.com/ziyi612/ba-scout/releases/latest"
AUTO_CHECK = "update_check_on_start"


class UpdatePanel:
    def __init__(self, parent, root_dir, config, is_match_active, on_restart, on_available):
        self.parent = parent
        self.root_dir = Path(root_dir)
        self.is_match_active = is_match_active
        self.on_restart = on_restart
        self.on_available = on_available
        self.config = config
        self.can_install = install_supported(self.root_dir)
        self.window = None
        self.release = None
        self.staged = None
        self.pending = None
        self.results = SimpleQueue()
        self.status = tk.StringVar(parent, value="尚未检查更新。")
        self.auto_check = tk.BooleanVar(parent, value=config.get(AUTO_CHECK, True) is not False)

    def open(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            return
        self.window = tk.Toplevel(self.parent)
        self.window.title("软件更新 · BA Scout")
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)
        configure_dialog(self.window)
        content = ttk.Frame(self.window, style="BA.TFrame", padding=16)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text=f"当前版本  {__version__}", style="Title.BA.TLabel").pack(anchor="w")
        ttk.Label(content, textvariable=self.status, style="BA.TLabel",
                  wraplength=393, justify="left").pack(fill="x", pady=(12, 8))
        ttk.Checkbutton(content, text="启动时自动检查", variable=self.auto_check,
                        command=self._save_preference).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(content, style="BA.TFrame")
        buttons.pack(fill="x")
        self.check_button = ttk.Button(buttons, text="检查更新", command=self.check, style="BA.TButton")
        self.check_button.pack(side="left")
        self.install_button = ttk.Button(buttons, command=self._advance, style="Primary.BA.TButton")
        ttk.Button(buttons, text="打开发布页", command=lambda: webbrowser.open(RELEASES_PAGE),
                   style="BA.TButton").pack(side="right")
        self._refresh_buttons()

    def _save_preference(self):
        self.config[AUTO_CHECK] = self.auto_check.get()

    def check(self, *, automatic=False):
        if self.pending or (automatic and not self.auto_check.get()):
            return
        self._start("check", "正在检查 GitHub Releases…", check_update, __version__)

    def _advance(self):
        if self.pending or not self.release or not self.can_install:
            return
        if self.staged is None:
            self._start("download", f"正在下载并校验 v{self.release.version}…",
                        stage_update, self.root_dir, self.release)
            return
        if self.is_match_active():
            self.status.set("更新已准备。请在离开准备房间或结束对局后安装。")
            return
        try:
            launch_install(self.root_dir, self.staged)
        except Exception:
            logging.exception("Could not start BA Scout update installer")
            self.status.set("无法启动更新安装程序。可重试或从发布页下载。")
            return
        self.on_restart()

    def _start(self, kind, message, function, *args):
        self.pending = kind
        self.status.set(message)
        self._refresh_buttons()

        def run():
            try:
                self.results.put((function(*args), None))
            except Exception as exc:
                logging.warning("BA Scout update %s error: %s", kind, exc)
                self.results.put((None, exc))

        threading.Thread(target=run, name=f"ba-scout-update-{kind}", daemon=True).start()
        self.parent.after(100, self._poll)

    def _poll(self):
        try:
            value, error = self.results.get_nowait()
        except Empty:
            self.parent.after(100, self._poll)
            return
        kind, self.pending = self.pending, None
        if error is not None:
            if isinstance(error, UpdateError):
                self.status.set(str(error))
            elif kind == "check":
                self.status.set("暂时无法检查更新。可以重试或查看发布页。")
            else:
                self.status.set("下载或校验失败，当前安装保持原样。可以重试。")
        elif kind == "check":
            if value != self.release:
                self.staged = None
            self.release = value
            self.on_available(value is not None)
            if value is None:
                self.status.set("未发现比当前版本更新的正式发布。")
            elif self.staged is not None:
                self.status.set(f"v{value.version} 已校验，可安装并重启。")
            elif self.can_install:
                self.status.set(f"发现新版本 v{value.version}。可先下载，在对局结束后安装。")
            else:
                self.status.set(f"发现新版本 v{value.version}。源码运行方式可从发布页获取便携版。")
        else:
            self.staged = value
            self.status.set(f"v{self.release.version} 已校验，可安装并重启。")
        self._refresh_buttons()

    def _refresh_buttons(self):
        if self.window is None or not self.window.winfo_exists():
            return
        self.check_button.configure(state="disabled" if self.pending else "normal")
        if self.can_install and self.release:
            self.install_button.configure(text="安装并重启" if self.staged else "下载更新",
                                          state="disabled" if self.pending else "normal")
            if not self.install_button.winfo_manager():
                self.install_button.pack(side="left", padx=8)
        else:
            self.install_button.pack_forget()
