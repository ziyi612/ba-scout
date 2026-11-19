"""Native styles for BA Scout's forms and read-only detail windows."""
import tkinter as tk
from tkinter import ttk

from .theme import ACCENT, BG, BORDER, FG, FONT as FONT_FAMILY, MUTED, SURFACE


FONT = (FONT_FAMILY, 10)


def configure_dialog(window):
    """Use named styles so opening a form does not restyle the overlay."""
    window.configure(background=BG)
    style = ttk.Style(window)
    if style.theme_use() != "clam":
        style.theme_use("clam")
    style.configure("BA.TFrame", background=BG)
    style.configure("BA.TLabel", background=BG, foreground=FG, font=FONT)
    style.configure("Muted.BA.TLabel", foreground=MUTED)
    style.configure("Title.BA.TLabel", foreground=FG, font=(FONT[0], 13, "bold"))
    style.configure("BA.TButton", background=SURFACE, foreground=FG, font=FONT,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                    focuscolor=ACCENT, relief="flat", padding=(10, 5))
    style.map("BA.TButton", background=[("pressed", BG), ("active", BORDER)],
              foreground=[("disabled", MUTED)])
    style.configure("Primary.BA.TButton", background=ACCENT, foreground=BG,
                    lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT, focuscolor=BG)
    style.map("Primary.BA.TButton", background=[("pressed", MUTED), ("active", FG)])
    for name in ("BA.TEntry", "BA.TCombobox"):
        style.configure(name, foreground=FG, fieldbackground=SURFACE, background=SURFACE,
                        insertcolor=FG, bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                        arrowcolor=MUTED, selectbackground=BORDER, selectforeground=FG, padding=(6, 4))
        style.map(name, bordercolor=[("focus", ACCENT)],
                  fieldbackground=[("readonly", SURFACE)], foreground=[("readonly", FG)])
    for option, value in (("background", SURFACE), ("foreground", FG),
                          ("selectBackground", BORDER), ("selectForeground", FG), ("font", FONT)):
        window.option_add("*TCombobox*Listbox." + option, value)
    style.configure("BA.TNotebook", background=BG, bordercolor=BORDER, tabmargins=(0, 4, 0, 1))
    style.configure("BA.TNotebook.Tab", background=BG, foreground=MUTED, font=FONT,
                    lightcolor=BG, darkcolor=BG, bordercolor=BORDER, padding=(14, 7))
    style.map("BA.TNotebook.Tab", foreground=[("selected", ACCENT)], background=[("selected", SURFACE)])
    style.configure("BA.Vertical.TScrollbar", troughcolor=BG, background=BORDER,
                    bordercolor=BG, lightcolor=BORDER, darkcolor=BORDER, arrowcolor=MUTED,
                    arrowsize=12)
    style.map("BA.Vertical.TScrollbar", background=[("active", MUTED)])


def reader(window):
    """Create a native text pane with selectable text and a linked scrollbar."""
    pane = tk.Frame(window, bg=BORDER, padx=1, pady=1)
    pane.pack(fill="both", expand=True, padx=14, pady=(14, 8))
    scroll = ttk.Scrollbar(pane, orient="vertical", style="BA.Vertical.TScrollbar")
    scroll.pack(side="right", fill="y")
    text = tk.Text(pane, bg=SURFACE, fg=FG, insertbackground=FG, font=FONT, wrap="word",
                   selectforeground=FG, selectbackground=BORDER, borderwidth=0,
                   highlightthickness=0, padx=16, pady=12, spacing3=2, yscrollcommand=scroll.set)
    text.pack(fill="both", expand=True)
    scroll.configure(command=text.yview)
    return text


def fill_reader(text, content):
    text.insert("1.0", content)
    text.tag_configure("heading", foreground=ACCENT, font=(FONT[0], 12, "bold"), spacing3=5)
    text.tag_add("heading", "1.0", "1.end")
    text.configure(state="disabled")
