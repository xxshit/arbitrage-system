"""A later market tick may replace both publications while history I/O is pending."""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref


class Publication(dict):
    pass


class Idle(BaseException):
    pass


class BackfillLifetimeTests(unittest.TestCase):
    def check_replaced(self, fail):
        refs, cache, progress, errors, removals = [], {}, [], [], []
        for name, symbols in (('spot', ['S', 'S', 'A']), ('dual', ['S', 'B', 'B', 'C', 'RWA', 'D'])):
            groups = [Publication(symbol=s, rows=[{'long_exchange': 'Binance' if s == 'B' else 'Other',
                                                    'short_exchange': 'Binance' if s in ('D', 'RWA') else 'Other'}])
                      for s in symbols]
            cache[name] = Publication(symbols=groups)
            refs += [weakref.ref(cache[name]), *(weakref.ref(group) for group in groups)]
            del groups
        def backfill(groups):
            self.assertEqual(removals, [True])
            self.assertEqual([item['symbol'] for item in groups], ['S', 'S', 'A', 'B', 'D'])
            cache.clear()  # Both later generations published by the five-second quote workers.
            self.assertTrue(all(ref() is None for ref in refs), 'backfill retained an obsolete publication during I/O')
            if fail:
                raise ValueError('synthetic network failure')
        def sleep(seconds):
            if seconds == 60 and cache:
                return
            self.assertEqual(seconds, 120)
            self.assertTrue(all(ref() is None for ref in refs), 'sleeping worker retained old quotes')
            raise Idle()
        env = dict(app=SimpleNamespace(app_context=nullcontext), time=SimpleNamespace(sleep=sleep),
                   db=SimpleNamespace(session=SimpleNamespace(remove=lambda: removals.append(True))),
                   PRICE_BACKFILL_SYNC_SECONDS=120, runtime_interval=lambda key, default: default,
                   report_background_error=lambda key, error: errors.append((key, type(error))),
                   BACKGROUND_RUNTIME=SimpleNamespace(progress=progress.append), is_rwa_stock_pair=lambda s: s == 'RWA',
                   load_latest_market_snapshot=lambda: cache['spot'], load_latest_dual_futures_snapshot=lambda: cache['dual'],
                   backfill_price_history=backfill)
        path = Path(__file__).resolve().parents[1] / 'app.py'
        nodes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)
                 and n.name in ('price_backfill_universe', 'background_price_history_backfill')]
        exec(compile(ast.Module(nodes, []), str(path), 'exec'), env)
        with self.assertRaises(Idle):
            env['background_price_history_backfill']()
        self.assertEqual(progress, [] if fail else ['success'])
        self.assertEqual(errors, [('price_history_backfill', ValueError)] if fail else [])

    def test_replaced_snapshots_released_before_network_and_sleep(self):
        self.check_replaced(False)

    def test_failed_backfill_preserves_release_and_retry(self):
        self.check_replaced(True)
