from types import SimpleNamespace
import unittest
from ba_scout.ui import ScoutWindow


class StaleHistoryTests(unittest.TestCase):
    def make_window(self):
        pending = []
        submitted = []
        def poll():
            events = pending[:]
            pending.clear()
            return events
        window = SimpleNamespace(
            closed=False,hotkeys=SimpleNamespace(poll=lambda:[]),controller=SimpleNamespace(poll_events=poll),
            recon=SimpleNamespace(submit_history=lambda player,history:submitted.append((player,history)),poll_events=lambda:[]),
            scan_id=7,recon_scan_id=9,states={},recon_states={},opening_states={},enemy_ids=[42],stale_history_ids=set(),
            scanning=False,core_elapsed=None,started=None,render_rows=lambda:None,update_summary=lambda:None,maybe_auto_report=lambda:None,
            root=SimpleNamespace(after=lambda *args:None),tick=lambda:None)
        return window,pending,submitted

    def event(self,status='stale',player=42,scan=7,history=None):
        return dict(type='player',scan_id=scan,player_id=player,status=status,
                    data={'recent_match_ids':history or ['123','124']})

    def test_stale_history_starts_immediately_and_is_marked(self):
        window,pending,submitted=self.make_window()
        pending.append(self.event())
        ScoutWindow.tick(window)
        self.assertEqual(submitted,[(42,['123','124'])])
        self.assertEqual(window.stale_history_ids,{42})

    def test_fresh_history_replaces_old_list_and_removes_warning(self):
        window,pending,submitted=self.make_window()
        pending.append(self.event())
        ScoutWindow.tick(window)
        pending.append(self.event(status='ok',history=['125']))
        ScoutWindow.tick(window)
        self.assertEqual(submitted[-1],(42,['125']))
        self.assertEqual(window.stale_history_ids,set())

    def test_other_scan_and_ally_cannot_start_enemy_history(self):
        window,pending,submitted=self.make_window()
        pending.extend([self.event(scan=6),self.event(player=43)])
        ScoutWindow.tick(window)
        self.assertEqual(submitted,[])
        self.assertEqual(window.stale_history_ids,set())


if __name__=='__main__':
    unittest.main()
