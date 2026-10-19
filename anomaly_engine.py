"""Path-local pulse detection. Pure Python: no network, database, or trading API.

All changes are percentage *points*, except the log-price metric. Detection is
independent of notification cadence. A retracement updates a pulse; it never
undoes it. New pulses use the post-retracement turning point, not an old high.
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timezone
import math
import uuid


POLICY_VERSION = 3
RULES = {"spread": ((15, .50), (30, .75), (120, 1.0)),
         "basis": ((15, .50), (30, .75), (120, 1.0)),
         "price": ((15, 1.5), (30, 2.5), (120, 4.0))}


def motion_threshold(metric, baseline, value, age):
    """Magnitude AND displacement; never make tiny zero-crossings actionable.

    Preserve the user's early positive-spread case without a blanket 1% gate:
    a near-zero anchor reaching +.5 can qualify before a full .5-point change.
    """
    if metric not in RULES or not all(finite(v) for v in (baseline, value, age)) or not 0 < age <= 300:
        return None
    if metric != "price" and max(abs(baseline), abs(value)) < .5 - 1e-9:
        return None
    if metric == "spread" and abs(baseline) <= .15 and value >= .5 - 1e-9 and value - baseline >= .4 - 1e-9:
        return .4
    return next((limit for seconds, limit in RULES[metric] if age <= seconds), 4.0 if metric == "price" else 1.0)


def pulse_is_actionable(pulse):
    """Apply the current policy to stored evidence without rewriting history.

    A later retracement cannot revoke a qualified trigger or observed peak.
    Missing quote evidence cannot be promoted into a confirmed displacement.
    """
    metric = pulse.get("metric")
    for value_key, time_key, context_key in (("trigger", "triggered_at", "trigger_context"), ("peak", "peak_at", "peak_context")):
        try:
            baseline = Sample(pulse["baseline_at"], pulse["baseline"], pulse["baseline_context"])
            sample = Sample(pulse[time_key], pulse[value_key], pulse[context_key])
            limit = motion_threshold(metric, baseline.value, sample.value, sample.at - baseline.at)
            if limit is not None and pulse.get("ordinal", 1) > 1:
                limit = max(limit, RULES[metric][0][1])
            if limit is not None and abs(sample.value - baseline.value) >= limit - 1e-9 and PulseDetector.supported(metric, baseline, sample, limit):
                return True
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return False


def alert_round_ids(events, pulses):
    """Correlate counts only; never combine path-local prices or quarantine.

    Restarts in one episode count once. New episodes starting within 30s of a
    coin round's FIRST onset join it; later arrivals don't slide the window.
    """
    result, episodes, recent = {}, {}, {}
    for item in sorted(events, key=lambda item: (item.created_at, item.id)):
        pulse = pulses.get(item.id)
        if not pulse:
            result[item.id] = f"legacy:{item.id}"
            continue
        episode_key = (item.symbol, item.strategy, item.long_exchange, item.short_exchange,
                       pulse.get("metric"), pulse.get("episode") or pulse["id"])
        round_id = episodes.get(episode_key)
        if round_id is None:
            stamp = item.created_at.replace(tzinfo=timezone.utc).timestamp()
            previous = recent.get(item.symbol)
            if previous and 0 <= stamp - previous[0] <= 30:
                round_id = previous[1]
            else:
                round_id = f"pulse:{item.id}"
                recent[item.symbol] = (stamp, round_id)
            episodes[episode_key] = round_id
        result[item.id] = round_id
    return result


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(slots=True)
class Sample:
    at: float
    value: float
    context: dict


@dataclass
class Track:
    # One-second buckets retain BOTH extrema and their original timestamps.
    # Active pulses are evaluated on EVERY tick before any history compaction.
    history: deque = field(default_factory=deque)
    pulse: dict | None = None
    turning: Sample | None = None
    last: Sample | None = None
    ordinal: int = 0
    episode: str = ""
    anchor: Sample | None = None
    anchor_direction: int = 0
    low: float | None = None
    high: float | None = None

    def clear_history(self):
        self.history.clear()
        self.low = self.high = None

    def possible_change(self, value):
        # A conservative envelope can reject an impossible threshold in O(1).
        # Any potentially crossing tick still runs the original chronological
        # scan and the same two-sided quote validation.
        return max(abs(value - self.low), abs(value - self.high)) if self.history else 0

    def remember(self, sample, metric):
        # Idle history needs only the fields used for displacement validation.
        # Do not retain four large quote contexts per second for every path.
        # Trigger, peak and current pulse evidence still retain full contexts.
        fields = {"spread": ("close_spread", "mid_spread"), "price": ("long_mid", "short_mid")}.get(metric, ())
        sample = Sample(sample.at, sample.value, {name: sample.context[name] for name in fields})
        if self.history and all(item.value == sample.value and item.context == sample.context for item in self.history[-1]):
            # A flat plateau needs only its latest real observation. Earlier
            # different extrema remain; this never coalesces a peak with a dip.
            self.history[-1] = [sample, sample]
        elif not self.history or int(self.history[-1][0].at) != int(sample.at):
            self.history.append([sample, sample])
        else:
            bucket = self.history[-1]
            if sample.value < bucket[0].value:
                bucket[0] = sample
            if sample.value > bucket[1].value:
                bucket[1] = sample
        self.low = sample.value if self.low is None else min(self.low, sample.value)
        self.high = sample.value if self.high is None else max(self.high, sample.value)
        expired_extreme = False
        while self.history and sample.at - self.history[0][1].at > 120:
            expired = self.history.popleft()
            expired_extreme |= any(item.value in (self.low, self.high) for item in expired)
        if expired_extreme:
            self.low = min(item.value for bucket in self.history for item in bucket)
            self.high = max(item.value for bucket in self.history for item in bucket)
        self.last = sample


class PulseDetector:
    def __init__(self):
        self.tracks = {}

    def reset(self, path, at, reason="data_gap"):
        updates = []
        for key in [key for key in self.tracks if key[0] == path]:
            track = self.tracks.pop(key)
            if track.pulse:
                track.pulse.update(state=reason, ended_at=at, version=track.pulse["version"] + 1)
                updates.append(deepcopy(track.pulse))
        return updates

    @staticmethod
    def supported(metric, baseline, sample, threshold):
        if metric == "spread":
            # A widening bid/ask alone is not evidence of a price displacement.
            delta = sample.value - baseline.value
            closing = sample.context["close_spread"] - baseline.context["close_spread"]
            middle = sample.context["mid_spread"] - baseline.context["mid_spread"]
            return delta * closing > 0 and abs(closing) >= threshold * .6 and abs(middle) >= threshold * .7
        if metric == "price":
            long_change = 100 * math.log(sample.context["long_mid"] / baseline.context["long_mid"])
            short_change = 100 * math.log(sample.context["short_mid"] / baseline.context["short_mid"])
            return long_change * short_change > 0 and min(abs(long_change), abs(short_change)) >= threshold * .6
        return True

    def observe(self, path, metric, value, at, context):
        if metric not in RULES or not finite(value) or not finite(at):
            return []
        key = (path, metric)
        track = self.tracks.setdefault(key, Track())
        if track.last and at <= track.last.at:
            return []
        # Quantity-only book updates cannot introduce a new price extremum.
        # Still tick once a second for expiry/current freshness and re-arming.
        if track.last and value == track.last.value and (metric != "spread" or context.get("close_spread") == track.last.context.get("close_spread")) and at - track.last.at < 1:
            return []
        updates = []
        # Never bridge a disconnected/stale interval with a new jump alarm.
        if track.last and at - track.last.at > 10:
            if track.pulse:
                track.pulse.update(state="data_gap", ended_at=at, version=track.pulse["version"] + 1)
                updates.append(deepcopy(track.pulse))
            track = self.tracks[key] = Track()
        sample = Sample(at, value, dict(context))
        screen_reference = sample.context.pop("screen_reference", {})
        context = sample.context
        pulse = track.pulse
        baseline = None
        threshold = RULES[metric][0][1]
        if pulse:
            direction = pulse["direction"]
            prior_state = pulse["state"]
            pulse["current"], pulse["current_at"], pulse["current_context"] = value, at, dict(context)
            for name in ("open_spread", "basis"):
                observed = context.get(name)
                if finite(observed):
                    bounds = pulse["ranges"].setdefault(name, {"min": observed, "max": observed})
                    bounds["min"], bounds["max"] = min(bounds["min"], observed), max(bounds["max"], observed)
            if pulse["state"] == "retraced":
                if track.turning is None or direction * (value - track.turning.value) < 0:
                    track.turning = sample
                if direction * (value - track.turning.value) >= threshold - 1e-9:
                    baseline = track.turning  # restart can be LOWER than the old peak
                elif direction * (value - pulse["baseline"]) <= -threshold:
                    baseline = Sample(pulse["baseline_at"], pulse["baseline"], pulse["baseline_context"])
                elif at - pulse["retrace_at"] >= 180:
                    pulse["state"], pulse["ended_at"] = "settled", at
                    track.pulse, track.ordinal, track.episode = None, 0, ""
                    track.clear_history()
            else:
                if direction * (value - pulse["peak"]) > 0:
                    pulse["peak"], pulse["peak_at"], pulse["peak_context"] = value, at, dict(context)
                excursion = abs(pulse["peak"] - pulse["baseline"])
                if direction * (pulse["peak"] - value) >= max(threshold * .6, excursion * .5) - 1e-9:
                    pulse.update(state="retraced", retrace_at=at)
                    track.turning = sample
                    if direction * (value - pulse["baseline"]) <= -threshold:
                        baseline = Sample(pulse["baseline_at"], pulse["baseline"], pulse["baseline_context"])
                elif at - pulse["peak_at"] >= 300:
                    pulse["state"], pulse["ended_at"] = "settled", at
                    track.pulse, track.ordinal, track.episode = None, 0, ""
                    track.clear_history()
                elif excursion >= (1 if metric != "price" else 3):
                    pulse["state"] = "expanded"
            if pulse["state"] != prior_state and pulse["state"] in ("retraced", "expanded"):
                pulse["notice_version"] += 1
                pulse["noticed_at"] = at
            pulse["version"] += 1
            updates.append(deepcopy(pulse))
            if baseline:
                limit = motion_threshold(metric, baseline.value, value, at - baseline.at)
                threshold = max(threshold, limit) if limit is not None else threshold
                if limit is None or abs(value - baseline.value) < threshold - 1e-9 or not self.supported(metric, baseline, sample, threshold):
                    baseline = None
            if baseline:
                pulse["ended_at"] = at
                updates[-1] = deepcopy(pulse)
        else:
            unchanged = track.last and value == track.last.value
            if metric == "spread":
                unchanged = unchanged and context.get("close_spread") == track.last.context.get("close_spread")
            elif metric == "price":
                unchanged = unchanged and all(context.get(name) == track.last.context.get(name) for name in ("long_mid", "short_mid"))
            if unchanged:
                track.remember(sample, metric)
                return updates
            # Compare signed displacement, including crossing zero. Choose a
            # chronological extremum WITHIN the stated window, never 65s ago.
            candidates = []
            possible_change = track.possible_change(value)
            minimum = .4 if metric == "spread" else RULES[metric][0][1]
            for bucket in track.history if possible_change >= minimum - 1e-9 else ():
                for previous in bucket:
                    age = at - previous.at
                    if not 0 < age <= 120:
                        continue
                    # The shortest eligible window always has the smallest
                    # threshold and strongest score/support. Scan once, not 3x.
                    limit = motion_threshold(metric, previous.value, value, age)
                    change = abs(value - previous.value)
                    if limit is not None and change >= limit - 1e-9 and self.supported(metric, previous, sample, limit):
                        candidates.append((change / limit, previous, limit))
            if candidates:
                _, baseline, threshold = max(candidates, key=lambda item: item[0])
            # Freeze a pre-alert anchor after a small directional departure.
            # Otherwise a slow 0 -> .5 ramp can be absorbed forever by rolling
            # windows. This anchor is bounded, resets on recovery, and is not an
            # absolute-level alarm on a permanently dislocated price.
            anchor_step = .1 if metric != "price" else .5
            if track.anchor and (at - track.anchor.at > 300 or track.anchor_direction * (value - track.anchor.value) < anchor_step * .5):
                track.anchor = None
            if track.anchor is None and possible_change >= anchor_step:
                departures = [previous for bucket in track.history for previous in bucket
                              if 0 < at - previous.at <= 120 and abs(value - previous.value) >= anchor_step
                              and self.supported(metric, previous, sample, anchor_step)]
                if departures:
                    track.anchor = max(departures, key=lambda previous: abs(value - previous.value))
                    track.anchor_direction = 1 if value > track.anchor.value else -1
            cumulative = motion_threshold(metric, track.anchor.value, value, at - track.anchor.at) if track.anchor else None
            if track.anchor and cumulative is not None and abs(value - track.anchor.value) >= cumulative - 1e-9 and self.supported(metric, track.anchor, sample, cumulative) and (baseline is None or abs(value - track.anchor.value) / cumulative > abs(value - baseline.value) / threshold):
                baseline, threshold = track.anchor, cumulative
        if baseline:
            old_reference = pulse.get("screen_reference", {}) if pulse else {}
            if old_reference and finite(old_reference.get("to_at")) and 0 <= at-old_reference["to_at"] <= 300:
                screen_reference = old_reference
            track.ordinal += 1
            track.episode = track.episode or uuid.uuid4().hex
            ranges = {name: {"min": context[name], "max": context[name]} for name in ("open_spread", "basis") if finite(context.get(name))}
            pulse = dict(id=uuid.uuid4().hex, episode=track.episode, ordinal=track.ordinal,
                         metric=metric, path=list(path), direction=1 if value > baseline.value else -1,
                         baseline=baseline.value, baseline_at=baseline.at, baseline_context=baseline.context,
                         trigger=value, triggered_at=at, trigger_context=dict(context),
                         peak=value, peak_at=at, peak_context=dict(context), current=value,
                         current_at=at, current_context=dict(context), threshold=threshold,
                         state="expanded" if abs(value - baseline.value) >= (1 if metric != "price" else 3) else "watch",
                         policy_version=POLICY_VERSION,
                         screen_reference=deepcopy(screen_reference),
                         ranges=ranges, version=1, notice_version=1, noticed_at=at, quality="aligned_book" if metric != "basis" else "mark_index")
            track.pulse, track.turning = pulse, None
            track.anchor = None
            track.clear_history()
            updates.append(deepcopy(pulse))
        track.remember(sample, metric)
        return updates
