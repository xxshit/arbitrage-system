"""Incremental, process-local projection of COMMITTED public alert history.

Not a response/quote cache: callers still screen against current books, time,
quarantines, asset classification and requested notional on every request.
The single application writer invalidates IDs only AFTER commit. Database
revision audits catch other writers; restart/schema changes rebuild from DB.
No original evidence, accounts or database connections are retained here.
"""
from collections import namedtuple
from collections.abc import Mapping
import threading
import time
import copy
from sys import intern

from alert_read_payload import PackedPulse, PulseSpool


HistoryEvent = namedtuple('HistoryEvent', 'id symbol strategy alert_type message open_spread close_spread basis funding_rate created_at long_exchange short_exchange')


class _IndexMap(Mapping):
    """Immutable publication with bounded copy units for integer event IDs.

    Each publication owns a flat tuple of 256 dictionaries. Unchanged shards
    are shared directly, never through a chain of prior publications. Only the
    private builder mutates dictionaries, before publishing them to readers.
    """
    __slots__ = ('_parts', '_size')

    def __init__(self, parts=None, size=0):
        self._parts = ({},) * 256 if parts is None else parts
        self._size = size

    def __getitem__(self, identity):
        return self._parts[identity & 255][identity]

    def __iter__(self):
        for part in self._parts:
            yield from part

    def __len__(self):
        return self._size

    def items(self):
        for part in self._parts:
            yield from part.items()

    def values(self):
        for part in self._parts:
            yield from part.values()


class _IndexMapBuilder:
    """One refresh's private writes; never mutate a published dictionary."""
    __slots__ = ('_base', '_parts', '_owned', '_size', '_sealed')

    def __init__(self, base=None):
        self._base = _IndexMap() if base is None else base
        self._parts = list(self._base._parts)
        self._owned = set()
        self._size = len(self._base)
        self._sealed = False

    def _mutable(self, partition):
        if self._sealed:
            raise RuntimeError('index_builder_already_published')
        if partition not in self._owned:
            self._parts[partition] = dict(self._parts[partition])
            self._owned.add(partition)
        return self._parts[partition]

    def __setitem__(self, identity, value):
        partition = identity & 255
        part = self._mutable(partition)
        self._size += identity not in part
        part[identity] = value

    def update(self, values):
        for identity, value in values.items():
            self[identity] = value

    def pop(self, identity, default=None):
        partition = identity & 255
        if identity not in self._parts[partition]:
            return default
        part = self._mutable(partition)
        self._size -= 1
        return part.pop(identity)

    def items(self):
        for part in self._parts:
            yield from part.items()

    def values(self):
        for part in self._parts:
            yield from part.values()

    def freeze(self):
        self._sealed = True
        return (_IndexMap(tuple(self._parts), self._size)
                if self._owned else self._base)


