"""No real exchange or account access: ownership, latency and local transport."""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

from anomaly_streams import AlertStreamHub, PublicWorker, Route, StreamLagGuard, parse_frame


class LagGuardTests(unittest.TestCase):
    def test_stale_streak_reconnects_even_while_messages_keep_arriving(self):
        guard = StreamLagGuard("gate", 4)
        for tick in range(5):
            self.assertFalse(guard.observe({"at": 900+tick}, 1000+tick, tick))
            self.assertFalse(guard.expired)
        self.assertFalse(guard.observe({"at": 905}, 1005, 5))
        self.assertTrue(guard.expired)

    def test_fresh_frame_clears_transient_lag_and_age_boundaries_stay_strict(self):
        guard = StreamLagGuard("bybit", 4)
        self.assertFalse(guard.observe({"at": 990}, 1000, 0))
        self.assertTrue(guard.observe({"at": 1002}, 1004, 4))
        self.assertIsNone(guard.stale_since)
        self.assertFalse(guard.observe({"at": 999}, 1005, 5))
        self.assertFalse(guard.expired)
        for stamp, expected in [(1001, True), (1000.999, False), (1007, True), (1007.001, False), (float('nan'), False)]:
            self.assertEqual(StreamLagGuard('gate', 4).observe({'at': stamp}, 1005, 0), expected)

    def test_binance_spot_requires_same_connection_clock_without_retiming_book(self):
        guard = StreamLagGuard("bn_spot", 4)
        book = parse_frame("bn_spot", dict(s="XUSDT", b="1", a="2", B="1", A="1"), 1000)[0]
        self.assertFalse(guard.observe(book, 1000, 0))
        clock = parse_frame("bn_spot", dict(e="24hrMiniTicker", s="BTCUSDT", E=999000), 1000)[0]
        self.assertFalse(guard.observe(clock, 1000, .1))
        self.assertTrue(guard.observe(book, 1000, .2))
        self.assertEqual((book['at'], book['timestamp_source']), (1000, 'receipt'))
        self.assertFalse(guard.observe({**book, 'at': 1010}, 1010, 10))
        self.assertFalse(StreamLagGuard('bn_spot', 4).observe(book, 1000, 0))  # reconnect is cold

    def test_clock_never_becomes_a_fake_price_observation(self):
        for stamp in (1000000, 990000):
            clock = parse_frame('bn_spot', dict(e='24hrMiniTicker', s='BTCUSDT', E=stamp), 1000)[0]
            self.assertNotIn('bid', clock)
            self.assertFalse(StreamLagGuard('bn_spot', 4).observe(clock, 1000, 0))


class ShardTests(unittest.TestCase):
    def routes(self, symbols):
        return [Route(('spot_futures', s, 'Gate', 'Binance'), ('gate', s), ('bn_future', s)) for s in symbols]

    def test_insertion_does_not_reconnect_all_later_shards(self):
        hub = AlertStreamHub()
        names = [f'B{i:03}' for i in range(205)]
        with patch.object(hub, '_start_worker') as start:
            hub.register('spot_futures', self.routes(names));hub.reconcile()
            first = dict(hub.workers)
            self.assertEqual(start.call_count, 6)
            hub.register('spot_futures', self.routes(['A000']+names));hub.reconcile()
            self.assertEqual(start.call_count, 8)  # only the two partial shards
            for key in [('gate', 0), ('gate', 1), ('bn_future', 0), ('bn_future', 1)]:
                self.assertIs(hub.workers[key], first[key])
            self.assertEqual(set(hub.owners), {(feed,s) for feed in ('gate','bn_future') for s in names+['A000']})

    def test_removal_only_replaces_the_affected_shard_and_removes_cache(self):
        hub = AlertStreamHub();names = [f'B{i:03}' for i in range(205)]
        with patch.object(hub, '_start_worker') as start:
            hub.register('spot_futures', self.routes(names));hub.reconcile()
            first = dict(hub.workers)
            hub.books[('gate', 'B001')] = {'at': 1}
            hub.register('spot_futures', self.routes([s for s in names if s != 'B001']));hub.reconcile()
            self.assertEqual(start.call_count, 8)
            self.assertIs(hub.workers[('gate', 1)], first[('gate', 1)])
            self.assertNotIn(('gate', 'B001'), hub.books)
            self.assertNotIn(('gate', 'B001'), hub.owners)
            hub.register('spot_futures', []);hub.reconcile()
            self.assertFalse(hub.workers);self.assertFalse(hub.owners)

    def test_retired_socket_cannot_ingest_or_invalidate_replacement(self):
        hub = AlertStreamHub();hub.register('spot_futures', self.routes(['X']))
        old, new = PublicWorker('gate', ['X'], hub), PublicWorker('gate', ['X'], hub)
        hub.owners[('gate', 'X')] = new
        quote = dict(symbol='X', at=1000, bid=1, ask=2, bid_qty=1, ask_qty=1)
        hub.ingest('gate', quote, 1000, owner=new)
        hub.ingest('gate', {**quote, 'at':1001, 'bid':1.5}, 1001, owner=old)
        hub.invalidate([('gate', 'X')], 1001, owner=old)
        self.assertEqual(hub.books[('gate', 'X')]['bid'], 1)
        hub.invalidate([('gate', 'X')], 1001, owner=new)
        self.assertNotIn(('gate', 'X'), hub.books)


class AsyncTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_compressed_socket_preserves_spike_retrace_and_restart_order(self):
        from websockets.asyncio.server import serve
        hub = AlertStreamHub()
        route = Route(('spot_futures', 'DEMO/USDT', 'Gate', 'Binance'), ('gate', 'DEMO_USDT'), ('bn_future', 'DEMOUSDT'))
        hub.register('spot_futures', [route])
        worker = PublicWorker('bn_future', ('DEMOUSDT',), hub)
        hub.owners[route.short_key] = worker
        received, sent = [], asyncio.Event()
        original = hub.ingest
        def ingest(feed, item, now, **kwargs):
            received.append(item['bid'])
            original(feed, item, now, **kwargs)
            if len(received) == 4:
                sent.set()
        async def handler(socket):
            await socket.recv()
            self.assertIn('permessage-deflate', socket.response.headers['Sec-WebSocket-Extensions'])
            for i, value in enumerate((100,101,100.1,100.7)):
                stamp=time.time()
                original('gate', dict(symbol='DEMO_USDT', at=stamp, bid=100, ask=100.001, bid_qty=1, ask_qty=1), stamp)
                await socket.send(json.dumps(dict(e='depthUpdate',s='DEMOUSDT',E=stamp*1000,u=i+1,b=[[str(value),'1']],a=[[str(value+.001),'1']])))
                await asyncio.sleep(.01)
            await sent.wait()
            await socket.wait_closed()
        async with serve(handler, '127.0.0.1', 0) as server:
            with patch.dict('anomaly_streams.ENDPOINTS', {'bn_future': f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'}), patch.object(hub,'ingest',side_effect=ingest):
                task=asyncio.create_task(worker.run())
                try:
                    await asyncio.wait_for(sent.wait(), 3)
                    self.assertEqual(received,[100,101,100.1,100.7])
                    pulses=[p for p in hub.batch() if p['metric']=='spread']
                    self.assertEqual(len(pulses),2, (hub.status(), [(key, track.last) for key,track in hub.detector.tracks.items()]))
                    self.assertEqual(pulses[0]['state'],'retraced')
                    self.assertGreater(pulses[0]['peak'],.99)
                    self.assertEqual(pulses[1]['ordinal'],2)
                finally:
                    worker.stop.set();task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        self.assertFalse(worker.connected)
        self.assertNotIn(route.short_key,hub.books)

    async def test_single_io_loop_is_reused_for_multiple_sockets(self):
        hub=AlertStreamHub()
        with patch('anomaly_streams.threading.Thread') as thread, patch('anomaly_streams.asyncio.run_coroutine_threadsafe') as schedule:
            schedule.side_effect=lambda coroutine,loop: coroutine.close()
            hub._start_worker(PublicWorker('gate',('X',),hub))
            hub._start_worker(PublicWorker('gate',('Y',),hub))
            self.assertEqual(thread.call_count,1)
            self.assertEqual(schedule.call_count,2)
            hub.loop.close()
