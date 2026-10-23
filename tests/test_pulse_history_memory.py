import math
import random
import tracemalloc
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from anomaly_engine import HISTORY_FIELDS, HistorySample, PulseDetector, Sample, Track


class ReferenceTrack(Track):
    """Frozen pre-compaction algorithm: test losslessness, not new policy."""
    def remember(self, sample, metric):
        sample = HistorySample(sample.at, sample.value, metric, *(sample.context[n] for n in HISTORY_FIELDS[metric]))
        if self.history and all((p.value, p.first, p.second) == (sample.value, sample.first, sample.second) for p in self.history[-1]):
            self.history[-1] = [sample, sample]
        elif not self.history or int(self.history[-1][0].at) != int(sample.at):
            self.history.append([sample, sample])
        else:
            if sample.value < self.history[-1][0].value:
                self.history[-1][0] = sample
            if sample.value > self.history[-1][1].value:
                self.history[-1][1] = sample
        self.low = sample.value if self.low is None else min(self.low, sample.value)
        self.high = sample.value if self.high is None else max(self.high, sample.value)
        expired_extreme = False
        while self.history and sample.at - self.history[0][1].at > 120:
            expired = self.history.popleft()
            expired_extreme |= any(p.value in (self.low, self.high) for p in expired)
        if expired_extreme:
            self.low = min(p.value for b in self.history for p in b)
            self.high = max(p.value for b in self.history for p in b)
        self.last = sample


def context(value, at):
    return dict(open_spread=value, close_spread=value+.02, mid_spread=value+.01,
                long_mid=100+math.sin(at/40), short_mid=101+math.cos(at/30),
                basis=value, funding_rate=.005)


class PulseHistoryMemoryTests(unittest.TestCase):
    def test_randomized_pulses_extrema_retraces_restarts_and_gaps_match_reference(self):
        randomizer = random.Random(9418)
        samples, at = [], 1000
        for i in range(3500):
            at += 12 if i % 430 == 0 else randomizer.choice((.05, .2, .5, 1, 2))
            value = 1.2*math.sin(i/22) if i % 700 < 300 else randomizer.uniform(-.01, .01)
            samples.append((at, value, context(value, at)))
        def simulate(track_type):
            detector, result = PulseDetector(), []
            with patch("anomaly_engine.Track", track_type), patch("anomaly_engine.uuid.uuid4", return_value=SimpleNamespace(hex="deterministic")):
                for at, value, payload in samples:
                    for metric in ("spread", "basis", "price"):
                        result.extend(detector.observe(("spot_futures", "TEST/USDT", "Gate", "Binance"), metric, value, at, payload))
            return result
        expected = simulate(ReferenceTrack)
        self.assertTrue(any(p["state"] == "retraced" for p in expected))
        self.assertTrue(any(p["ordinal"] > 1 for p in expected))
        self.assertTrue(any(p["state"] == "data_gap" for p in expected))
        self.assertEqual(simulate(Track), expected)

    def test_retained_idle_history_memory_reduced_without_shortening_window(self):
        def measure(track_type):
            tracemalloc.start()
            tracks = [track_type() for _ in range(100)]
            for i in range(242):
                for track in tracks:
                    at = 1000+i*.5
                    value = math.sin(i)*.02
                    track.remember(Sample(at, value, context(value, at)), "spread")
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertGreaterEqual(len(tracks[0].history), 120)
            return current
        before, after = measure(ReferenceTrack), measure(Track)
        self.assertLess(after, before*.65, (before, after))


if __name__ == "__main__":
    unittest.main()
