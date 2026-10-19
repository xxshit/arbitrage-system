"""Durable 24-hour range admission, separate from raw pulse detection.

The caller commits state and pulse evidence in ONE transaction. No network,
orders, notifications or wall-clock side effects live here. Bounds belong to a
directed strategy/path AND metric; upper and lower edges are compared separately.
"""
from copy import deepcopy
import hashlib
import json

from anomaly_engine import finite, pulse_is_actionable


WINDOW = 24 * 60 * 60
REMINDER_POLICY = 1


def reminder_scope(pulse):
    return [*pulse["path"], pulse["metric"]]


def reminder_state_id(pulse):
    return hashlib.sha256(json.dumps(reminder_scope(pulse), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def breakthrough_step(metric, edge):
    # Price is an absolute log-price, not a spread. Never take 20% of log(price).
    magnitude = abs(edge["value"] - edge["origin"]) if metric == "price" else abs(edge["value"])
    return max(.6 if metric == "price" else .20, .20 * magnitude)


def admit_pulse(state, pulse, previous=None, *, seeded=False):
    """Return new state and pulse metadata; repeated/out-of-order writes are inert.

    Only an actual NEW observed peak can produce a ticket. Elapsing 24h, a phase
    change, retracement, new episode, retry, or a restart alone cannot do so.
    Checkpoints remember admitted levels, NOT every minor observed high. The
    first baseline seeds the opposite boundary so an ordinary pullback through
    the already-seen interval cannot become an independent downward alert.
    """
    state = deepcopy(state or {"scope": reminder_scope(pulse), "points": []})
    metadata = deepcopy((previous or {}).get("reminder") or {"policy": REMINDER_POLICY, "notices": []})
    if state["scope"] != reminder_scope(pulse):
        raise ValueError("reminder scope mismatch")
    if not pulse_is_actionable(pulse) or pulse.get("state") == "isolated":
        return state, metadata
    if previous and pulse.get("peak_at", 0) <= previous.get("peak_at", 0):
        return state, metadata
    at, value, baseline = pulse.get("peak_at"), pulse.get("peak"), pulse.get("baseline")
    direction = pulse.get("direction")
    if not all(finite(v) for v in (at, value, baseline)) or direction not in (-1, 1):
        return state, metadata
    latest_at = max(at, state.get("last_peak_at", 0))
    if at < latest_at - WINDOW:
        return state, metadata
    state["last_peak_at"] = latest_at
    # A fair queue may deliver another pulse's later peak first. Compare a
    # late peak against ALL already-known bounds; don't erase it or rewind the
    # range clock. Same-pulse obsolete versions are rejected by the caller.
    points = state["points"] = [p for p in state["points"] if latest_at - WINDOW <= p["at"] <= latest_at]
    first = not points
    if first and at - pulse["triggered_at"] >= WINDOW:
        return state, metadata  # expiry is not a new motion episode
    edge = max(points, key=lambda p: direction * p["value"]) if points else None
    if edge and direction * (value - edge["value"]) < breakthrough_step(pulse["metric"], edge) - 1e-9:
        return state, metadata
    if first:
        # Age this baseline from the first admitted peak; it defines the known
        # interval of that reminder, not a fresh quote at some later expiry.
        points.append({"at": at, "value": baseline, "origin": baseline})
    points.append({"at": at, "value": value, "origin": baseline})
    ticket = {"key": f"range:{pulse['id']}:{len(metadata['notices']) + 1}", "at": at,
              "value": value, "direction": direction, "kind": "first" if first else "breakout"}
    if seeded:
        ticket["seeded"] = True  # migration must not replay historical notices
    metadata["notices"].append(ticket)
    return state, metadata


def current_notice(pulse, now):
    """Latest real admission, never resurrected by stage/quality changes."""
    notices = (pulse or {}).get("reminder", {}).get("notices", [])
    latest = notices[-1] if notices else None
    return latest if latest and not latest.get("seeded") and 0 <= now - latest["at"] <= 120 else None


def reminder_count(pulse, now):
    return sum(0 <= now - ticket["at"] <= WINDOW for ticket in pulse.get("reminder", {}).get("notices", []))
