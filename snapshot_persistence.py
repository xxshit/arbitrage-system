"""Lossless latest-state deltas; never used for market history or account tables."""

from collections import defaultdict
from datetime import datetime
import json
import math
import struct

from sqlalchemy import JSON, Float, String, case, cast, func, literal, update

SNAPSHOT_TABLES = frozenset({"latest_market_snapshot", "latest_dual_futures_snapshot"})
UPDATE_BATCH_SIZE = 100
JSON_PATCH_LIMIT = 1024


def json_equal(left, right):
    # Python considers True == 1; JSON does not. Dict key order is irrelevant.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    return left == right


def storage_value(column, value, dialect):
    # These existing MySQL columns are FLOAT (binary32), not DOUBLE. Compare at
    # their EXISTING storage precision; do not round live quotes or history.
    if dialect == "mysql" and isinstance(column.type, Float) and value is not None:
        if math.isfinite(value):
            try:
                return struct.unpack("f", struct.pack("f", value))[0]
            except OverflowError:
                pass  # Let the database reject an invalid value as before.
    if dialect == "mysql" and isinstance(value, datetime):
        return value.replace(microsecond=0)  # Existing DATETIME has no fsp.
    return value


def changed_values(table, previous, values, dialect):
    """Omit only fields already equal in storage, including real None/zero."""
    return {key: value for key, value in values.items()
            if storage_value(table.c[key], getattr(previous, key), dialect) !=
            storage_value(table.c[key], value, dialect)}


def update_snapshots(session, table, updates):
    """Batch changed fields into bounded statements instead of one per path.

    Keep per-row captured_at heartbeats even when a quote is unchanged. A missing
    path is NOT refreshed by a global clock. All statements share the caller's
    transaction; no cache/checkpoint is advanced here.
    """
    if table.name not in SNAPSHOT_TABLES:
        raise ValueError("Only latest public quote snapshots may use this writer")
    groups = defaultdict(list)
    for values in updates:
        fields = tuple(sorted(key for key in values if key != "id"))
        if fields:
            groups[fields].append(values)
    for fields, rows in groups.items():
        for start in range(0, len(rows), UPDATE_BATCH_SIZE):
            batch = rows[start:start + UPDATE_BATCH_SIZE]
            columns = {}
            for field in fields:
                values = [row[field] for row in batch]
                columns[field] = (values[0] if all(value == values[0] for value in values)
                                  else case({row["id"]: row[field] for row in batch}, value=table.c.id))
            session.execute(update(table).where(table.c.id.in_([row["id"] for row in batch])).values(**columns))


def json_delta(previous, current, limit=JSON_PATCH_LIMIT):
    """Bounded JSON_SET/REMOVE plan, or None for a full replacement.

    List-size changes replace the list, so deletes cannot shift later indexes.
    Keys are JSON-escaped and passed as bound parameters, not SQL fragments.
    """
    sets, removes = [], []

    def walk(old, new, path):
        if len(sets) + len(removes) > limit:
            raise OverflowError
        if json_equal(old, new):
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in old.keys() - new.keys():
                removes.append(path + "." + json.dumps(key, ensure_ascii=False))
            for key, value in new.items():
                child = path + "." + json.dumps(key, ensure_ascii=False)
                if key not in old:
                    sets.append((child, value))
                else:
                    walk(old[key], value, child)
        elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            for index, (left, right) in enumerate(zip(old, new)):
                walk(left, right, f"{path}[{index}]")
        else:
            sets.append((path, new))

    try:
        walk(previous, current, "$")
    except OverflowError:
        return None
    if len(sets) + len(removes) > limit:
        return None
    # A dense/structural update is cheaper and safer as one ordinary JSON value.
    encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded([sets, removes])) >= len(encoded(current)) * .75:
        return None
    return sets, removes


def equity_payload_update(session, table, previous, current, updated_at):
    if table.name != "equity_spot_snapshot":
        raise ValueError("JSON deltas are only for the public equity snapshot")
    values = {"updated_at": updated_at}
    if not json_equal(previous, current):
        dialect = session.get_bind().dialect
        native_json = dialect.name == "mysql" and not getattr(dialect, "is_mariadb", False)
        patch = json_delta(previous, current) if native_json else None
        if patch is None:
            values["payload"] = current
        else:
            sets, removes = patch
            expression = table.c.payload
            if removes:
                expression = func.json_remove(expression, *removes)
            if sets:
                args = []
                for path, value in sets:
                    # Explicit String prevents SQLAlchemy's JSON bind processor
                    # from encoding the JSON text a second time (null -> "null").
                    encoded = literal(json.dumps(value, ensure_ascii=False, allow_nan=False), type_=String)
                    args.extend((path, cast(encoded, JSON)))
                expression = func.json_set(expression, *args)
            values["payload"] = expression
    session.execute(update(table).where(table.c.id == 1).values(**values))
