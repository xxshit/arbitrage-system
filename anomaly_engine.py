"""Path-local pulse detection. Pure Python: no network, database, or trading API.

All changes are percentage *points*, except the log-price metric. Detection is
independent of notification cadence. A retracement updates a pulse; it never
undoes it. New pulses use the post-retracement turning point, not an old high.
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import math
import uuid


RULES = {"spread": ((15, .20), (30, .30), (120, .50)),
         "basis": ((15, .20), (30, .30), (120, .50)),
         "price": ((15, 1.0), (30, 1.5), (120, 3.0))}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
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

    def remember(self, sample):
        if not self.history or int(self.history[-1][0].at) != int(sample.at):
            self.history.append([sample, sample])
        else:
            bucket = self.history[-1]
            if sample.value < bucket[0].value:
                bucket[0] = sample
            if sample.value > bucket[1].value:
                bucket[1] = sample
        while self.history and sample.at - self.history[0][1].at > 120:
            self.history.popleft()
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
                elif at - pulse["retrace_at"] >= 30:
                    pulse["state"], pulse["ended_at"] = "settled", at
                    track.pulse, track.ordinal, track.episode = None, 0, ""
                    track.history.clear()
            else:
                if direction * (value - pulse["peak"]) > 0:
                    pulse["peak"], pulse["peak_at"], pulse["peak_context"] = value, at, dict(context)
                excursion = abs(pulse["peak"] - pulse["baseline"])
                if direction * (pulse["peak"] - value) >= max(threshold * .6, excursion * .5) - 1e-9:
                    pulse.update(state="retraced", retrace_at=at)
                    track.turning = sample
                    if direction * (value - pulse["baseline"]) <= -threshold:
                        baseline = Sample(pulse["baseline_at"], pulse["baseline"], pulse["baseline_context"])
                elif at - pulse["peak_at"] >= 120:
                    pulse["state"], pulse["ended_at"] = "settled", at
                    track.pulse, track.ordinal, track.episode = None, 0, ""
                    track.history.clear()
                elif excursion >= (.5 if metric != "price" else 2):
                    pulse["state"] = "expanded"
            if pulse["state"] != prior_state and pulse["state"] in ("retraced", "expanded"):
                pulse["notice_version"] += 1
                pulse["noticed_at"] = at
            pulse["version"] += 1
            updates.append(deepcopy(pulse))
            if baseline and not self.supported(metric, baseline, sample, threshold):
                baseline = None
            if baseline:
                pulse["ended_at"] = at
                updates[-1] = deepcopy(pulse)
        else:
            # Compare signed displacement, including crossing zero. Choose a
            # chronological extremum WITHIN the stated window, never 65s ago.
            candidates = []
            for seconds, limit in RULES[metric]:
                for bucket in track.history:
                    for previous in bucket:
                        age = at - previous.at
                        change = abs(value - previous.value)
                        if 0 < age <= seconds and change >= limit - 1e-9 and self.supported(metric, previous, sample, limit):
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
            if track.anchor is None:
                departures = [previous for bucket in track.history for previous in bucket
                              if 0 < at - previous.at <= 120 and abs(value - previous.value) >= anchor_step
                              and self.supported(metric, previous, sample, anchor_step)]
                if departures:
                    track.anchor = max(departures, key=lambda previous: abs(value - previous.value))
                    track.anchor_direction = 1 if value > track.anchor.value else -1
            cumulative = .5 if metric != "price" else 3
            if not baseline and track.anchor and abs(value - track.anchor.value) >= cumulative - 1e-9 and self.supported(metric, track.anchor, sample, cumulative):
                baseline, threshold = track.anchor, cumulative
        if baseline:
            track.ordinal += 1
            track.episode = track.episode or uuid.uuid4().hex
            ranges = {name: {"min": context[name], "max": context[name]} for name in ("open_spread", "basis") if finite(context.get(name))}
            pulse = dict(id=uuid.uuid4().hex, episode=track.episode, ordinal=track.ordinal,
                         metric=metric, path=list(path), direction=1 if value > baseline.value else -1,
                         baseline=baseline.value, baseline_at=baseline.at, baseline_context=baseline.context,
                         trigger=value, triggered_at=at, trigger_context=dict(context),
                         peak=value, peak_at=at, peak_context=dict(context), current=value,
                         current_at=at, current_context=dict(context), threshold=threshold,
                         state="expanded" if abs(value - baseline.value) >= (.5 if metric != "price" else 2) else "watch",
                         ranges=ranges, version=1, notice_version=1, noticed_at=at, quality="aligned_book" if metric != "basis" else "mark_index")
            track.pulse, track.turning = pulse, None
            track.anchor = None
            track.history.clear()
            updates.append(deepcopy(pulse))
        track.remember(sample)
        return updates
