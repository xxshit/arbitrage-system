"""Conservative PUBLIC-quote screening, never account permissions or execution.

Capture and attention are deliberately separate. A pulse is retained even when
it is unsuitable for opening a hedge. All percentages here are percentage points.
"""
from collections import deque
from dataclasses import dataclass, field
import math
from statistics import median

from anomaly_engine import finite


DEFAULT_NOTIONAL = 1000.0
MIN_NET_EDGE = .30
# Deliberately explicit budgets, NOT a claim about the user's exchange/VIP fees.
SPOT_FEE = .20
FUTURES_FEE = .10
SLIPPAGE_BUFFER = .10
HOLD_HOURS = 8


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[math.ceil((len(ordered) - 1) * fraction)]


def inverse_spread(value):
    return 100 * (1 / (1 + value / 100) - 1)


@dataclass
class NoiseWindow:
    # One small tuple per five seconds, at most five minutes PER PATH, not per
    # metric. Exclude the latest 15s so a triggering spike cannot set its own bar.
    samples: deque = field(default_factory=lambda: deque(maxlen=61))
    cached: dict = field(default_factory=dict)
    checked_at: float = 0
    last_at: float | None = None

    def observe(self, at, context):
        if self.last_at is not None and at - self.last_at > 10:
            self.samples.clear()
            self.cached = {}
            self.checked_at = 0
        self.last_at = at
        if not self.samples or at - self.samples[-1][0] >= 5:
            opening, closing = context["open_spread"], context["close_spread"]
            self.samples.append((at, opening, closing))
        if at - self.checked_at < 5:
            return self.cached
        self.checked_at = at
        prior = [row for row in self.samples if 15 <= at - row[0] <= 300]
        if len(prior) < 12 or prior[-1][0] - prior[0][0] < 55:
            self.cached = {"ready": False, "samples": len(prior)}
            return self.cached
        forward_open = [row[1] for row in prior]
        forward_close = [row[2] for row in prior]
        reverse_open = [inverse_spread(row[2]) for row in prior]
        reverse_close = [inverse_spread(row[1]) for row in prior]
        def profile(opens, closes):
            moves = [abs(opens[i] - opens[i-3]) for i in range(3, len(opens))]
            width = median(max(0, c-o) for o, c in zip(opens, closes))
            return {"open": median(opens), "exit": percentile(closes, .75),
                    "noise": percentile(moves, .90), "width": width,
                    "floor": max(.40, 3 * percentile(moves, .90), 2 * width)}
        self.cached = {"ready": True, "samples": len(prior), "from_at": prior[0][0],
                       "to_at": prior[-1][0], "forward": profile(forward_open, forward_close),
                       "reverse": profile(reverse_open, reverse_close)}
        return self.cached


def signal_direction(pulse):
    """A negative spot gap is not a spot-long entry; futures can swap BOTH legs."""
    if pulse.get("metric") != "spread":
        return None
    if pulse.get("direction") == 1:
        return "forward"
    if pulse.get("path", [None])[0] == "futures_futures":
        return "reverse"
    return None


def watch_baseline(pulse, profile, direction):
    # Preserve the frozen near-zero anchor on a slow 0 -> +.5 ramp. A rolling
    # median must not absorb the event itself. The adaptive NOISE floor remains.
    baseline = pulse.get("baseline")
    if direction == "forward" and finite(baseline) and abs(baseline) <= .15:
        return min(profile["open"], baseline)
    return profile["open"]


def pulse_has_watch_peak(pulse):
    direction = signal_direction(pulse)
    reference = pulse.get("screen_reference", {})
    profile = reference.get(direction, {})
    peak = pulse.get("peak") if direction == "forward" else pulse.get("peak_close")
    if not direction or not reference.get("ready") or not all(finite(v) for v in (peak, profile.get("open"), profile.get("floor"))):
        return False
    if direction == "reverse":
        peak = inverse_spread(peak)
    return peak >= .5 - 1e-9 and peak - watch_baseline(pulse, profile, direction) >= profile["floor"] - 1e-9


def okx_contract_unit(instrument):
    """OKX bidSz/askSz are contracts; ctVal is base units per linear contract."""
    try:
        unit = float(instrument["ctVal"])
        base = instrument["instId"].split("-")[0]
        if instrument.get("ctType") == "linear" and instrument.get("settleCcy") == "USDT" and instrument.get("ctValCcy") == base and finite(unit) and unit > 0:
            return unit
    except (KeyError, ValueError, TypeError):
        pass
    return None  # unknown never means one base coin per contract


