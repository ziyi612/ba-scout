"""Five native, keyboard-accessible opponent cards for the preparation overlay."""
import re
import tkinter as tk
from tkinter import font as tkfont

from .theme import ACCENT, AMBER, BG, BORDER, DANGER, FG, FONT, MUTED, SURFACE


def _fit(line, font, width):
    """Shorten the name first so a unit's historical sample stays readable."""
    if font.measure(line) <= width:
        return line
    match = re.search(r"\s+\d+/\d+局(?:\*)?$", line)
    tail = " " + match.group().strip() if match else ""
    head = line[:match.start()] if match else line
    low, high = 0, len(head)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(head[:middle] + "…" + tail) <= width:
            low = middle
        else:
            high = middle - 1
    return head[:low].rstrip() + "…" + tail


def _status_colors(status):
    if any(word in status for word in ("冲突", "失败")):
        return DANGER, "#472d28"
    if any(word in status for word in ("重名", "超时", "限流", "补查", "部分", "旧缓存", "待核对")):
        return AMBER, "#3d3323"
    if status in ("本次查询", "缓存"):
        return "#b3d8be", "#273d31"
    return MUTED, "#28342e"


class _PlayerCard(tk.Frame):
    HEIGHT = 58

    def __init__(self, owner, iid):
        super().__init__(owner, height=self.HEIGHT, bg=BG, bd=0, highlightthickness=0, takefocus=True)
        self.owner = owner
        self.iid = iid
        self.values = ()
        self.selected = False
        self.expanded = False
        self.body_color = None

        self.outline = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.outline.place(x=0, y=0, relwidth=1, relheight=1)
        self.left = tk.Frame(self, bd=0)
        self.middle = tk.Frame(self, bd=0)
        self.right = tk.Frame(self, bd=0)
        self.left.place(relx=0, x=14, relwidth=.35, width=-22, y=4, relheight=1, height=-8)
        self.middle.place(relx=.35, x=4, relwidth=.26, width=-14, y=2, relheight=1, height=-4)
        self.right.place(relx=.61, x=5, relwidth=.39, width=-19, y=2, relheight=1, height=-4)

        def label(parent, font, color=FG, **options):
            return tk.Label(parent, font=font, fg=color, bd=0, padx=0, pady=0,
                            justify="left", anchor="w", takefocus=False, **options)

        self.name = label(self.left, owner.name_font)
        self.name.place(x=0, y=0, relwidth=1, height=24)
        self.encounter = label(self.left, (FONT, -11), ACCENT)
        self.pending_note = label(self.left, (FONT, -12), MUTED)
        self.metrics = tk.Frame(self.left, bd=0)
        self.metrics.place(x=0, y=25, relwidth=1, height=23)
        self.score_caption = label(self.metrics, (FONT, -11), MUTED, text="评分")
        self.score_caption.pack(side="left", padx=(0, 4))
        self.score = label(self.metrics, ("Segoe UI", -17, "bold"), ACCENT)
        self.score.pack(side="left")
        self.win = label(self.metrics, (FONT, -12), MUTED)
        self.win.pack(side="left", padx=(10, 0))

        self.spec_caption = label(self.middle, (FONT, -11), MUTED, text="历史双专精")
        self.spec_caption.place(x=0, y=0, relwidth=1, height=16)
        self.specs = label(self.middle, owner.body_font)
        self.specs.place(x=0, y=16, relwidth=1, height=38)
        self.period = label(self.right, (FONT, -11), MUTED, text=owner.opening_label)
        self.period.place(x=0, y=0, relwidth=.60, height=16)
        self.opening = label(self.right, owner.body_font)
        self.opening.place(x=0, y=16, relwidth=1, height=38)
        self.status = label(self.right, (FONT, -11), bg=SURFACE)
        self.status.configure(padx=5)
        self.status.place(relx=1, x=-14, y=0, anchor="ne", height=16)
        self.chevron = label(self.right, ("Segoe UI", -13, "bold"), MUTED, text="›")
        self.chevron.place(relx=1, y=0, anchor="ne", height=16, width=9)

        self.fill_widgets = (self.left, self.middle, self.right, self.name, self.encounter, self.pending_note, self.metrics,
                             self.score_caption, self.score, self.win, self.spec_caption,
                             self.specs, self.period, self.opening, self.chevron)
        for target in (self, self.outline, self.status, *self.fill_widgets):
            target.configure(cursor="hand2")
            target.bind("<ButtonPress-1>", self._activate)
            target.bind("<Double-Button-1>", self._details)
        for key, step in (("Up", -1), ("Down", 1)):
            self.bind(f"<{key}>", lambda event, n=step: owner.focus_neighbor(iid, n))
        for key in ("space", "Return"):
            self.bind(f"<{key}>", self._activate)
        self.bind("<Configure>", self._resized)
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)

    def _activate(self, event):
        self.focus_set()
        self.owner.on_activate(self.iid)
        return "break"

    def _details(self, event):
        self.focus_set()
        if not self.iid.startswith("slot-"):
            self.owner.on_details(self.iid)
        return "break"

    def _resized(self, event):
        self._draw()
        self._text()

    def _draw(self, event=None):
        waiting = self.iid.startswith("slot-")
        color = "#23342b" if self.selected else ("#141f19" if waiting else SURFACE)
        width, height = self.winfo_width(), self.winfo_height()
        if width > 5 and height > 5:
            canvas = self.outline
            canvas.delete("shape")
            x, y, r = width - 1, height - 1, 7
            points = (r,1, x-r,1, x,1, x,r, x,y-r, x,y, x-r,y,
                      r,y, 1,y, 1,y-r, 1,r, 1,1)
            edge = ACCENT if self.focus_get() is self else "#65755c" if self.selected else BORDER
            canvas.create_polygon(points, smooth=True, splinesteps=10, fill=color,
                                  outline=edge, width=1, tags="shape")
            if self.selected:
                canvas.create_line(3, 9, 3, y-8, fill=ACCENT, width=3,
                                   capstyle=tk.ROUND, tags="shape")
        if self.body_color != color:
            self.body_color = color
            for target in self.fill_widgets:
                target.configure(bg=color)

    def _text(self):
        if not self.values:
            return
        name, rating, performance, specs, recent, status = self.values
        width = self.winfo_width()
        if width <= 1:
            width = max(710, self.owner.winfo_width())
        name_width = max(24, int(width * .35) - 25)
        history = self.encounter.cget("text")
        if history:
            badge_width = self.encounter.winfo_reqwidth()
            name_width = max(12, name_width - badge_width - 6)
            self.encounter.place(relx=1, x=-1, y=2, anchor="ne", height=20)
        else:
            self.encounter.place_forget()
        self.name.place_configure(relwidth=0, width=name_width)
        self.name.configure(text=_fit(name, self.owner.name_font, name_width))
        pending = self.iid.startswith("slot-")
        self.name.configure(fg=MUTED if pending else FG)
        show_note = pending and performance != "—" and bool(performance)
        self.name.place_configure(y=12 if pending and not show_note else 0)
        if show_note:
            self.pending_note.configure(text=performance)
            self.pending_note.place(x=0, y=25, relwidth=1, height=19)
        else:
            self.pending_note.place_forget()
        self.score.configure(text="" if pending else rating)
        self.win.configure(text="" if pending else performance.split("\n", 1)[0].replace("胜率 —", "胜率待查"))
        if pending:
            self.metrics.place_forget()
            self.middle.place_forget()
            self.period.place_forget()
            self.status.place_configure(y=17)
            self.chevron.place_configure(y=17)
        else:
            self.metrics.place(x=0, y=25, relwidth=1, height=23)
            self.middle.place(relx=.35, x=4, relwidth=.26, width=-14, y=2, relheight=1, height=-4)
            self.period.place(x=0, y=0, relwidth=.60, height=16)
            self.status.place_configure(y=0)
            self.chevron.place_configure(y=0)
        spec_text = "" if pending else ("待取得" if specs == "—" else specs)
        self.specs.configure(text="\n".join(_fit(s, self.owner.body_font, max(24, int(width*.26)-17))
                                             for s in spec_text.splitlines()),
                             fg=MUTED if specs == "—" else FG)
        recent_text = "" if pending else recent
        self.opening.configure(text="\n".join(_fit(s, self.owner.body_font, max(30, int(width*.39)-22))
                                               for s in recent_text.splitlines()),
                               fg=MUTED if "等待" in recent or "未取得" in recent else FG)
        ink, fill = _status_colors(status)
        self.status.configure(text=status.replace("\n", " "), fg=ink, bg=fill)

    def update_values(self, values):
        if self.values != values:
            self.values = values
            self._text()

    def mark(self, selected, expanded):
        if self.selected != selected:
            self.selected = selected
            self._draw()
        if self.expanded != expanded:
            self.expanded = expanded
            self.chevron.configure(text="⌄" if expanded else "›", fg=ACCENT if expanded else MUTED)


