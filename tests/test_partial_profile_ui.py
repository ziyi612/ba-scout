"""Keep genuinely available basic data/history without claiming full statistics."""
from copy import deepcopy
import unittest

from ba_scout.ui import ScoutWindow
from tests import test_advisor_stale_history
from tests.test_reporting import snapshot, render, STAMP


class PartialProfileHistoryTests(unittest.TestCase):
    def make_window(self):
        return test_advisor_stale_history.StaleHistoryTests().make_window()

    def test_partial_profile_starts_history_without_core_stats(self):
        window, pending, submitted = self.make_window()
        pending.append(dict(type='player', scan_id=7, player_id=42, status='partial',
                            data={'player_id': 42, 'core_ready': False,
                                  'recent_match_ids': ['987']}))
        ScoutWindow.tick(window)
        self.assertEqual(submitted, [(42, ['987'])])
        self.assertEqual(window.states[42]['status'], 'partial')
        self.assertFalse(window.states[42]['data']['core_ready'])

    def test_new_partial_history_replaces_stale_list_without_changing_old_statistics(self):
        window, pending, submitted = self.make_window()
        old = {'player_id': 42, 'core_ready': True, 'wins': 12,
               'from_cache': True, 'fetched_at': STAMP, 'recent_match_ids': ['123']}
        fresh = {'player_id': 42, 'core_ready': False, 'profile_incomplete': True,
                 'from_cache': False, 'recent_match_ids': ['987']}
        before = deepcopy(old)
        window.stale_history_ids.add(42)
        pending.append(dict(type='player', scan_id=7, player_id=42, status='partial',
                            data=old, partial_data=fresh))
        ScoutWindow.tick(window)
        self.assertEqual(submitted, [(42, ['987'])])
        self.assertEqual(window.stale_history_ids, set())
        self.assertEqual(window.states[42]['data'], before)

    def test_partial_for_another_scan_or_ally_does_not_start_history(self):
        window, pending, submitted = self.make_window()
        for scan, player in [(6, 42), (7, 43)]:
            pending.append(dict(type='player', scan_id=scan, player_id=player,
                                status='partial', data={'player_id': player,
                                'recent_match_ids': ['987']}))
        ScoutWindow.tick(window)
        self.assertEqual(submitted, [])


class PartialProfileReportTests(unittest.TestCase):
    def test_missing_statistics_are_not_zero_or_success(self):
        args = list(snapshot())
        args[1] = {7: {'status': 'partial', 'data': {
            'player_id': 7, 'core_ready': False, 'profile_incomplete': True,
            'rating': 2193, 'rank': 10245, 'fetched_at': STAMP,
            'source_url': 'https://dash.batrace.top/player/7'}}}
        report = render(args)
        self.assertIn('本次累计战绩与专精统计暂缺', report)
        self.assertIn('评分 2,193.0；排名 10,245', report)
        self.assertIn('胜率 未取得', report)
        self.assertNotIn('胜率 0.0%', report)
        self.assertNotIn('核心数据：本次查询成功', report)

    def test_old_stats_and_fresh_basic_data_keep_separate_times(self):
        args = list(snapshot())
        state = args[1][7]
        state['status'] = 'partial'
        state['data']['from_cache'] = True
        state['partial_data'] = {'player_id': 7, 'rating': 2193, 'rank': 10245,
                                 'fetched_at': '2026-10-29T01:00:00Z',
                                 'source_url': 'https://dash.batrace.top/player/7'}
        report = render(args)
        self.assertIn('以下沿用旧缓存', report)
        self.assertIn('本次基础资料：评分 2,193.0；排名 10,245', report)
        self.assertIn('本次基础资料抓取时间：2026-10-29T01:00:00Z', report)
        self.assertIn('抓取时间：' + STAMP, report)
        self.assertIn('胜率 60.0%', report)

    def test_wrong_identity_partial_basic_data_is_not_exported(self):
        args = list(snapshot())
        state = args[1][7]
        state.update(status='partial', partial_data={'player_id': 8, 'rating': 9999})
        state['data']['from_cache'] = True
        self.assertNotIn('9,999', render(args))


if __name__ == '__main__':
    unittest.main()
