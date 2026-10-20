"""Explainable spot-long/perpetual-short research, not execution or alerts.

Rates are percentages of the spot reference notional, not account ROI.
Funding scenarios are assumptions; grades are priorities, never probabilities.
"""
import math

POLICY = dict(version=1, spot_fee=.20, futures_fee=.10, slippage=.10,
              min_net=.10, priority_net=.30, stable_range=.15,
              active_range=.40, active_peak=1.0, baseline_daily=.03)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def baseline(hours):
    return .03 * hours / 24 if number(hours) and 0 < hours <= 24 else None


def funding_profile(events, now, interval):
    """Adjacent settlement cadence is inferred, never silently backfilled.

    Missing/irregular cadence is unknown, not a low-insurance observation.
    Keep signed totals: an exceptional positive print cannot hide negative ones.
    """
    events = sorted({int(t): r for t, r in events if number(t) and number(r)
                     and now-48*3600 < t <= now}.items())
    recent = [(t, r) for t, r in events if t > now-86400]
    classified = []
    for (previous, _), (t, rate) in zip(events, events[1:]):
        hours = (t-previous)/3600
        if t > now-86400 and hours in (1, 2, 4, 8):
            classified.append((t, rate, baseline(hours)))
    positive = sum(r > 0 for _, r in recent)
    above = [(t, r) for t, r, base in classified if r > base+1e-6]
    complete = bool(events and recent and number(interval) and interval > 0
                    and events[0][0] <= now-86400
                    and now-recent[-1][0] <= interval*3600+120
                    and len(classified) == len(recent)
                    and all((b[0]-a[0]) <= interval*3600+120
                            for a, b in zip(events, events[1:]) if b[0] > now-86400))
    return dict(count=len(recent), above=len(above), positive=positive,
                baseline_count=sum(abs(r-b) <= 1e-6 for _, r, b in classified),
                unknown_count=len(recent)-len(classified),
                total=sum(r for _, r in recent) if recent else None,
                previous=recent[-1][1] if recent else None,
                last_at=recent[-1][0] if recent else None,
                last_above_at=above[-1][0] if above else None,
                complete=complete, cadence="相邻实际结算间隔推断")


def quote_metrics(live, now):
    if not live:
        return None
    long, short = live.get("long", {}), live.get("short", {})
    if any(not number(leg.get(k)) or leg[k] <= 0 for leg in (long, short) for k in ("bid", "ask")):
        return None
    if any(leg["bid"] > leg["ask"] for leg in (long, short)):
        return None
    stamps = [live.get(k) for k in ("long_at", "short_at")]
    if any(not number(t) or not -2 <= now-t <= 4 for t in stamps) or abs(stamps[0]-stamps[1]) > 2:
        return None
    quantities = [leg.get(k) for leg in (long, short) for k in ("bid_qty", "ask_qty")]
    capacity = min(quantities)*.5*long["ask"] if all(number(q) and q > 0 for q in quantities) else None
    opening, closing = 100*(short["bid"]/long["ask"]-1), 100*(short["ask"]/long["bid"]-1)
    if not number(opening) or not number(closing):
        return None
    return dict(open_spread=opening, close_spread=closing,
                capacity=capacity if number(capacity) else None,
                at=min(stamps), long=long, short=short)


