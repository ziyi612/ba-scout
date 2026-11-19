"""Native five-row table with an emphasized score and a compact status label."""
import tkinter as tk
from tkinter import ttk

from .theme import ACCENT, AMBER, DANGER, FONT, MUTED, ROW_ALT, SELECTED, SURFACE


class RosterTable(ttk.Treeview):
    """Keep the native table's geometry, focus, keyboard and selection behavior."""
    def __init__(self, parent, **options):
        super().__init__(parent, **options)
        self._labels = {}
        self._paint_job = None
        self._empty = tk.Label(self, text="", bg=SURFACE, fg=MUTED, font=(FONT, -15), bd=0)
        for event in ("<Configure>", "<Map>", "<B1-Motion>", "<<TreeviewSelect>>"):
            self.bind(event, self.schedule_paint, add="+")

    def schedule_paint(self, event=None):
        if self._paint_job is None:
            self._paint_job = self.after_idle(self._paint_cells)

    def _select_cell(self, iid, open_details=False):
        self.focus_set()
        self.focus(iid)
        self.selection_set(iid)
        if open_details:
            self.event_generate("<<RosterActivate>>")
        return "break"

    def show_empty(self, message):
        if message:
            self._empty.configure(text=message)
            self._empty.place(relx=.5, rely=.55, anchor="center")
        else:
            self._empty.place_forget()

    def _label(self, iid, column):
        key = iid, column
        if key not in self._labels:
            label = tk.Label(self, bd=0, padx=0, pady=0, highlightthickness=0, takefocus=False)
            targets = [label]
            if column == "status":
                badge = tk.Label(label, name="badge", font=(FONT, -12), relief="flat",
                                 padx=4, pady=1, bd=0, highlightthickness=0, takefocus=False)
                badge.place(x=3, rely=0.5, anchor="w")
                targets.append(badge)
            # A badge is a child window. Recognize its double click on that
            # window; replaying two single presses on the tree loses the gesture.
            for target in targets:
                target.bind("<ButtonPress-1>", lambda event: self._select_cell(iid))
                target.bind("<Double-Button-1>", lambda event: self._select_cell(iid, open_details=True))
                target.bind("<MouseWheel>", lambda e: self.event_generate("<MouseWheel>", delta=e.delta))
            self._labels[key] = label
        return self._labels[key]

    def _paint_cells(self):
        self._paint_job = None
        children = self.get_children()
        selected = self.selection()
        for key in tuple(self._labels):
            if key[0] not in children:
                self._labels.pop(key).destroy()
        for index, iid in enumerate(children):
            values = self.item(iid, "values")
            if len(values) != 6:
                continue
            row_bg = SELECTED if iid in selected else (ROW_ALT if index % 2 else SURFACE)
            for column in ("rating", "status"):
                bounds = self.bbox(iid, column)
                label = self._label(iid, column)
                if not bounds:
                    label.place_forget()
                    continue
                x, y, w, h = bounds
                if column == "rating":
                    score = values[1]
                    label.configure(text=score, font=("Segoe UI", -17, "bold"),
                                    fg=ACCENT if score != "—" else MUTED, bg=row_bg, anchor="w")
                    label.configure(padx=3)
                    label.place(x=x, y=y, width=w, height=h)
                    continue
                status = values[5]
                ink, fill = "#c2dec3", "#284133"
                if "失败" in status or "冲突" in status:
                    ink, fill = DANGER, "#4a302a"
                elif any(word in status for word in ("补查", "超时", "重名", "部分", "旧", "限流", "待核对")):
                    ink, fill = AMBER, "#423927"
                elif status not in ("本次查询", "缓存"):
                    ink, fill = MUTED, "#28352d"
                label.configure(bg=row_bg)
                label.place(x=x, y=y, width=w, height=h)
                label.children["badge"].configure(text=status, fg=ink, bg=fill)

    def destroy(self):
        if self._paint_job is not None:
            self.after_cancel(self._paint_job)
        super().destroy()
