import math
import os
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "spread-history-test")
import app as module
from spread_history import (HistoryBuffer, HistoryStore, RETENTION, observations,
                            encode_chunk, decode_chunk, chart_series, merge_minutes)


class SpreadHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HistoryStore(self.temp.name)
        self.buffer = HistoryBuffer()
        self.start = 1792454400  # UTC midnight, exact minute/hour boundaries.
        self.key = ("TEST/USDT", "Gate", "open")

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, offset, value, key=None):
        self.buffer.observe([(key or self.key, self.start+offset, value)])

    def read(self, start=0, end=3600, step=60, key=None):
        return chart_series(self.store.chunks(*(key or self.key), self.start+start, self.start+end),
                            self.start+start, self.start+end, step)

    def test_minute_keeps_start_peak_trough_last_with_actual_times(self):
        for offset, value in [(0, 0), (5, 1), (10, .1), (15, -.5), (20, .2), (25, .15)]:
            self.observe(offset, value)
        self.store.persist(self.buffer, self.start+30)
        output = self.read()
        self.assertEqual([(t-self.start, v) for t, v, _ in output["points"]],
                         [(0, 0), (5, 1), (15, -.5), (25, .15)])
        self.assertEqual((output["low"], output["high"]), (-.5, 1))
        self.assertFalse(any(p[2] for p in output["points"]))

    def test_repeated_flush_restart_and_same_timestamp_are_idempotent(self):
        self.observe(0, 0)
        self.observe(5, 1)
        self.store.persist(self.buffer, self.start+6)
        self.observe(5, 999)  # Not a fresh observation.
        self.observe(10, .1)
        self.store.persist(self.buffer, self.start+11)
        self.buffer = HistoryBuffer()
        self.observe(15, -.5)
        self.store.persist(self.buffer, self.start+16)
        self.assertEqual(self.read()["high"], 1)
        self.assertEqual(self.read()["low"], -.5)
        self.assertEqual(self.store.persist(self.buffer, self.start+17), 0)

    def test_freshness_failed_sources_stale_mark_and_deduplicated_basis(self):
        now = 1000
        row = {"long_exchange": "Gate", "open_spread": 0, "basis": -.3, "basis_source_at": 999}
        groups = [{"symbol": "TEST/USDT", "rows": [row, {**row, "long_exchange": "Binance"}]}]
        observed = {name: 999 for name in ("funding", "futures_books", "binance_spot", "gate_spot")}
        result = list(observations(groups, observed, {}, now))
        self.assertEqual(len(result), 3)
        self.assertEqual(len([p for p in result if p[0][-1] == "basis"]), 1)
        result = list(observations(groups, observed, {"gate_spot": "cached"}, now))
        self.assertEqual(len(result), 2)
        result = list(observations(groups, {**observed, "funding": 900}, {}, now))
        self.assertTrue(all(p[0][-1] == "open" for p in result))
        result = list(observations(groups, {**observed, "futures_books": 1020}, {}, now))
        self.assertTrue(all(p[0][-1] == "basis" for p in result))
        groups[0]["rows"] = [{**row, "basis_source_at": 800, "open_spread": float("nan")}]
        self.assertEqual(list(observations(groups, observed, {}, now)), [])

    def test_paths_and_basis_are_separate_even_with_shared_symbol(self):
        for key, value in [(self.key, 1), (("TEST/USDT", "Binance", "open"), 2),
                           (("TEST/USDT", "Binance", "basis"), -.2),
                           (("OTHER/USDT", "Gate", "open"), 3)]:
            self.observe(0, value, key)
        self.store.persist(self.buffer, self.start+10)
        self.assertEqual(self.read()["high"], 1)
        self.assertEqual(self.read(key=("TEST/USDT", "Binance", "basis"))["high"], -.2)
        self.assertIsNone(self.read(key=("MISSING/USDT", "Gate", "open"))["high"])

    def test_gaps_in_a_minute_across_minutes_and_long_buckets_do_not_get_bridged(self):
        for offset in (0, 5, 40, 45, 120, 125):
            self.observe(offset, offset/100)
        self.store.persist(self.buffer, self.start+130)
        short = self.read()
        self.assertTrue(next(p[2] for p in short["points"] if p[0] == self.start+120))
        long = self.read(step=3600)
        self.assertTrue(all(p[2] for p in long["points"]))

    def test_continuous_extrema_far_apart_are_not_misclassified_as_a_gap(self):
        for offset in range(0, 120, 5):
            self.observe(offset, .2)
        self.store.persist(self.buffer, self.start+125)
        self.assertFalse(any(p[2] for p in self.read()["points"]))

    def test_pending_retry_is_retained_and_ack_does_not_erase_concurrent_observation(self):
        self.observe(0, 0)
        snapshot = self.buffer.snapshot(self.start)
        self.observe(5, 2)
        for chunk, minutes in snapshot.items():
            self.buffer.acknowledge(chunk, minutes)
        self.assertTrue(self.buffer.snapshot(self.start+6))
        with patch.object(self.store, "connect", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.persist(self.buffer, self.start+6)
        self.store.persist(self.buffer, self.start+7)
        self.assertEqual(self.read()["high"], 2)

    def test_bound_pending_memory_and_prune_saved_minutes(self):
        buffer = HistoryBuffer(max_minutes=3)
        for i in range(5):
            buffer.observe([(self.key, self.start+i*60, i)])
        self.assertEqual(len(buffer.minutes), 3)
        self.assertEqual(buffer.dropped, 2)
        self.store.persist(buffer, self.start+300)
        self.assertEqual(buffer.snapshot(self.start+301), {})
        self.assertEqual(len(buffer.minutes), 0)

    def test_exact_rolling_cutoff_across_days_and_future_points_excluded(self):
        for offset in (-1, 0, 86400, 86401):
            self.observe(offset, offset)
        self.store.persist(self.buffer, self.start+86402)
        data = self.read(end=86400)
        self.assertEqual((data["low"], data["high"]), (0, 86400))

    def test_read_of_missing_history_creates_no_files(self):
        self.assertEqual(self.read()["points"], [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_maintenance_preserves_boundary_day_makes_recoverable_checkpoint_and_prunes_only_own_expired_files(self):
        self.observe(0, 1)
        self.store.persist(self.buffer, self.start)
        harmless = Path(self.temp.name)/"accounts.sqlite3"
        harmless.write_bytes(b"do not touch")
        self.store.maintain(self.start+RETENTION)
        name = self.store.day(self.start)+".sqlite3"
        checkpoint = Path(self.temp.name)/"checkpoints"/name
        self.assertTrue(checkpoint.exists())
        recover = HistoryStore(checkpoint.parent)
        self.assertTrue(list(recover.chunks(*self.key, self.start, self.start+60)))
        self.store.maintain(self.start+RETENTION+86400)
        self.assertFalse((Path(self.temp.name)/name).exists())
        self.assertFalse(checkpoint.exists())
        self.assertTrue(harmless.exists())

    def test_long_view_is_bounded_preserves_peak_and_streams_chunks(self):
        def chunks():
            for hour in range(360):
                minutes = {}
                for minute in range(60):
                    t = self.start + hour*3600 + minute*60
                    minutes[str(t)] = [[[t, .1], [t+5, 9 if (hour, minute) == (137, 23) else .2],
                                        [t+50, -.1], [t+55, .1]]]
                yield encode_chunk(minutes)
        tracemalloc.start()
        data = chart_series(chunks(), self.start, self.start+RETENTION, 1800)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLessEqual(len(data["points"]), 2880)
        self.assertEqual(data["high"], 9)
        self.assertLess(peak, 5*1024*1024)
        self.assertFalse(any(p[2] for p in data["points"]))

    def test_api_validation_auth_and_cache_with_no_upstream(self):
        with patch.object(module, "SPREAD_HISTORY_STORE", self.store), patch.object(module, "get_json", side_effect=AssertionError("No network")):
            for suffix in ("period=16d", "exchange=OKX", "symbol=../../etc/USDT"):
                args = {"symbol": "TEST/USDT", "exchange": "Gate", "period": "24h"}
                key, value = suffix.split("=")
                args[key] = value
                with module.app.test_request_context("/api/spot-futures/history", query_string=args):
                    self.assertEqual(module.spot_spread_history()[1], 400)
            with module.app.test_request_context("/api/spot-futures/history?symbol=TEST/USDT&exchange=Gate&period=15d"):
                result = module.spot_spread_history().get_json()
                self.assertEqual(result["retention_days"], 15)
                self.assertEqual(result["series"]["open"]["points"], [])
            response = module.app.test_client().get("/api/spot-futures/history?symbol=TEST/USDT&exchange=Gate")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")

    def test_bad_or_oversized_payload_is_not_silently_drawn(self):
        with self.assertRaises(Exception):
            decode_chunk(b"corrupt")
        with self.assertRaises(ValueError):
            decode_chunk(encode_chunk({"x": "a"*150000}))

    def test_low_disk_space_leaves_retry_buffer_and_prior_checkpoint_intact(self):
        self.observe(0, 1)
        self.store.persist(self.buffer, self.start)
        self.store.maintain(self.start)
        checkpoint = Path(self.temp.name)/"checkpoints"/(self.store.day(self.start)+".sqlite3")
        before = checkpoint.read_bytes()
        self.observe(5, 2)
        with patch("spread_history.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(OSError):
                self.store.persist(self.buffer, self.start+6)
        self.assertTrue(self.buffer.snapshot(self.start+6))
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_checkpoint_includes_new_wal_values_while_an_old_reader_is_active(self):
        self.observe(0, 1)
        self.store.persist(self.buffer, self.start)
        self.store.maintain(self.start)
        reader = self.store.connect(self.store.day(self.start))
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT payload FROM history").fetchall()
            self.observe(5, 2)
            self.store.persist(self.buffer, self.start+6)
            self.store.maintain(self.start+6)
            recover = HistoryStore(Path(self.temp.name)/"checkpoints")
            data = chart_series(recover.chunks(*self.key, self.start, self.start+60),
                                self.start, self.start+60, 60)
            self.assertEqual(data["high"], 2)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
