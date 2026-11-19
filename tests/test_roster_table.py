"""Check that the visible presentation of the native roster stays interactive."""
import gc
import tkinter as tk
import unittest

from ba_scout.roster_table import RosterTable


class RosterTableTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.addCleanup(self.close_widgets)
        self.root.overrideredirect(True)
        # The widget needs a window to measure rows. Keep it off the desktop.
        self.root.geometry('650x180+8000+8000')
        columns = ('name', 'rating', 'performance', 'spec', 'recent', 'status')
        self.table = RosterTable(self.root, columns=columns, show='headings', selectmode='browse')
        for column in columns:
            self.table.column(column, width=100, minwidth=50, anchor='w')
        self.table.pack(fill='both', expand=True)
        self.table.insert('', 'end', iid='one', values=('Raven', '2,100', '50%', '骑兵', '1/5', '本次查询'))
        self.table.insert('', 'end', iid='two', values=('Orbit', '—', '—', '—', '—', '补查中'))
        self.table.selection_set('one')
        self.paint()

    def close_widgets(self):
        self.root.destroy()
        self.table = self.root = None
        # Tk's last reference must disappear on the test's UI thread before a
        # later test allocates objects on an OCR or HTTP worker thread.
        gc.collect()

    def paint(self):
        self.table.schedule_paint()
        self.root.update()

    def badge(self, iid):
        return self.table._labels[iid, 'status'].children['badge']

    def click(self, target, timestamp):
        x, y = 3, target.winfo_height() // 2
        for event in ('<ButtonPress-1>', '<ButtonRelease-1>'):
            target.event_generate(event, x=x, y=y, time=timestamp, rootx=target.winfo_rootx() + x,
                                  rooty=target.winfo_rooty() + y)
            self.root.update()

    def test_visible_rating_and_status_reflect_late_profile(self):
        rating = self.table._labels['two', 'rating']
        self.assertTrue(rating.winfo_ismapped())
        self.assertTrue(self.badge('two').winfo_ismapped())
        self.assertEqual(rating.cget('text'), '—')
        self.assertEqual(self.badge('two').cget('text'), '补查中')
        self.table.item('two', values=('Orbit', '2,305', '55%', '空降', '2/5', '本次查询'))
        self.paint()
        cells = self.table.item('two', 'values')
        self.assertEqual(rating.cget('text'), cells[1])
        self.assertEqual(self.badge('two').cget('text'), cells[5])

    def test_single_and_double_click_on_formatted_cells_reach_native_tree(self):
        opened = []
        def open_details(event):
            opened.append(self.table.selection())
        self.table.bind('<Double-1>', open_details)
        self.table.bind('<<RosterActivate>>', open_details)
        self.click(self.table._labels['two', 'rating'], 1000)
        self.assertEqual(self.table.selection(), ('two',))
        self.click(self.badge('one'), 2000)
        self.assertEqual(self.table.selection(), ('one',))
        self.click(self.badge('one'), 2075)
        self.assertEqual(opened, [('one',)])
        self.click(self.table._labels['two', 'rating'], 3000)
        self.click(self.table._labels['two', 'rating'], 3075)
        self.assertEqual(opened, [('one',), ('two',)])

    def test_clearing_roster_removes_painted_values_before_reusing_a_slot(self):
        old_score = self.table._labels['two', 'rating']
        old_badge = self.badge('two')
        self.table.delete(*self.table.get_children())
        self.paint()
        self.assertFalse(old_score.winfo_exists())
        self.assertFalse(old_badge.winfo_exists())
        self.assertFalse(self.table._labels)
        self.table.insert('', 'end', iid='two', values=('New', '—', '—', '—', '—', '等名字'))
        self.paint()
        self.assertEqual(self.table._labels['two', 'rating'].cget('text'), '—')
        self.assertEqual(self.badge('two').cget('text'), '等名字')
