"""Bounded order-rejection and small reduce-only scenarios; no exchange IO."""
from dataclasses import replace
from decimal import Decimal as D
import json
import socket
import time
import unittest
from unittest.mock import patch

from binance_hedge.client import BinanceClient, ExchangeError, OrderGate
from binance_hedge.executor import Executor
from binance_hedge.planner import GuardError, reduction_plan
from test_binance_execution_boundaries import MatchClient, MemoryConnection, execute
from test_binance_taker import book, request_body, rules


class RejectedConnection(MemoryConnection):
    def __init__(self, market, status=400, code=-5021):
        super().__init__('api.binance.com' if market == 'spot' else 'fapi.binance.com')
        self.status, self.code = status, code

    def getresponse(self):
        owner = self
        class Response:
            status = owner.status
            def read(self, size):
                return json.dumps({'code': owner.code, 'msg': 'synthetic rejection'}).encode()
        return Response()


def rejection(market='futures', status=400, code=-5021, **changes):
    client = BinanceClient('synthetic-key', 'synthetic-secret', allow_orders=True)
    connection = RejectedConnection(market, status, code)
    client._connections[market] = connection
    client._offset[market] = (0, time.monotonic())
    params = dict(symbol='BTCUSDT', side='SELL', type='LIMIT', timeInForce='FOK',
                  quantity='.196', price='101.90', newClientOrderId='synthetic-rejection')
    params.update(changes)
    try:
        saved = Executor(None, client).place(market, params, OrderGate(time.monotonic()+1, parties=1), {})
        assert len(connection.writes) == 1
        return saved
    finally:
        client.close()


class NoNetworkTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('External network forbidden'))
        self.network.start()
    def tearDown(self):
        self.network.stop()


class RejectionTests(NoNetworkTests):
    def test_futures_fok_rejection_proves_zero_fill(self):
        saved = rejection()
        self.assertEqual(saved['status'], 'REJECTED')
        self.assertTrue(saved['explicit_rejection'])
        self.assertEqual(D(saved['executed_qty']), 0)
        self.assertEqual(saved['code'], '-5021')

    def test_other_market_order_type_http_status_and_codes_stay_unknown(self):
        cases = [dict(market='spot'), dict(timeInForce='IOC'), dict(timeInForce='GTC'),
                 dict(type='MARKET'), dict(status=503), dict(code=-2013),
                 dict(code=-2010), dict(code='-5021'), dict(code=-5021.0)]
        for changes in cases:
            with self.subTest(changes=changes):
                saved = rejection(**changes)
                self.assertEqual(saved['status'], 'UNKNOWN')
                self.assertNotIn('executed_qty', saved)

    def test_existing_validation_rejection_remains_definite(self):
        self.assertTrue(rejection(code=-1111)['explicit_rejection'])

    def rejected_execution(self, uncertain=False):
        classified = rejection(status=503 if uncertain else 400)
        class Client(MatchClient):
            def submit(self, market, params, **kwargs):
                raw = super().submit(market, params, **kwargs)
                if market == 'futures' and params['timeInForce'] == 'FOK':
                    raise ExchangeError(classified['code'], uncertain=not classified.get('explicit_rejection', False))
                return raw
            def query(self, market, symbol, client_id):
                if market == 'futures':
                    self.future_queries += 1
                    raise ExchangeError(-2013)
                return super().query(market, symbol, client_id)
        client = Client()
        client.zero = {'futures'}
        client.future_queries = 0
        out, store, intent = execute(client, request=request_body(residual_policy='reduce'))
        return client, out, store, intent

    def test_definite_rejection_allows_verified_spot_exit_without_missing_order_query(self):
        client, out, store, intent = self.rejected_execution()
        self.assertEqual(out['state'], 'NO_FILL')
        self.assertEqual(client.future_queries, 0)
        self.assertEqual(len(client.calls), 3)
        exit_market, params = client.calls[-1]
        self.assertEqual((exit_market, params['side'], params['timeInForce']), ('spot', 'SELL', 'IOC'))
        self.assertEqual(D(out['result']['residual_qty']), 0)
        Executor(store, client).reconcile(intent, out['result'])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.future_queries, 0)

    def test_uncertain_response_and_missing_order_never_authorize_exit(self):
        client, out, store, intent = self.rejected_execution(uncertain=True)
        self.assertEqual(out['state'], 'UNKNOWN')
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(out['result']['spot']['fills_verified'])
        self.assertNotIn('reduction', out['result'])
        Executor(store, client).reconcile(intent, out['result'])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(store.states[-1]['state'], 'UNKNOWN')


