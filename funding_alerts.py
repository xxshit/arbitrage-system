"""Public Binance funding evidence. No accounts, execution, or profit estimates.

Rates are percentage points; timestamps are UTC Unix seconds. An indicative
cap touch is never proof of a settled cap or a changed settlement interval.
"""
from copy import deepcopy
import hashlib
import json
import math

WINDOW = 86400
RATE_MAX_AGE = 90
INFO_MAX_AGE = 90
GAP_SECONDS = 180
EPSILON = 1e-8


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def fresh(stamp, now, age):
    return isinstance(stamp, (int, float)) and math.isfinite(stamp) and 0 <= now - stamp <= age


def cap_side(rate, cap, floor):
    if rate is None:
        return None
    # Reject contradictory/out-of-bound data rather than guessing a new limit.
    if cap is not None and cap > 0 and abs(rate - cap) <= EPSILON:
        return "positive"
    if floor is not None and floor < 0 and abs(rate - floor) <= EPSILON:
        return "negative"
    return None


def binance_observations(instruments, rates, info, rate_at, info_at, now):
    """Only a fresh, complete successful metadata response establishes defaults.

    fundingInfo lists adjusted symbols. Absent rows establish the documented
    default 8H interval, NOT a fabricated cap/floor. Consumers exclude equities.
    """
    if not (fresh(rate_at, now, RATE_MAX_AGE) and fresh(info_at, now, INFO_MAX_AGE)):
        return []
    if not isinstance(instruments, list) or not isinstance(rates, list) or not isinstance(info, list):
        return []
    metadata = {}
    for item in info:
        if not isinstance(item, dict) or not item.get("symbol"):
            return []
        hours = number(item.get("fundingIntervalHours"))
        if hours is None or hours <= 0 or hours > 24:
            return []
        metadata[item["symbol"]] = item
    trading = {item.get("symbol") for item in instruments if isinstance(item, dict)
               and item.get("status") == "TRADING" and item.get("contractType") == "PERPETUAL"
               and item.get("quoteAsset") == "USDT"}
    observations = []
    for item in rates:
        if not isinstance(item, dict) or item.get("symbol") not in trading:
            continue
        contract = item["symbol"]
        rate, next_ms, source_ms = (number(item.get(key)) for key in ("lastFundingRate", "nextFundingTime", "time"))
        if rate is None or next_ms is None or source_ms is None or not fresh(source_ms / 1000, now, RATE_MAX_AGE):
            continue
        # A stale pre-settlement tick can otherwise look like a fresh cap touch.
        if not now < next_ms / 1000 <= now + 24 * 3600 + 60:
            continue
        meta = metadata.get(contract)
        cap = number(meta.get("adjustedFundingRateCap")) if meta else None
        floor = number(meta.get("adjustedFundingRateFloor")) if meta else None
        cap, floor = cap * 100 if cap is not None and cap > 0 else None, floor * 100 if floor is not None and floor < 0 else None
        observations.append({"exchange": "Binance", "contract": contract,
                             "symbol": contract[:-4] + "/USDT", "rate": rate * 100,
                             "cap": cap, "floor": floor,
                             "interval": float(meta["fundingIntervalHours"]) if meta else 8,
                             "next_at": next_ms / 1000, "rate_at": min(rate_at, source_ms / 1000),
                             "info_at": info_at, "observed_at": now})
    return observations