def screen_pulse(pulse, live, now, notional=DEFAULT_NOTIONAL):
    result = {"tier": "record", "reason": "仅异动记录", "live": False, "net_edge": None}
    direction = signal_direction(pulse)
    if direction is None:
        result["reason"] = "非本方向开仓信号"
        return result
    reference = pulse.get("screen_reference", {})
    profile = reference.get(direction, {})
    if not reference.get("ready") or not all(finite(profile.get(k)) for k in ("open", "exit", "floor")):
        result["reason"] = "基线积累中" if reference else "历史未记录筛选基线"
        return result
    if not finite(reference.get("to_at")) or not 0 <= now-reference["to_at"] <= 600:
        result["reason"] = "本轮参考基线已过期"
        return result
    if not live or any(not finite(live.get(k)) or not -2 <= now-live[k] <= 4 for k in ("long_at", "short_at")) or abs(live["long_at"]-live["short_at"]) > 2:
        result["reason"] = "盘口过期或不同步"
        return result
    if not finite(pulse.get("current_at")) or not 0 <= now-pulse["current_at"] <= 10:
        result["reason"] = "异动记录已过期"
        return result
    if pulse.get("ended_at") or pulse.get("state") in ("data_gap", "isolated", "settled"):
        result["reason"] = "本轮已结束"
        return result
    strategy, _, long_exchange, short_exchange = pulse["path"]
    long, short = live.get("long", {}), live.get("short", {})
    if direction == "reverse":
        long, short = short, long
        long_exchange, short_exchange = short_exchange, long_exchange
    if any(not finite(leg.get(k)) or leg[k] <= 0 for leg in (long, short) for k in ("ask", "bid")):
        result["reason"] = "盘口缺失"
        return result
    opening = 100 * (short["bid"] / long["ask"] - 1)
    result.update(direction=direction, long_exchange=long_exchange, short_exchange=short_exchange,
                  entry_spread=opening, exit_assumption=profile["exit"], noise_floor=profile["floor"],
                  notional=notional, quoted_at=min(live["long_at"], live["short_at"]))
    # No near-zero narrowing, static positive premium, or ordinary noisy range.
    if opening < .5 - 1e-9 or opening - watch_baseline(pulse, profile, direction) < profile["floor"] - 1e-9:
        result["reason"] = "当前空间已收窄" if opening < .5 else "未超出本路径常态波动"
        return result
    result.update(tier="watch", reason="提前关注，尚非套利候选", live=True)
    if strategy == "spot_futures" and live.get("transfer_state") != "normal":
        result["reason"] = "充提受限" if live.get("transfer_state") == "restricted" else "充提未知或过期"
        return result
    if not finite(notional) or not 100 <= notional <= 1_000_000:
        result["reason"] = "参考金额无效"
        return result
    # Equal underlying exposure, not equal USDT on two differently priced legs.
    # Only use HALF of the quoted top level. If it cannot cover the size, do not
    # invent deeper levels/VWAP. Within this cap the observed entry VWAP is BBO.
    quantity = notional / long["ask"]
    capacities = [leg.get(k) for leg in (long, short) for k in ("ask_qty", "bid_qty")]
    if any(not finite(q) or q <= 0 for q in capacities):
        result["reason"] = "数量单位或盘口深度未知"
        return result
    result["capacity_usdt"] = min(capacities) * .5 * long["ask"]
    if quantity > min(capacities) * .5:
        result["reason"] = "首档深度不足参考金额"
        return result
    if not finite(live.get("metadata_at")) or not 0 <= now-live["metadata_at"] <= 60:
        result["reason"] = "资费或充提元数据过期"
        return result
    rates = [(short, -1)] if strategy == "spot_futures" else [(long, 1), (short, -1)]
    adverse = 0
    for leg, sign in rates:
        rate, interval = leg.get("funding"), leg.get("interval")
        funding_at = leg.get("funding_at")
        if not finite(funding_at) or not 0 <= now-funding_at <= 600 or not finite(rate) or not finite(interval) or not 0 < interval <= 24:
            result["reason"] = "资费缺失"
            return result
        # Include an extra boundary settlement rather than assuming we enter
        # safely after it. Future rates/prices remain assumptions, not promises.
        adverse += max(0, sign * rate) * (math.floor(HOLD_HOURS / interval)+1) * (leg["ask"] / long["ask"]) * 1.1
    long_fee = SPOT_FEE if strategy == "spot_futures" else FUTURES_FEE
    # The exit is a scenario based on the pre-event 75th percentile closing gap,
    # NOT zero convergence or a guaranteed future quote. Apply a 10% notional
    # buffer to four-leg fee budgets; allow no favourable negative exit credit.
    exit_gap = max(0, profile["exit"]) * 1.1
    fees = 1.1 * (2 * long_fee + 2 * FUTURES_FEE * short["ask"] / long["ask"])
    edge = opening - exit_gap - fees - SLIPPAGE_BUFFER - adverse
    result.update(net_edge=edge, exit_assumption=exit_gap, fee_budget=fees,
                  slippage_budget=SLIPPAGE_BUFFER, funding_budget=adverse, hold_hours=HOLD_HOURS)
    if edge < MIN_NET_EDGE - 1e-9:
        result["reason"] = "扣成本后空间不足0.30%"
        return result
    result.update(tier="candidate", reason="条件候选，回归及成交未保证")
    return result
