"""An identity-only profile is not a successful core-statistics result."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from ba_scout.engine import ScanController
from tests.test_engine import TEMP_ROOT, player_data, roster, wait_finished


class CoreReadyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='core-ready-',dir=TEMP_ROOT)
        self.path=Path(self.temp.name)
        self.controllers=[]

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
        self.temp.cleanup()

    def controller(self, fetcher):
        ctrl=ScanController(str(self.path),fetcher,ttl=1)
        self.controllers.append(ctrl)
        return ctrl

    def cache(self, *, ready, age=0):
        stamp=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
        data=player_data(6,core_ready=ready,fetched_at=stamp,rating=2222)
        raw={'schema':1,'player_id':6,'stored_at':stamp,'data':data}
        path=self.path/'6.json'
        path.write_text(json.dumps(raw),encoding='utf-8')
        return path.read_bytes()

    def test_identity_only_fresh_response_is_error_and_not_cached(self):
        ctrl=self.controller(lambda pid,timeout:player_data(pid,core_ready=False))
        ctrl.start(roster(ids=[6]),1)
        es=wait_finished(ctrl)
        event=next(e for e in es if e['type']=='player')
        self.assertEqual(event['status'],'error')
        self.assertIsNone(event['data'])
        self.assertIn('核心统计',event['message'])
        self.assertEqual(es[-1]['complete'],0)
        self.assertFalse((self.path/'6.json').exists())

    def test_incomplete_refresh_retains_existing_good_stale_cache(self):
        original=self.cache(ready=True,age=30)
        ctrl=self.controller(lambda pid,timeout:player_data(pid,core_ready=False))
        ctrl.start(roster(ids=[6]),1)
        es=wait_finished(ctrl)
        rows=[e for e in es if e['type']=='player']
        self.assertEqual([e['status'] for e in rows],['stale','error'])
        self.assertEqual(rows[-1]['data']['rating'],2222)
        self.assertTrue(rows[-1]['data']['from_cache'])
        self.assertEqual((self.path/'6.json').read_bytes(),original)

    def test_old_incomplete_cache_is_ignored_and_replaced_by_real_core(self):
        self.cache(ready=False)
        calls=[]
        ctrl=self.controller(lambda pid,timeout:(calls.append(pid),player_data(pid,core_ready=True))[1])
        ctrl.start(roster(ids=[6]),1)
        es=wait_finished(ctrl)
        self.assertEqual(calls,[6])
        rows=[e for e in es if e['type']=='player']
        self.assertEqual([e['status'] for e in rows],['ok'])
        self.assertTrue(json.loads((self.path/'6.json').read_text('utf-8'))['data']['core_ready'])

    def test_prefetch_incomplete_result_keeps_good_cache_and_stays_error(self):
        original=self.cache(ready=True,age=30)
        calls=[]
        ctrl=self.controller(lambda pid,timeout:(calls.append(pid),player_data(pid,core_ready=False))[1])
        token=ctrl.begin_prefetch('100',time.monotonic()+1)
        ctrl.prefetch(token,6)
        end=time.monotonic()+.5
        while time.monotonic()<end and ctrl._prefetch.records[6].phase!='completed':
            time.sleep(.002)
        self.assertEqual(ctrl._prefetch.records[6].phase,'completed')
        ctrl.start(roster(ids=[6]),1,prefetch_token=token)
        es=wait_finished(ctrl)
        rows=[e for e in es if e['type']=='player']
        self.assertEqual(calls,[6])
        self.assertEqual([e['status'] for e in rows],['stale','error'])
        self.assertTrue(rows[-1]['data']['core_ready'])
        self.assertTrue(rows[-1]['data']['from_cache'])
        self.assertEqual((self.path/'6.json').read_bytes(),original)
        self.assertEqual(es[-1]['complete'],0)

    def test_prefetch_does_not_use_incomplete_cache_as_success(self):
        self.cache(ready=False)
        calls=[]
        ctrl=self.controller(lambda pid,timeout:(calls.append(pid),player_data(pid,core_ready=False))[1])
        token=ctrl.begin_prefetch('100',time.monotonic()+1)
        ctrl.prefetch(token,6)
        ctrl.start(roster(ids=[6]),1,prefetch_token=token)
        es=wait_finished(ctrl)
        rows=[e for e in es if e['type']=='player']
        self.assertEqual(calls,[6])
        self.assertEqual([e['status'] for e in rows],['error'])
        self.assertIsNone(rows[-1]['data'])
        self.assertEqual(es[-1]['complete'],0)


if __name__=='__main__':
    unittest.main()
