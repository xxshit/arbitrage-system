"""Small immutable alert digests; full public projections live in a temp spool.

This is a disposable read cache, NOT history storage. MySQL evidence is untouched.
pread permits concurrent snapshots without a shared seek cursor. Old snapshots
own their spool until released, including during atomic rebuild/compaction.
"""
from collections.abc import Mapping
from functools import lru_cache
import json
import os
import tempfile
import zlib
from sys import intern
from anomaly_engine import finite


class PulseSpool:
    def __init__(self):
        self.file = tempfile.TemporaryFile(prefix="arbiscope-alert-read-")
        self.size = 0

    def __del__(self):
        self.file.close()

    def append(self, data):
        offset = self.size
        self.file.seek(offset)
        self.file.write(data)
        self.file.flush()
        self.size += len(data)
        return offset, len(data)


class PackedPulse(Mapping):
    __slots__ = ("spool", "offset", "size", "identity", "episode", "metric",
                 "notice_at", "notice_times", "has_reminder", "last_ticket",
                 "bounds", "path", "current_at", "reference_at", "ended")

    def __init__(self, payload, spool):
        self.spool = spool
        self.offset, self.size = spool.append(zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), 1))
        self.identity, self.episode, self.metric = (payload.get(k) for k in ("id", "episode", "metric"))
        reminder = payload.get("reminder", {})
        notices = reminder.get("notices", [])
        self.last_ticket = notices[-1] if notices else None
        self.notice_at = self.last_ticket["at"] if notices else payload.get("noticed_at")
        self.notice_times = tuple(ticket["at"] for ticket in notices)
        self.has_reminder = "reminder" in payload
        ranges = payload.get("ranges", {})
        self.bounds = tuple(ranges.get(field, {}).get(bound) for field in ("open_spread", "basis") for bound in ("min", "max"))
        self.path = tuple(intern(part) if isinstance(part, str) else part for part in payload.get("path", ()))
        self.current_at = payload.get("current_at", 0)
        self.reference_at = payload.get("screen_reference", {}).get("to_at", 0)
        self.ended = bool(payload.get("ended_at")) or payload.get("state") in ("data_gap", "isolated", "settled")

    def raw(self):
        data = os.pread(self.spool.file.fileno(), self.size, self.offset)
        if len(data) != self.size:
            raise IOError("incomplete alert projection spool")
        return data

    def unpack(self):
        return json.loads(zlib.decompress(self.raw()))

    def __getitem__(self, key):
        if key == "id":
            return self.identity
        if key == "episode":
            return self.episode
        if key == "metric":
            return self.metric
        return self.unpack()[key]

    def __iter__(self):
        return iter(self.unpack())

    def __len__(self):
        return len(self.unpack())

    def __bool__(self):
        return True

    def can_screen_live(self, now):
        return not self.ended and finite(self.current_at) and finite(self.reference_at) and 0 <= now - self.current_at <= 10 and 0 <= now - self.reference_at <= 600

    def ticket(self, now):
        ticket = self.last_ticket
        return ticket if ticket and not ticket.get("seeded") and 0 <= now - ticket["at"] <= 120 else None

    def count(self, now):
        return sum(0 <= now - at <= 86400 for at in self.notice_times)


class Screenings(Mapping):
    """Only materialize assessments for visible rows and potentially live pulses."""
    def __init__(self, pulses, quotes, now, notional, screen):
        self.pulses, self.quotes, self.now = pulses, quotes, now
        self.overrides = {}

        @lru_cache(maxsize=256)
        def assess(identity):
            pulse = pulses.get(identity)
            if pulse is None:
                return {"tier": "record", "reason": "历史记录，未核验当前盘口", "live": False}
            payload = pulse.unpack()
            return screen(payload, quotes.get(pulse.path), now, notional)
        self.assess = assess

    def __getitem__(self, identity):
        return self.overrides[identity] if identity in self.overrides else self.assess(identity)

    def __setitem__(self, identity, value):
        self.overrides[identity] = value

    def __iter__(self):
        return iter(self.pulses)

    def __len__(self):
        return len(self.pulses)
