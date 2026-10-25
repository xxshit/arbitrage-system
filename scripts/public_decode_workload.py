"""Generated public-response decode workload; no transport, app or database.

Exercise varying gzip sizes concurrently with other soak work. Preserve exact
JSON fields and a bounded latest-generation cache, never any live market data.
"""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import time

from market_transport import decode_json


class PublicDecodeWorkload:
    def __init__(self, lanes=12, variants=32):
        assert 1 <= lanes <= 12 and 1 <= variants <= 32
        self.lanes, self.variants = lanes, variants
        self.cycles = self.responses = 0
        self.cache = {}
        self.bodies = []
        self.lengths = []
        for variant in range(variants):
            raw = json.dumps(self.expected(variant), separators=(",", ":")).encode()
            self.bodies.append(gzip.compress(raw, compresslevel=1))
            self.lengths.append(len(raw))

    @staticmethod
    def expected(variant):
        size = 25000 + ((variant*131071) % 700000)
        return dict(variant=variant, symbol=f"SYNTH{variant}/USDT", rate="0.00005001",
                    price="123.000000001", body="value-" + "x"*size)

    def step(self):
        started = time.monotonic()
        cycle = self.cycles + 1
        def read(lane):
            variant = (cycle*11+lane*7) % self.variants
            value, length = decode_json(self.bodies[variant], "gzip")
            assert value == self.expected(variant), "synthetic public decode values changed"
            assert length == self.lengths[variant], "synthetic decoded length changed"
            return lane, value
        with ThreadPoolExecutor(max_workers=self.lanes, thread_name_prefix="soak-gzip") as executor:
            for lane, value in executor.map(read, range(self.lanes)):
                self.cache[lane] = value
        self.cycles = cycle
        self.responses += self.lanes
        return time.monotonic()-started

    def verify(self):
        assert self.cycles > 0 and self.responses == self.cycles*self.lanes
        assert len(self.cache) == self.lanes
        for lane, value in self.cache.items():
            variant = (self.cycles*11+lane*7) % self.variants
            assert value == self.expected(variant), "synthetic cached public value changed"
        return dict(cycles=self.cycles, responses=self.responses, lanes=self.lanes,
                    variants=self.variants, exact_values=True, real_network=False)