class AlertHistoryIndex:
    def __init__(self):
        self.read_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.change_lock = threading.Lock()
        self.generation = 0
        self.full = True
        self.dirty = set()
        self.rows = _IndexMap()
        self.revisions = _IndexMap()
        self.last_now = None
        self.last_audit = 0
        self.full_reads = self.delta_reads = self.decoded_rows = 0
        self.last_read_seconds = 0
        self.spool = None
        self.published_ready = False

    def reset(self):
        # Never wait for a DB read in the committing writer's thread.
        with self.change_lock:
            self.generation += 1
            self.full = True
            self.published_ready = False
            self.dirty.clear()

    def invalidate(self, identities):
        with self.change_lock:
            if not self.full:
                self.dirty.update(identities)
                if len(self.dirty) > 10000:
                    self.full = True
                    self.published_ready = False
                    self.dirty.clear()

    def snapshot(self, start, now, load, audit, project, symbol=None, materialize=True):
        """load(ids|None, since|None) yields (12 scalar columns, JSON, revision).

        Refresh work is serialized separately from the stream and commit locks.
        Failed reads never partially replace the projection or acknowledge IDs.
        Returned rows/payloads are read-only by convention; updates replace them.
        """
        began = time.monotonic()
        with self.read_lock:
            with self.change_lock:
                generation, full, dirty = self.generation, self.full, self.dirty
                self.full, self.dirty = False, set()
            full = full or self.last_now is None or now < self.last_now
            try:
                revisions = None
                audit_at = time.monotonic()
                if not full and audit_at - self.last_audit >= 900:
                    revisions = dict(audit())
                    dirty.update(identity for identity, revision in revisions.items()
                                 if identity not in self.revisions or self.revisions[identity] != revision)
                    dirty.update(self.revisions.keys() - revisions.keys())
                replacements, versions = {}, {}
                spool = PulseSpool() if full or self.spool is None else self.spool
                for row in load(None if full else dirty, None if full else self.last_now):
                    values = list(row[:12])
                    for position in (1, 2, 3, 10, 11):
                        if isinstance(values[position], str):
                            values[position] = intern(values[position])
                    event = HistoryEvent(*values)
                    evidence, revision = row[12:]
                    versions[event.id] = revision
                    pulse = project(evidence) if evidence is not None else None
                    # An ineligible pulse must not masquerade as legacy history.
                    if evidence is None or pulse is not None:
                        replacements[event.id] = (event, PackedPulse(pulse, spool) if pulse is not None else None)
                with self.change_lock:
                    if self.generation != generation:
                        # Schema/reset raced this read. Do not publish a mixed
                        # generation; the next request safely rebuilds.
                        self.full = True
                        raise RuntimeError('alert_history_generation_changed')
                # Copy only shards changed by this refresh. Readers retain the
                # last complete immutable publication during reads and audits.
                rows = _IndexMapBuilder(None if full else self.rows)
                revisions_before = _IndexMapBuilder(None if full else self.revisions)
                for identity in dirty:
                    rows.pop(identity, None)
                    revisions_before.pop(identity, None)
                rows.update(replacements)
                revisions_before.update(versions)
                # Exact rolling window, including a record on the 24H boundary.
                # Only projection entries expire; DB evidence is never deleted.
                for identity in [identity for identity, (item, _) in rows.items()
                                 if not start <= item.created_at <= now]:
                    rows.pop(identity)
                    revisions_before.pop(identity, None)
                if revisions is not None:
                    revisions_before = _IndexMapBuilder()
                    revisions_before.update(revisions)
                if full or revisions is not None:
                    self.last_audit = audit_at
                # Repeated updates append immutable blobs. Bound dead cache
                # space without changing snapshots currently used by readers.
                live_bytes = sum(pulse.size for _, pulse in rows.values() if pulse is not None)
                if spool.size > live_bytes * 2 + 16 * 1024 * 1024:
                    compacted = PulseSpool()
                    compact_rows = _IndexMapBuilder()
                    for identity, (item, pulse) in rows.items():
                        if pulse is not None:
                            blob = pulse.raw()
                            pulse = copy.copy(pulse)
                            pulse.spool = compacted
                            pulse.offset, pulse.size = compacted.append(blob)
                        compact_rows[identity] = item, pulse
                    rows, spool = compact_rows, compacted
                with self.change_lock:
                    if self.generation != generation:
                        self.full = True
                        raise RuntimeError('alert_history_generation_changed')
                    with self.cache_lock:
                        self.rows, self.revisions, self.spool, self.last_now = rows.freeze(), revisions_before.freeze(), spool, now
                        self.published_ready = not self.full
                if full:
                    self.full_reads += 1
                else:
                    self.delta_reads += 1
                self.decoded_rows += len(versions)
                self.last_read_seconds = time.monotonic() - began
            except Exception:
                with self.change_lock:
                    self.full = self.full or full
                    self.dirty.update(dirty)
                raise
        return self.cached_snapshot(start, now, symbol) if materialize else None

    def cached_snapshot(self, start, now, symbol=None):
        # Immutable publication; no DB, read-lock, network or full JSON parsing.
        with self.cache_lock:
            rows = self.rows
        values = (value for value in rows.values() if (symbol is None or value[0].symbol == symbol) and start <= value[0].created_at <= now)
        values = tuple(values)
        events = sorted((item for item, _ in values), key=lambda item: (item.created_at, item.id), reverse=True)
        pulses = {item.id: pulse for item, pulse in values if pulse is not None}
        return events, pulses

    def ready(self):
        return self.published_ready

    def status(self):
        return dict(rows=len(self.rows), full_reads=self.full_reads,
                    delta_reads=self.delta_reads, decoded_rows=self.decoded_rows,
                    spool_bytes=self.spool.size if self.spool else 0,
                    last_read_seconds=round(self.last_read_seconds, 4))