class PlayerCards(tk.Frame):
    """Own the visible cards and one drawer placed immediately after its owner."""
    def __init__(self, parent, on_activate, on_details):
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0)
        self.on_activate = on_activate
        self.on_details = on_details
        self.cards = {}
        self.order = []
        self.selected = self.detail_iid = None
        self.opening_label = "前20秒出兵"
        self.columnconfigure(0, weight=1)
        self.name_font = tkfont.Font(root=self, family=FONT, size=-16, weight="bold")
        self.body_font = tkfont.Font(root=self, family=FONT, size=-13)
        self.empty = tk.Label(self, bg=SURFACE, fg=MUTED, font=(FONT, -15), pady=12)

        self.detail_panel = tk.Frame(self, bg="#1b2b23", bd=0)
        self.detail_panel.columnconfigure(1, weight=1)
        tk.Frame(self.detail_panel, bg=ACCENT, width=2).grid(row=0, column=0, rowspan=2, sticky="ns", padx=(4, 9))
        self.detail_title = tk.Label(self.detail_panel, bg="#1b2b23", fg=FG, bd=0, padx=0, pady=0,
                                     font=(FONT, -13, "bold"), anchor="w", justify="left", wraplength=580)
        self.detail_title.grid(row=0, column=1, sticky="ew", pady=(3, 0))
        self.detail_title.bind("<Configure>", self._wrap_drawer_title)
        self.detail_button = tk.Button(self.detail_panel, text="完整资料 ›", fg=ACCENT, bg="#23392d", bd=0,
                                      padx=7, pady=0, cursor="hand2", activeforeground=FG,
                                      activebackground=BORDER, font=(FONT, -11), relief="flat",
                                      command=lambda: self.on_details(self.detail_iid))
        self.detail_button.grid(row=0, column=2, sticky="e", padx=7, pady=(2, 0))
        self.detail_body = tk.Label(self.detail_panel, bg="#1b2b23", fg=MUTED, bd=0, padx=0, pady=0,
                                    font=(FONT, -12), anchor="w", justify="left", wraplength=690)
        self.detail_body.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 3), padx=(0, 6))
        self.detail_panel.bind("<Configure>", self._size_drawer)

    def _size_drawer(self, event):
        width = max(100, event.width-28)
        if int(self.detail_body.cget("wraplength")) != width:
            self.detail_body.configure(wraplength=width)

    def _wrap_drawer_title(self, event):
        width = max(1, event.width - 2)
        if int(self.detail_title.cget("wraplength")) != width:
            self.detail_title.configure(wraplength=width)

    def set_rows(self, items):
        order = [iid for iid, values, tag in items]
        for iid in self.cards.keys() - set(order):
            self.cards.pop(iid).destroy()
        if self.detail_iid is not None and self.detail_iid not in order:
            self.show_detail(None)
        if self.selected not in order:
            self.selected = None
        changed_order = order != self.order
        self.order = order
        for index, (iid, values, _tag) in enumerate(items):
            if iid not in self.cards:
                self.cards[iid] = _PlayerCard(self, iid)
            card = self.cards[iid]
            if changed_order:
                card.grid(row=index*2, column=0, sticky="ew", pady=(4 if index else 0, 0))
            card.update_values(tuple(values))
        if changed_order and self.detail_iid in self.cards:
            self._place_drawer()
        if order:
            self.empty.grid_remove()

    def set_encounters(self, labels):
        for iid, card in self.cards.items():
            label = labels.get(iid, "")
            if card.encounter.cget("text") != label:
                card.encounter.configure(text=label)
                card._text()

    def set_selected(self, iid):
        self.selected = iid if iid in self.cards else None
        for key, card in self.cards.items():
            card.mark(key == self.selected, key == self.detail_iid)

    def set_opening_label(self, text):
        if text != self.opening_label:
            self.opening_label = text
            for card in self.cards.values():
                card.period.configure(text=text)

    def show_empty(self, message):
        if message and not self.order:
            self.empty.configure(text=message)
            self.empty.grid(row=0, column=0, sticky="ew")
        else:
            self.empty.grid_remove()

    def _place_drawer(self):
        self.detail_panel.grid(row=self.order.index(self.detail_iid)*2+1, column=0, sticky="ew", padx=(4, 0))

    def show_detail(self, iid, title="", body=""):
        self.detail_iid = iid if iid in self.cards else None
        if self.detail_iid is None:
            self.detail_panel.grid_remove()
            self.detail_title.configure(text="")
            self.detail_body.configure(text="")
        else:
            self.detail_title.configure(text=title)
            self.detail_body.configure(text=body)
            if iid.startswith("slot-"):
                self.detail_button.grid_remove()
            else:
                self.detail_button.grid()
            self._place_drawer()
        self.set_selected(self.selected)

    def focus_neighbor(self, iid, offset):
        if iid in self.cards:
            index = min(len(self.order)-1, max(0, self.order.index(iid)+offset))
            self.cards[self.order[index]].focus_set()
        return "break"

    def visible_rows(self):
        """Read what each card actually displays, including shortened model names."""
        result = []
        for iid in self.order:
            c = self.cards[iid]
            result.append(dict(iid=iid, name=c.name.cget("text"), rating=c.score.cget("text"),
                               performance=c.win.cget("text"), spec=c.specs.cget("text"),
                               recent=c.opening.cget("text"), status=c.status.cget("text"),
                               encounter=c.encounter.cget("text")))
        return result
