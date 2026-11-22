"""Native player-card geometry, interaction and incremental updates."""
import gc
import tkinter as tk
import unittest
from tkinter import ttk
from types import SimpleNamespace

from ba_scout.player_cards import PlayerCards
from ba_scout.ui import ScoutWindow


def player(i, name=None, model="F-35B Lightning II"):
    return (str(i), (name or f"Player {i}", "2,105", "胜率 52.1%\n交换 1.23",
                      "美 空降+骑兵\n美 骑兵+装甲", model + " 3/5局\nAH-64D 2/5局", "本次查询"), "")


def slot(index, status):
    return (f"slot-{index}", (f"对手 {index+1}", "—", "—", "—", "—", status), "pending")


class PlayerCardsTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry("716x428+8000+8000")
        self.activated = []
        self.opened = []
        self.board = PlayerCards(self.root, self.activate, self.opened.append)
        self.board.pack(fill="x")
        self.addCleanup(self.close)

    def close(self):
        self.root.destroy()
        self.board = self.root = None
        gc.collect()

    def activate(self, iid):
        self.activated.append(iid)
        self.board.set_selected(iid)
        if self.board.detail_iid == iid:
            self.board.show_detail(None)
        else:
            self.board.show_detail(iid, "玩家详情", "前20秒 3/5局\n历史样本")

    def click(self, widget, timestamp):
        for pattern in ("<ButtonPress-1>", "<ButtonRelease-1>"):
            widget.event_generate(pattern, x=3, y=3, rootx=widget.winfo_rootx()+3,
                                  rooty=widget.winfo_rooty()+3, time=timestamp)
            self.root.update()

    def test_encounter_mark_shares_the_name_line_and_is_cleared_on_change(self):
        self.board.set_rows([player(12, "A very long name repeated twice for the layout check")])
        self.board.set_encounters({"12": "曾为对手 · 曾为队友"})
        self.root.update()
        card = self.board.cards["12"]
        self.assertTrue(card.encounter.winfo_ismapped())
        self.assertLessEqual(card.name.winfo_x() + card.name.winfo_width(), card.encounter.winfo_x())
        self.assertEqual(self.board.visible_rows()[0]["encounter"], "曾为对手 · 曾为队友")
        self.board.set_encounters({})
        self.root.update()
        self.assertFalse(card.encounter.winfo_ismapped())
        self.assertEqual(self.board.visible_rows()[0]["encounter"], "")

    def test_cards_arrive_without_replacing_or_reordering_an_existing_player(self):
        self.board.set_rows([player(12), slot(1, "补查中"), player(33)])
        self.root.update()
        first_card = self.board.cards["12"]
        placeholder = self.board.cards["slot-1"]
        self.assertFalse(placeholder.metrics.winfo_ismapped())
        self.assertFalse(placeholder.middle.winfo_ismapped())
        blank = self.board.visible_rows()[1]
        self.assertEqual((blank["rating"], blank["spec"], blank["recent"], blank["status"]),
                         ("", "", "", "补查中"))
        self.activate("12")
        self.board.set_rows([player(12, "Raven"), player(24), player(33)])
        self.root.update()
        self.assertIs(self.board.cards["12"], first_card)
        self.assertEqual(self.board.detail_iid, "12")
        self.assertFalse(placeholder.winfo_exists())
        shown = self.board.visible_rows()
        self.assertEqual([r["iid"] for r in shown], ["12", "24", "33"])
        self.assertEqual((shown[0]["name"], shown[0]["rating"], shown[0]["performance"]),
                         ("Raven", "2,105", "胜率 52.1%"))
        self.board.set_rows([])
        self.board.show_empty("等待名单")
        self.root.update()
        self.assertFalse(first_card.winfo_exists())
        self.assertFalse(self.board.detail_panel.winfo_ismapped())
        self.assertEqual(self.board.detail_body.cget("text"), "")
        self.assertTrue(self.board.empty.winfo_ismapped())

    def test_inline_drawer_follows_its_card_and_click_toggles_without_hiding_other_players(self):
        self.board.set_rows([player(i) for i in range(5)])
        self.root.update()
        self.click(self.board.cards["2"].score, 1000)
        panel = self.board.detail_panel
        self.assertEqual(self.activated, ["2"])
        self.assertTrue(panel.winfo_ismapped())
        self.assertLess(self.board.cards["2"].winfo_y(), panel.winfo_y())
        self.assertLess(panel.winfo_y(), self.board.cards["3"].winfo_y())
        for card in self.board.cards.values():
            self.assertTrue(card.winfo_ismapped())
            self.assertLessEqual(card.winfo_rooty()+card.winfo_height(),
                                 self.root.winfo_rooty()+self.root.winfo_height())
        self.click(self.board.cards["2"].score, 2000)
        self.assertFalse(panel.winfo_ismapped())
        self.assertEqual(self.activated, ["2", "2"])
        self.click(self.board.cards["3"].opening, 3200)
        self.click(self.board.cards["3"].opening, 3280)
        self.assertEqual(self.opened, ["3"])
        self.assertEqual(self.activated, ["2", "2", "3"])
        self.board.detail_button.invoke()
        self.assertEqual(self.opened, ["3", "3"])

    def test_keyboard_browses_players_and_opens_selected_card(self):
        self.board.set_rows([player(13), slot(1, "重名待确认"), player(21)])
        self.root.update()
        first = self.board.cards["13"]
        first.focus_force()
        self.root.update()
        first.event_generate("<Down>")
        self.root.update()
        pending = self.board.cards["slot-1"]
        self.assertIs(self.root.focus_get(), pending)
        pending.event_generate("<Return>")
        self.root.update()
        self.assertEqual(self.activated, ["slot-1"])
        self.assertFalse(self.board.detail_button.winfo_ismapped())
        pending.event_generate("<Up>")
        self.root.update()
        first.event_generate("<space>")
        self.root.update()
        self.assertIs(self.root.focus_get(), first)
        self.assertEqual(self.activated, ["slot-1", "13"])
        self.assertTrue(self.board.detail_button.winfo_ismapped())

    def test_long_fields_show_truncation_and_keep_the_observed_sample_size(self):
        original = player(1, "昵称" * 30, "Long display name " * 20)
        self.board.set_rows([original])
        self.board.set_opening_label("开局前120秒")
        self.root.update()
        card = self.board.cards["1"]
        visible = self.board.visible_rows()[0]
        self.assertTrue(visible["name"].endswith("…"))
        self.assertTrue(visible["recent"].splitlines()[0].endswith("… 3/5局"))
        for line in visible["recent"].splitlines():
            self.assertLessEqual(self.board.body_font.measure(line), card.opening.winfo_width())
        self.assertEqual(card.period.cget("text"), "开局前120秒")
        self.assertEqual(card.values, original[1])

    def test_two_line_values_and_expanded_title_fit_at_windows_display_scales(self):
        original_scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", original_scaling)
        self.board.set_rows([player(i) for i in range(5)])
        title = "长昵称玩家 " * 15 + " · 前20秒 · 有效样本 5/5 局 · 交换比 2.10 · 排位 200 场"
        self.board.show_detail("2", title, "AH-64D 3/5局")
        for scaling in (1.333, 1.667, 2.0):
            self.root.tk.call("tk", "scaling", scaling)
            self.root.update()
            with self.subTest(scaling=scaling):
                card = self.board.cards["2"]
                for field in (card.period, card.spec_caption, card.opening, card.specs):
                    self.assertGreaterEqual(field.winfo_height(), field.winfo_reqheight())
                heading, button = self.board.detail_title, self.board.detail_button
                self.assertLessEqual(heading.winfo_rootx()+heading.winfo_width(), button.winfo_rootx())
                self.assertEqual(heading.cget("text"), title)
                self.assertLessEqual(int(heading.cget("wraplength")), heading.winfo_width())
                self.assertGreater(heading.winfo_height(), self.board.body_font.metrics("linespace"))
                self.assertGreaterEqual(heading.winfo_height(), heading.winfo_reqheight())

    def test_overlay_scrolls_long_drawer_without_resizing_the_cards(self):
        self.board.destroy()
        self.root.geometry("732x366+8000+8000")
        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(2, minsize=14)
        self.root.rowconfigure(0, weight=1)
        window = ScoutWindow.__new__(ScoutWindow)
        window.view_mode = "cards"
        canvas = window.card_viewport = tk.Canvas(self.root, bd=0, highlightthickness=0,
                                                   yscrollincrement=1)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = window.card_scroll = ttk.Scrollbar(self.root, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        self.board = window.cards = PlayerCards(canvas, self.activate, self.opened.append)
        window.card_item = canvas.create_window(0, 0, anchor="nw", window=self.board)

        # Interrupt a geometry feedback loop so a regression fails the test
        # without holding the release process indefinitely.
        changes = []
        def layout(event):
            changes.append((event.widget, event.width, event.height))
            if len(changes) < 64:
                window.layout_cards(event)

        canvas.bind("<Configure>", layout)
        self.board.bind("<Configure>", layout)
        self.board.set_rows([player(i) for i in range(5)])
        self.root.update()
        width = self.board.winfo_width()
        self.assertEqual(width, canvas.winfo_width())
        self.assertFalse(scroll.winfo_ismapped())
        self.board.show_detail("4", "有效样本 2/5 局 · 排位 213 场", "F-16C 2/5局\n" * 6)
        self.root.update()
        self.assertTrue(scroll.winfo_ismapped())
        self.assertEqual(self.board.winfo_width(), width)
        self.assertGreater(self.board.winfo_reqheight(), canvas.winfo_height())
        wheel = SimpleNamespace(x_root=canvas.winfo_rootx()+10, y_root=canvas.winfo_rooty()+10, delta=-120*20)
        self.assertEqual(window.scroll_cards(wheel), "break")
        self.root.update()
        self.assertGreater(canvas.yview()[0], 0)
        self.assertEqual(canvas.yview()[1], 1)
        self.board.show_detail(None)
        self.root.update()
        self.assertFalse(scroll.winfo_ismapped())
        self.assertEqual(self.board.winfo_width(), width)
        self.assertEqual(canvas.yview(), (0.0, 1.0))
        self.assertLess(len(changes), 64)


if __name__ == "__main__":
    unittest.main()