def classify(row, funding, history, live, now, notional=1000, hold_hours=72):
    """Return a path-local candidate, retaining flat positive gaps at grade C."""
    if row.get("price_divergence") or row.get("is_delisting"):
        return None
    quote = quote_metrics(live, now)
    opening = quote["open_spread"] if quote else row.get("open_spread")
    closing = quote["close_spread"] if quote else row.get("close_spread")
    rate = quote["short"].get("funding") if quote else row.get("funding_rate")
    interval = quote["short"].get("interval") if quote else row.get("funding_interval_hours")
    base = baseline(interval)
    history = history or {}
    fresh_history = number(history.get("computed_at")) and 0 <= now-history["computed_at"] <= 360
    ready = fresh_history and history.get("ready", False)
    active = (ready and history.get("range", 0) >= POLICY["active_range"]
              and history.get("max_abs", 0) >= POLICY["active_peak"])
    stable = ready and history.get("range", math.inf) <= POLICY["stable_range"]
    rate_fresh = (quote and number(quote["short"].get("funding_at"))
                  and 0 <= now-quote["short"]["funding_at"] <= 600)
    higher = number(rate) and base is not None and rate > base+1e-6
    recent_above = (number(funding.get("last_above_at"))
                    and now-funding["last_above_at"] <= 12*3600
                    and number(funding.get("previous")) and funding["previous"] > 0
                    and number(funding.get("total")) and funding["total"] > 0)
    funding_watch = higher or recent_above
    spread_watch = active or (number(opening) and opening >= .30)
    if not funding_watch and not spread_watch:
        return None
    reasons, risks, notes = [], [], []
    drivers = []
    if funding_watch:
        drivers.append("资费")
        if higher:
            reasons.append(f"本期预计 {rate:+.4f}% / {interval:g}H，高于基准 {base:.4f}%")
        if funding.get("above"):
            reasons.append(f"近24H已结算 {funding['count']} 期，其中 {funding['above']} 期高于基准")
    if spread_watch:
        drivers.append("价差")
        if active:
            reasons.append(f"近24H开差 {history['low']:+.2f}%～{history['high']:+.2f}%，存在价差波动")
        elif stable:
            reasons.append(f"已采区间开差接近 {history['median']:+.2f}%，常态溢价；等待更好的开平窗口")
        else:
            reasons.append("保留正价差观察，尚不能把当前开差直接当作收益")
    if stable and not funding_watch:
        reasons.append("资费未明显脱离基准，低优先观察，不假定价差会归零")
    change = row.get("change_24h")
    trend = "走势待积累"
    if number(change):
        trend = "价格走强" if change > 0 else "回撤正资费观察" if funding_watch else "价格回撤"
    if not quote:
        risks.append("盘口过期或不同步，等待新报价")
    if quote and quote["capacity"] is None:
        risks.append("首档数量或单位未知")
    elif quote and quote["capacity"] < notional:
        risks.append("首档容量不足参考金额")
    if not live or live.get("transfer_state") != "normal":
        risks.append("充提受限" if live and live.get("transfer_state") == "restricted" else "充提未知或过期")
    if not live or not number(live.get("metadata_at")) or not 0 <= now-live["metadata_at"] <= 60:
        risks.append("路径元数据待刷新")
    if not rate_fresh or base is None:
        risks.append("资费报价待刷新")
    if row.get("listing_review"):
        risks.append("本路径近期有下架公告，先核验产品状态")
    if not ready or not history.get("close_ready"):
        risks.append("平差历史积累中，不能确认当前净空间")
    exit_gap = max(0, history["exit_q75"]) if ready and history.get("close_ready") else None
    spread_net = funding_net = funding_scenario = fee_budget = None
    if quote and exit_gap is not None:
        # Equal base quantity. Scenario fixes spot exit price to today's ask,
        # futures exit to that price × historical q75 closing spread. No credit
        # for negative exit discounts; actual prices/funding can invalidate it.
        fee_budget = 1.1*(2*POLICY["spot_fee"] +
                          2*POLICY["futures_fee"]*quote["short"]["ask"]/quote["long"]["ask"])
        adverse = (max(0, -rate)*(math.floor(hold_hours/interval)+1)*1.1*
                   quote["short"]["ask"]/quote["long"]["ask"]
                   if rate_fresh and base is not None else 0)
        spread_net = opening-exit_gap*1.1-fee_budget-POLICY["slippage"]-adverse
        if (rate_fresh and base is not None and funding.get("complete")
                and number(funding.get("total"))):
            # No perpetuating a single high forecast: cap at actual signed 24H,
            # and halve the scenario. None is not zero income.
            funding_scenario = (max(0, min(rate*24/interval, funding["total"]))*hold_hours/24*.5
                                * min(1, quote["short"]["bid"]/quote["long"]["ask"]))
            funding_net = spread_net+funding_scenario
    if funding_watch and not funding.get("complete"):
        # Incomplete funding must not veto an independently qualified spread.
        notes.append("结算历史覆盖不足，资费仅作观察，不计入净空间")
    current = not risks and max(v for v in (spread_net, funding_net, -math.inf) if v is not None) >= POLICY["min_net"]
    net = max((v for v in (spread_net, funding_net) if v is not None), default=None)
    grade = "B" if current or funding_watch or active else "C"
    if current and net >= POLICY["priority_net"]:
        grade = "A"
    if not quote or (live and live.get("transfer_state") == "restricted"):
        grade = "C"
    if current:
        reasons.append("当前盘口与历史退出情景扣除费用预算后仍有空间")
    elif active and number(opening) and number(history.get("median")) and opening <= history["median"]+.10:
        reasons.append("目前接近常态，等待下一次有利的开仓位置")
    return dict(grade=grade, grade_label={"A": "优先", "B": "关注", "C": "低优先观察"}[grade],
                status="opportunity" if current else "watch",
                status_label="当前有机会" if current else "值得观察",
                reasons=reasons, risks=risks, notes=notes, drivers=drivers, trend=trend,
                open_spread=opening, close_spread=closing, funding_current=rate,
                funding=funding, history=history,
                capacity_usdt=quote["capacity"] if quote else None,
                quoted_at=quote["at"] if quote else None,
                scenario=dict(net=net, spread_net=spread_net, funding_net=funding_net,
                              funding_income=funding_scenario, exit_spread=exit_gap,
                              fee_budget=fee_budget, slippage=POLICY["slippage"],
                              notional=notional, hold_hours=hold_hours,
                              assumptions="现货退出价格不变，平差取近24H的75分位；资费按当前与已结算24H较低值折半。费用为预算，不是账户费率或收益承诺。"))