def advance(state, observation, settlements=()):
    """Return a new durable state and immutable events, atomically persisted by caller.

    Initial states and changes after a long data gap are silent. Cap touches
    are recorded once per cycle/direction, reminded once per 24H for the same
    limit/interval. An actual 4/8H -> 1H transition is a separate meaningful
    event and is not swallowed by spread-range or cap-touch de-duplication.
    """
    old = state or {}
    now = observation["observed_at"]
    # Monotonic-cache -> wall-time conversion can jitter by microseconds between
    # calls. Tolerate only that conversion noise, not a genuinely older response.
    if old and (now <= old["observed_at"] or observation["rate_at"] + .01 < old["rate_at"] or observation["info_at"] + .01 < old["info_at"]):
        return deepcopy(old), []
    result = deepcopy(old)
    result.update(observation)
    result.setdefault("pending", [])
    result.setdefault("touches", {})
    result["notices"] = {key: stamp for key, stamp in old.get("notices", {}).items() if now - stamp < WINDOW}
    continuous = bool(old) and fresh(old["observed_at"], now, GAP_SECONDS)
    events = []

    def emit(kind, identity, *, notice=False, **evidence):
        event = {"exchange": observation["exchange"], "contract": observation["contract"],
                 "symbol": observation["symbol"], "kind": kind, "observed_at": now,
                 "rate": observation["rate"], "interval": observation["interval"],
                 "cap": observation["cap"], "floor": observation["floor"],
                 "next_at": observation["next_at"], "notice": bool(notice and continuous),
                 "after_gap": bool(old) and not continuous, **evidence}
        key = [observation["exchange"], observation["contract"], kind, identity]
        event["id"] = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
        events.append(event)

    if old and old["next_at"] <= now and old["next_at"] != observation["next_at"]:
        boundary = old["next_at"]
        if 0 <= boundary - old["rate_at"] <= 300 and 0 <= boundary - old["info_at"] <= 300:
            if not any(item["at"] == boundary for item in result["pending"]):
                result["pending"].append({"at": boundary, "cap": old["cap"], "floor": old["floor"],
                                          "interval": old["interval"], "touched": bool(old.get("side")) or any(key.startswith(f"{boundary}:") for key in old.get("touches", {}))})
    result["pending"] = [item for item in result["pending"] if now - item["at"] <= 3600]

    for pending in result["pending"]:
        if pending.get("resolved"):
            continue
        matches = [item for item in settlements if abs(item["at"] - pending["at"]) <= 60 and item["at"] <= now]
        if not matches:
            continue
        settled = min(matches, key=lambda item: abs(item["at"] - pending["at"]))
        side = cap_side(settled["rate"], pending["cap"], pending["floor"])
        pending.update(resolved=True, settled_rate=settled["rate"], side=side)
        result["settled"] = {**pending, "actual_at": settled["at"]}
        if side or pending["touched"]:
            emit("settled_cap" if side else "settled_below_cap", settled["at"],
                 rate=settled["rate"], interval=pending["interval"], cap=pending["cap"], floor=pending["floor"],
                 settlement_at=settled["at"], side=side)

    if old and old["interval"] != observation["interval"] and observation["info_at"] > old["info_at"]:
        change = {"from_interval": old["interval"], "to_interval": observation["interval"],
                  "observed_at": now, "previous_observed_at": old["info_at"], "after_gap": not continuous}
        result["change"] = change
        emit("interval_shortened" if observation["interval"] == 1 else "interval_changed",
             [old["interval"], observation["interval"], observation["info_at"]],
             notice=observation["interval"] == 1, **change)

    side = cap_side(observation["rate"], observation["cap"], observation["floor"])
    result["side"] = side
    result["touches"] = {key: stamp for key, stamp in result["touches"].items() if now - stamp < WINDOW}
    if side:
        limit = observation["cap"] if side == "positive" else observation["floor"]
        touch_key = f"{observation['next_at']}:{side}:{limit}:{observation['interval']}"
        notice_key = f"{side}:{limit}:{observation['interval']}"
        if touch_key not in result["touches"]:
            # Cold start still displays/records the observed cap, without an alarm.
            emit("cap_touch", touch_key, side=side, notice=notice_key not in result["notices"])
            result["touches"][touch_key] = now
            result["notices"].setdefault(notice_key, now)
    return result, events


def status(state, now):
    if not (fresh(state.get("rate_at"), now, RATE_MAX_AGE) and fresh(state.get("info_at"), now, INFO_MAX_AGE)):
        return "stale"
    if state["next_at"] <= now:
        return "settling"
    if state["interval"] == 1:
        return "hourly_cap" if state.get("side") else "hourly"
    # Only the latest known cycle is relevant; an old pending cap must not win.
    pending = max(state.get("pending", []), key=lambda item: item["at"], default=None)
    if pending and pending.get("side") and now - pending["at"] <= 20 * 60:
        return "settled_cap"
    if state.get("side"):
        return "cap_touch"
    if pending and pending.get("touched") and not pending.get("resolved") and now - pending["at"] <= 20 * 60:
        return "settling"
    return "normal"


def event_is_active(event, state, now):
    if not event.get("notice") or not fresh(event.get("observed_at"), now, 120) or status(state, now) == "stale":
        return False
    if event["kind"] == "interval_shortened":
        return state["interval"] == 1 and state.get("change", {}).get("observed_at") == event["observed_at"]
    if event["kind"] == "cap_touch":
        return state.get("side") == event.get("side") and state["next_at"] == event["next_at"] and state["next_at"] > now
    return False
