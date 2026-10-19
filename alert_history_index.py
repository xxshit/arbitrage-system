"""Incremental, process-local projection of COMMITTED public alert history.

Not a response/quote cache: callers still screen against current books, time,
quarantines, asset classification and requested notional on every request.
The single application writer invalidates IDs only AFTER commit. Database
revision audits catch other writers; restart/schema changes rebuild from DB.
No original evidence, accounts or database connections are retained here.
"""
from collections import namedtuple
import threading
import time


HistoryEvent = namedtuple('HistoryEvent', 'id symbol strategy alert_type message open_spread close_spread basis funding_rate created_at long_exchange short_exchange')


class AlertHistoryIndex:
    def __init__(self):
        self.read_lock = threading.Lock()
        self.change_lock = threading.Lock()
        self.generation = 0
        self.full = True
        self.dirty = set()
        self.rows = {}
        self.revisions = {}
        self.last_now = None
        self.last_audit = 0
        self.full_reads = self.delta_reads = self.decoded_rows = 0
        self.last_read_seconds = 0

    def reset(self):
        # Never wait for a DB read in the committing writer's thread.
        with self.change_lock:
            self.generation += 1
            self.full = True
            self.dirty.clear()

    def invalidate(self, identities):
        with self.change_lock:
            if not self.full:
                self.dirty.update(identities)
                if len(self.dirty) > 10000:
                    self.full = True
                    self.dirty.clear()

    def snapshot(self, start, now, load, audit, project):
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
                if not full and audit_at - self.last_audit >= 60:
                    revisions = dict(audit())
                    dirty.update(identity for identity, revision in revisions.items()
                                 if identity not in self.revisions or self.revisions[identity] != revision)
                    dirty.update(self.revisions.keys() - revisions.keys())
                replacements, versions = {}, {}
                for row in load(None if full else dirty, None if full else self.last_now):
                    event = HistoryEvent(*row[:12])
                    evidence, revision = row[12:]
                    versions[event.id] = revision
                    pulse = project(evidence) if evidence is not None else None
                    # An ineligible pulse must not masquerade as legacy history.
                    if evidence is None or pulse is not None:
                        replacements[event.id] = (event, pulse)
                with self.change_lock:
                    if self.generation != generation:
                        # Schema/reset raced this read. Do not publish a mixed
                        # generation; the next request safely rebuilds.
                        self.full = True
                        raise RuntimeError('alert_history_generation_changed')
                if full:
                    self.rows, self.revisions = replacements, versions
                    self.full_reads += 1
                else:
                    for identity in dirty:
                        self.rows.pop(identity, None)
                        self.revisions.pop(identity, None)
                    self.rows.update(replacements)
                    self.revisions.update(versions)
                    self.delta_reads += 1
                # Exact rolling window, including a record on the 24H boundary.
                # Only projection entries expire; DB evidence is never deleted.
                for identity in [identity for identity, (item, _) in self.rows.items()
                                 if not start <= item.created_at <= now]:
                    self.rows.pop(identity)
                    self.revisions.pop(identity, None)
                if revisions is not None:
                    self.revisions = revisions
                if full or revisions is not None:
                    self.last_audit = audit_at
                self.last_now = now
                self.decoded_rows += len(versions)
                self.last_read_seconds = time.monotonic() - began
                values = tuple(self.rows.values())
            except Exception:
                with self.change_lock:
                    self.full = self.full or full
                    self.dirty.update(dirty)
                raise
        events = sorted((item for item, _ in values), key=lambda item: (item.created_at, item.id), reverse=True)
        pulses = {item.id: pulse for item, pulse in values if pulse is not None}
        return events, pulses

    def status(self):
        return dict(rows=len(self.rows), full_reads=self.full_reads,
                    delta_reads=self.delta_reads, decoded_rows=self.decoded_rows,
                    last_read_seconds=round(self.last_read_seconds, 4))
