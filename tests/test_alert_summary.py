"""Exact scalar and ownership checks, without importing app or running workers."""
import ast
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref

from alert_summary import AlertSummary
from anomaly_engine import alert_round_ids


class Event(SimpleNamespace):
    pass


def item(identity, spread=.5, basis=-.2):
    return Event(id=identity, symbol='TEST/USDT', strategy='spot_futures',
                 open_spread=spread, basis=basis, long_exchange='Gate', short_exchange='Binance',
                 created_at=datetime(2026,11,12)-timedelta(seconds=identity))


class AlertSummaryTests(unittest.TestCase):
    def test_descending_round_iterator_matches_unsorted_with_time_and_id_ties(self):
        rows = [item(i) for i in range(1,120)]
        for row in rows:
            row.created_at = datetime(2026,11,12)-timedelta(seconds=row.id//3*5)
            row.long_exchange = 'Gate' if row.id%2 else 'Bitget'
        pulses = {row.id:dict(id=f'p{row.id}', episode=f'e{row.id//11}', metric='spread')
                  for row in rows if row.id%7}
        expected = alert_round_ids(rows, pulses)
        descending = sorted(rows, key=lambda row:(row.created_at,row.id), reverse=True)
        self.assertEqual(alert_round_ids(descending,pulses,newest_first=True), expected)

    def test_selection_keeps_invisible_round_anchors_in_correlation(self):
        rows = [item(i) for i in range(1,120)]
        pulses = {row.id:dict(id=f'p{row.id}', episode=f'e{row.id//5}', metric='spread')
                  for row in rows if row.id%7}
        full = alert_round_ids(rows,pulses)
        selected = {1,17,28,39,74,119}
        actual = alert_round_ids(rows,pulses,newest_first=True,
                                 include=lambda row,pulse:row.id in selected)
        self.assertEqual(actual,{identity:full[identity] for identity in selected})
        self.assertTrue(any(round_id not in {f'pulse:{i}' for i in selected}
                            for round_id in actual.values()))

    def test_observation_ties_precede_pulse_bounds_and_signed_zero(self):
        acc = AlertSummary()
        acc.add(item(1, 0.0, None), SimpleNamespace(has_reminder=False, bounds=(-0.0, -0.0, -99, 99)),
                'one', 0, (0, 1), None, False)
        acc.add(item(2, -0.0, 2), None, 'two', 0, (0, 1), 2, True)
        ranges = acc.value_ranges()
        self.assertEqual(math.copysign(1, ranges['open_spread']['min']), 1)
        self.assertEqual(math.copysign(1, ranges['open_spread']['max']), 1)
        self.assertEqual(ranges['basis'], {'min':2,'max':2})
        self.assertEqual(acc.best.id, 1)
        self.assertEqual(acc.first.id, 2)
        self.assertEqual(acc.max_spread.id, 1)

    def test_nonfinite_values_do_not_enter_ranges_but_keep_legacy_peak_time(self):
        acc = AlertSummary()
        acc.add(item(1, math.nan, math.inf), None, 'one', 0, (0,), math.inf, True)
        acc.add(item(2, 3, -2), SimpleNamespace(has_reminder=False, bounds=(-4, 5, -6, 7)),
                'two', 0, (1,), -2, True)
        self.assertEqual(acc.value_ranges(), {'open_spread':{'min':-4,'max':5},'basis':{'min':-6,'max':7}})
        self.assertEqual(acc.max_spread.id, 1)  # same first-NaN max semantics
        self.assertEqual(acc.max_basis.id, 1)
        self.assertEqual(acc.best.id, 2)

    def test_modern_tickets_and_legacy_rounds_have_independent_counts(self):
        acc = AlertSummary()
        pulse = SimpleNamespace(has_reminder=True, count=lambda now: 3 if now==99 else 0,
                                bounds=(None,None,None,None))
        for identity, value in enumerate((None,None,pulse,pulse),1):
            acc.add(item(identity), value, 'same-round', 99, (0,), -.2, True)
        self.assertEqual(acc.count, 4)
        self.assertEqual(acc.alert_count, 7)

    def test_accumulator_does_not_hold_all_events_or_pulses(self):
        acc = AlertSummary()
        refs = []
        for identity in range(1000):
            event = item(identity)
            refs.append(weakref.ref(event))
            acc.add(event, None, f'round:{identity}', 99, (0,), -.2, True)
        del event
        self.assertEqual(sum(ref() is not None for ref in refs), 2)  # latest/best/peaks + first
        result = acc.value_ranges()
        del acc
        self.assertTrue(all(ref() is None for ref in refs))
        self.assertEqual(result['basis']['min'], -.2)

    def test_route_group_scope_releases_accumulators_before_returned_payload(self):
        source = Path(__file__).resolve().parents[1]/'app.py'
        route = next(n for n in ast.parse(source.read_text()).body if getattr(n,'name','')=='alerts')
        node = next(n for n in route.body if getattr(n,'name','')=='alert_group_payload')
        references = []
        class Summary(AlertSummary):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                references.append(weakref.ref(self))
        class Attention:
            def __getitem__(self, identity):
                return {'tier':'record', 'live':False}
        context = lambda row: dict(strategy=row.strategy,long_exchange=row.long_exchange,short_exchange=row.short_exchange)
        ns = dict(AlertSummary=Summary, view='summary', pulses={}, screening_now=99, attention=Attention(),
                  event_payload=lambda row, rounds: dict(id=row.id, round_id=rounds[row.id]),
                  path_id=lambda key:json.dumps(key), path_key=lambda row:[row.strategy,row.long_exchange,row.short_exchange],
                  priority=lambda row:(row.id%2,0,-row.id), alert_binance_basis=lambda row:row.basis,
                  alert_has_binance_basis_source=lambda row:True, alert_context=context,
                  count_reminders=lambda rows,rounds:len({rounds[row.id] for row in rows}),
                  format_shanghai_time=lambda at,fmt:at.strftime(fmt), transfer_exchanges={'TEST/USDT':[]},
                  transfer_statuses={})
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
        rows = [item(i) for i in range(100)]
        rounds = {row.id:f'legacy:{row.id}' for row in rows}
        result = ns['alert_group_payload']('TEST/USDT',rows,rounds)
        self.assertEqual(len(references), 2)
        self.assertTrue(all(ref() is None for ref in references))
        self.assertEqual(result['observation_count'],100)
        self.assertEqual(result['paths'][0]['alert_count'],100)


if __name__ == '__main__':
    unittest.main()
