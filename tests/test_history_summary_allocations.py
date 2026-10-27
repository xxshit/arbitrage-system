"""Generated-data checks for bounded per-key allocation, without app startup."""
from array import array as real_array
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import opportunity_activity as activity
import opportunity_history as history
from spread_history import HistoryStore, encode_chunk

NOW = (1792800000 // 86400) * 86400 + 12*3600


class SummaryAllocationTests(unittest.TestCase):
    def test_hour_count_does_not_multiply_profile_array_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(directory)
            for back in (0, 1, 3, 7):
                midnight = NOW-12*3600-back*86400
                conn = store.connect(store.day(midnight), writable=True)
                try:
                    with conn:
                        for name in ('ONE', 'TWO'):
                            for metric in ('open', 'close'):
                                for hour in range(12):
                                    stamp = midnight+hour*3600
                                    v = .5 if metric == 'open' else .625
                                    payload = encode_chunk({str(stamp+m*60): [[[stamp+m*60, v]]] for m in range(60)})
                                    conn.execute('INSERT INTO history VALUES(?,?,?,?,?)',
                                                 (name+'/USDT', 'Gate', metric, stamp, payload))
                finally:
                    conn.close()
            initialized = []
            def counted(kind, values=()):
                if kind == 'd' and len(values) == 1 and math.isnan(values[0]):
                    initialized.append(1)
                return real_array(kind, values)
            cache = history.ProfileCache()
            with patch.object(history, 'array', counted):
                cache.refresh(store, NOW)
            result, at = cache.snapshot()
            self.assertEqual(at, NOW)
            self.assertEqual(len(result), 2)
            for profile in result.values():
                self.assertTrue(profile['ready'])
                self.assertTrue(profile['close_ready'])
                self.assertEqual(profile['median'], .5)
                self.assertEqual(profile['exit_q75'], .625)
                self.assertEqual(profile['windows']['15d']['range'], 0)
            # Two metrics for the active 24h plus one array per path/day.
            self.assertLessEqual(len(initialized), 2*2 + 2*4)
            print(json.dumps(dict(check='profile_array_initializations', actual=len(initialized), budget=12)))

    def test_episode_count_does_not_multiply_summary_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = activity.ActivityLedger(directory)
            ledger.restore()
            conn = ledger.connect()
            try:
                with conn:
                    for n in range(200):
                        at = NOW-2000+n*5
                        conn.execute('INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?)',
                                     ('ONE/USDT','Gate',at,1,1.25,at,at+1,1))
            finally:
                conn.close()
            ledger.states[('ONE/USDT', 'Gate')] = dict(first=NOW-2000, last=NOW, value=.2, active=None)
            constructions = []
            def counted(*args, **kwargs):
                if 'windows' in kwargs:
                    constructions.append(1)
                return dict(*args, **kwargs)
            with patch.object(activity, 'dict', counted, create=True):
                ledger.persist(NOW)
            profile = ledger.snapshot()[('ONE/USDT','Gate')]
            self.assertEqual(profile['count'], 200)
            self.assertEqual(profile['returned'], 200)
            self.assertEqual(profile['windows']['24h']['positive'], 200)
            self.assertEqual(len(profile['windows']['24h']['recent']), 5)
            self.assertEqual(profile['since'], NOW-2000)
            self.assertLessEqual(len(constructions), 1)
            print(json.dumps(dict(check='episode_summary_initializations', actual=len(constructions), budget=1)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
