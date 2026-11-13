"""Bounded counts of known public caches, without walking the Python heap.

Only lengths and fixed category names leave this module. No symbols, quotes,
payloads, SQL, account objects, object addresses or retained sample history.
Busy locks and the time budget skip observations, never delay market work.
"""
import time


def public_object_counts(hub, payload_cache, payload_lock, snapshots, *, owners=None, budget_seconds=.008):
    try:
        return _counts(hub, payload_cache, payload_lock, snapshots, owners=owners, budget_seconds=budget_seconds)
    except Exception as error:
        # A diagnostic must not restart the health worker or leak exception text.
        return {'incomplete': True, 'error_type': type(error).__name__}


def _counts(hub, payload_cache, payload_lock, snapshots, *, owners=None, budget_seconds=.008):
    began = time.monotonic()
    deadline = began + max(0, min(.02, budget_seconds))
    output = {}

    def due():
        return time.monotonic() <= deadline

    if hub.lock.acquire(blocking=False):
        try:
            row = dict(routes=len(hub.routes), books=len(hub.books), marks=len(hub.marks),
                       workers=len(hub.workers), pending=len(hub.pending),
                       checkpoints=len(hub.persisted_checkpoints), tracks=len(hub.detector.tracks),
                       noise_windows=len(hub.noise), live_quotes=len(hub.live_quotes),
                       counted_tracks=0, history_buckets=0, packed_history_payload_bytes=0,
                       active_pulses=0, noise_points=0, counted_noise_windows=0)
            for (_, metric), track in hub.detector.tracks.items():
                if row['counted_tracks'] % 64 == 0 and (not due() or row['counted_tracks'] >= 16384):
                    break
                count = len(track.history)
                row['counted_tracks'] += 1
                row['history_buckets'] += count
                row['packed_history_payload_bytes'] += count * (32 if metric == 'basis' else 64)
                row['active_pulses'] += bool(track.pulse)
            for window in hub.noise.values():
                if row['counted_noise_windows'] % 64 == 0 and (not due() or row['counted_noise_windows'] >= 8192):
                    break
                row['counted_noise_windows'] += 1
                row['noise_points'] += len(window.samples)
            row['complete'] = row['counted_tracks'] == row['tracks'] and row['counted_noise_windows'] == row['noise_windows']
            output['stream'] = row
        finally:
            hub.lock.release()
    else:
        output['stream'] = {'skipped_busy': True}

    if due() and payload_lock.acquire(blocking=False):
        try:
            row = dict(entries=len(payload_cache), counted_entries=0, public_rows=0)
            for cached in payload_cache.values():
                if row['counted_entries'] >= 128 or not due():
                    break
                payload = cached.get('payload')
                rows = payload
                if type(payload) is dict:
                    rows = payload.get('symbols', payload.get('data'))
                    if rows is None and type(payload.get('result')) is dict:
                        rows = payload['result'].get('list')
                row['public_rows'] += len(rows) if type(rows) is list else 0
                row['counted_entries'] += 1
            row['complete'] = row['counted_entries'] == row['entries']
            output['market_payloads'] = row
        finally:
            payload_lock.release()
    else:
        output['market_payloads'] = {'skipped_busy_or_budget': True}

    # Callers give only the five known public cache roots. Ignore extra keys.
    for name in ('spot', 'dual', 'equity', 'spot_view', 'dual_view'):
        if not due():
            output[name] = {'skipped_budget': True}
            continue
        snapshot = snapshots.get(name)
        symbols = snapshot.get('symbols') if type(snapshot) is dict else None
        if type(symbols) is not list:
            output[name] = dict(symbols=0, groups_counted=0, rows=0, complete=True)
            continue
        row = dict(symbols=len(symbols), groups_counted=0, rows=0)
        for group in symbols:
            if row['groups_counted'] % 64 == 0 and (not due() or row['groups_counted'] >= 8192):
                break
            items = group.get('rows') if type(group) is dict else None
            row['rows'] += len(items) if type(items) is list else 0
            row['groups_counted'] += 1
        row['complete'] = row['groups_counted'] == row['symbols']
        output[name] = row
    for name, fields in (
        ('spread_buffer', ('minutes',)), ('book_buffer', ('pending', 'last_bucket')),
        ('activity', ('states', 'dirty', 'pending', 'summaries')),
        ('validation', ('active',)), ('trend_references', ('entries', 'retries')),
        ('profiles', ('data', 'days')),
    ):
        owner = (owners or {}).get(name)
        if owner is None:
            continue
        if not due() or not owner.lock.acquire(blocking=False):
            output[name] = {'skipped_busy_or_budget': True}
            continue
        try:
            output[name] = {field: len(getattr(owner, field)) for field in fields}
        finally:
            owner.lock.release()
    output['elapsed_seconds'] = round(time.monotonic()-began, 6)
    return output