class ReductionTests(NoNetworkTests):
    def proposal(self, rule=None, market='futures', mode='BOTH', gap='-.01', depth=None):
        depth = depth or book(market == 'futures')
        return reduction_plan(request_body(residual_policy='reduce'), market, rule or rules(futures=True),
                              depth, time.monotonic(), D(gap), D('102') if market == 'futures' else D('100'),
                              position_side=mode)

    def test_confirmed_fee_gap_sends_small_reduce_only_then_reconciles_exactly(self):
        client = MatchClient()
        # 0.001 BTC fee is below the existing 2% fee bound for this synthetic clip.
        client.base_fee = '.001'
        out, store, intent = execute(client, request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'], 'HEDGED')
        self.assertEqual(len(client.calls), 3)
        market, params = client.calls[-1]
        self.assertEqual((market, params['side'], params['timeInForce']), ('futures', 'BUY', 'IOC'))
        self.assertEqual(params['reduceOnly'], 'true')
        self.assertEqual(params['positionSide'], 'BOTH')
        self.assertEqual(D(params['quantity']), D('.001'))
        self.assertLess(D(params['quantity'])*D(params['price']), D('5'))
        self.assertTrue(out['result']['reduction']['depth_check']['reduce_only'])
        self.assertEqual(D(out['result']['residual_qty']), 0)
        self.assertTrue(out['result']['reduction_fill']['fills_verified'])
        Executor(store, client).reconcile(intent, out['result'])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(D(store.states[-1]['result']['residual_qty']), 0)

    def test_small_exit_floors_without_increasing_confirmed_excess(self):
        proposal = self.proposal(gap='-.0109')
        self.assertTrue(proposal['reduce_only'])
        self.assertEqual(D(proposal['quantity']), D('.010'))
        self.assertEqual(D(proposal['expected_residual']), D('.0009'))

    def test_spot_hedge_and_unconfirmed_mode_keep_minimum(self):
        for market, mode, rule in [('spot', 'BOTH', rules()), ('futures', 'SHORT', rules(futures=True)),
                                   ('futures', None, rules(futures=True))]:
            with self.subTest(market=market, mode=mode), self.assertRaisesRegex(GuardError, '名义金额'):
                self.proposal(rule, market, mode)

    def test_market_rule_mismatch_or_invalid_position_mode_cannot_enable_exception(self):
        for market, mode, rule in [('futures', 'BOTH', rules()), ('spot', 'BOTH', rules(futures=True)),
                                   ('futures', 'LONG', rules(futures=True)), ('futures', 'bad', rules(futures=True))]:
            with self.subTest(market=market, mode=mode), self.assertRaises(GuardError):
                self.proposal(rule, market, mode)

    def test_opening_and_spot_rule_cannot_bypass_minimum(self):
        with self.assertRaisesRegex(GuardError, '名义金额'):
            rules(futures=True).check(D('.01'), D('102'))
        with self.assertRaises(GuardError):
            rules().check(D('.01'), D('102'), reduce_only=True)

    def test_reduce_only_preserves_lot_price_and_max_notional_guards(self):
        cases = [(rules(futures=True), '.0001', '102'), (rules(futures=True), '.0011', '102'),
                 (rules(futures=True), '101', '102'), (rules(futures=True), '.01', '102.001'),
                 (rules(futures=True), '.01', '0'), (rules(futures=True), '.01', '1000001'),
                 (replace(rules(futures=True), min_price=D('110')), '.01', '102'),
                 (replace(rules(futures=True), max_notional=D('1')), '.01', '102')]
        for rule, qty, price in cases:
            with self.subTest(qty=qty, price=price), self.assertRaises(GuardError):
                rule.check(D(qty), D(price), reduce_only=True)

    def test_below_min_qty_stale_book_and_out_of_budget_still_stop(self):
        with self.assertRaises(GuardError): self.proposal(gap='-.0001')
        with self.assertRaises(GuardError): self.proposal(depth=book(True, age=2))
        depth = book(True)
        depth = replace(depth, asks=((D('110'), D('100')),))
        with self.assertRaisesRegex(GuardError, '反向深度'): self.proposal(depth=depth)

    def test_small_ioc_partial_preserves_residual_and_never_retries(self):
        client = MatchClient(); client.base_fee = '.002'; client.ioc_available = D('.001')
        out, store, intent = execute(client, request=request_body(residual_policy='reduce'))
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(out['state'], 'ATTENTION')
        self.assertEqual(D(out['result']['residual_qty']), D('-.001'))
        Executor(store, client).reconcile(intent, out['result'])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(D(store.states[-1]['result']['residual_qty']), D('-.001'))

    def test_no_exit_permission_sends_no_small_order(self):
        client = MatchClient(); client.base_fee = '.001'
        out, _, _ = execute(client, request=request_body(residual_policy='hold'))
        self.assertEqual(out['state'], 'ATTENTION')
        self.assertEqual(len(client.calls), 2)

    def test_unknown_leg_blocks_even_confirmed_fee_gap(self):
        client = MatchClient(); client.base_fee = '.001'; client.query_missing = {'futures'}
        out, _, _ = execute(client, request=request_body(residual_policy='reduce'))
        self.assertEqual(out['state'], 'UNKNOWN')
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn('reduction', out['result'])

    def test_hedge_exit_uses_short_without_reduce_only_and_keeps_small_exit_blocked(self):
        class HedgeClient(MatchClient):
            def prepare(self, request, anchor=None):
                plan, spot, future = super().prepare(request, anchor)
                plan['position_side'] = 'SHORT'
                return plan, spot, future
        # Execute with an equally confirmed hedge-mode plan.
        from test_binance_taker import make_plan
        from test_binance_execution_boundaries import Recorder
        for fee, zero, expected_calls in [('0', {'spot'}, 3), ('.001', set(), 2)]:
            with self.subTest(fee=fee):
                client = HedgeClient(); client.zero = zero; client.base_fee = fee
                request = request_body(residual_policy='reduce'); plan = make_plan(request); plan['position_side'] = 'SHORT'
                intent = dict(id='b'*32, request=request, plan=plan, expires_at=time.time()+15)
                store = Recorder(); Executor(store, client).execute(intent, '12345')
                self.assertEqual(len(client.calls), expected_calls)
                if expected_calls == 3:
                    params = client.calls[-1][1]
                    self.assertEqual(params['positionSide'], 'SHORT')
                    self.assertNotIn('reduceOnly', params)
                else:
                    self.assertEqual(store.states[-1]['state'], 'ATTENTION')
                    self.assertIn('名义金额', store.states[-1]['result']['reason'])
