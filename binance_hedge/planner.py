"""Exact-quantity, price-capped FOK clips; never infer liquidity from a ticker.

No exchange calls or application imports. All monetary quantities cross the API
as decimal strings. A plan is a limit, not a promise of cross-market atomicity.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from math import lcm
import re


class GuardError(Exception):
    pass


def dec(value):
    if len(str(value)) > 64:
        raise GuardError("数值长度超出范围")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise GuardError("数值格式无效") from None
    if not result.is_finite() or not -100 <= result.as_tuple().exponent <= 18 or result.copy_abs() > Decimal("1e18"):
        raise GuardError("数值超出支持范围")
    return result


def text(value):
    return format(dec(value), "f")


def floor(value, step):
    return (dec(value) / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil(value, step):
    return (dec(value) / step).to_integral_value(rounding=ROUND_CEILING) * step


def common_step(a, b):
    # max(step1, step2) is WRONG for steps such as .002 and .003.
    scale = Decimal(10) ** max(-a.as_tuple().exponent, -b.as_tuple().exponent, 0)
    return Decimal(lcm(int(a * scale), int(b * scale))) / scale


def symbol_pair(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{2,20}/USDT", value):
        raise GuardError("首版仅支持同名币种的 Binance 现货 / USDT 永续")
    return value.replace("/", "")


@dataclass(frozen=True)
class Rules:
    symbol: str
    base: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_price: Decimal
    max_price: Decimal
    min_notional: Decimal
    max_notional: Decimal

    @classmethod
    def parse(cls, row, futures=False):
        if row.get("status") != "TRADING" or row.get("quoteAsset") != "USDT":
            raise GuardError("交易对未处于 USDT 正常交易状态")
        if futures and (row.get("contractType") != "PERPETUAL" or row.get("marginAsset") != "USDT"):
            raise GuardError("首版仅支持 USDT 保证金永续合约")
        if not futures and row.get("isSpotTradingAllowed") is not True:
            raise GuardError("该币种不允许普通现货交易")
        if "LIMIT" not in row.get("orderTypes", []) or (futures and "FOK" not in row.get("timeInForce", [])):
            raise GuardError("交易对不支持所需的限价 FOK")
        filters = {f["filterType"]: f for f in row["filters"]}
        if futures and filters.get("POSITION_RISK_CONTROL", {}).get("positionControlSide", "NONE") != "NONE":
            raise GuardError("交易所有持仓方向限制，暂停新开双腿")
        try:
            price, lot = filters["PRICE_FILTER"], filters["LOT_SIZE"]
            minimum = max(dec(filters.get("MIN_NOTIONAL", {}).get("minNotional", filters.get("MIN_NOTIONAL", {}).get("notional", "0"))),
                          dec(filters.get("NOTIONAL", {}).get("minNotional", "0")))
            result = cls(row["symbol"], row["baseAsset"], dec(price["tickSize"]), dec(lot["stepSize"]),
                         dec(lot["minQty"]), dec(lot["maxQty"]), dec(price["minPrice"]), dec(price["maxPrice"]),
                         minimum, dec(filters.get("NOTIONAL", {}).get("maxNotional", "0")))
        except KeyError:
            raise GuardError("交易所过滤规则不完整") from None
        if min(result.tick, result.step, result.min_qty, result.max_qty) <= 0:
            raise GuardError("交易所精度规则无效")
        return result

    def check(self, quantity, price):
        if quantity < self.min_qty or quantity > self.max_qty or quantity % self.step:
            raise GuardError("数量不符合交易所步长或范围")
        if price <= 0 or price % self.tick or (self.min_price and price < self.min_price) or (self.max_price and price > self.max_price):
            raise GuardError("价格不符合交易所步长或范围")
        if quantity * price < self.min_notional or (self.max_notional and quantity * price > self.max_notional):
            raise GuardError("单批金额不符合交易所最小 / 最大名义金额")


@dataclass(frozen=True)
class Book:
    bids: tuple
    asks: tuple
    started: float
    received: float
    update_id: int

    @classmethod
    def parse(cls, payload, started, received):
        sides = []
        for key, reverse in (("bids", True), ("asks", False)):
            rows = tuple((dec(p), dec(q)) for p, q, *_ in payload.get(key, []))
            if not rows or len(rows) > 1000 or any(p <= 0 or q <= 0 for p, q in rows):
                raise GuardError("盘口缺失或包含无效档位")
            prices = [p for p, _ in rows]
            if prices != sorted(set(prices), reverse=reverse):
                raise GuardError("盘口顺序异常")
            sides.append(rows)
        if sides[0][0][0] >= sides[1][0][0] or received < started:
            raise GuardError("盘口交叉或采集时钟异常")
        return cls(*sides, started, received, int(payload["lastUpdateId"]))


def sweep(levels, quantity, cap, buy):
    remaining, cost, levels_used = quantity, Decimal(0), 0
    for price, available in levels:
        if (buy and price > cap) or (not buy and price < cap):
            break
        take = min(remaining, available)
        cost += take * price
        remaining -= take
        levels_used += 1
        if not remaining:
            return cost / quantity, levels_used
    raise GuardError("限价内盘口深度不足")


def capacity(levels, cap, buy):
    return sum((q for p, q in levels if (p <= cap if buy else p >= cap)), Decimal(0))


def validate_books(spot, future, now):
    # REST has no exchange timestamp for Spot depth: bound the whole round trip,
    # not merely the time since a delayed response arrived.
    if any(not 0 <= now - book.started <= 1.0 or book.received - book.started > .4 for book in (spot, future)):
        raise GuardError("盘口超过 1 秒或读取超过 400ms，请重新核验")
    if abs(spot.started - future.started) > .1 or abs(spot.received - future.received) > .4:
        raise GuardError("双腿盘口采集时差过大")


def plan_clip(request, spot_rules, future_rules, spot, future, now, spot_fee, future_fee, anchor=None):
    symbol = symbol_pair(request.get("symbol"))
    if spot_rules.symbol != symbol or future_rules.symbol != symbol or spot_rules.base != future_rules.base:
        raise GuardError("币种单位不一致；倍率合约和别名配对暂不自动下单")
    budget, clip = dec(request.get("quote_amount")), dec(request.get("clip_quote", "20"))
    slip, min_edge = dec(request.get("slippage_bps", "10")), dec(request.get("min_net_bps", "0"))
    if not 10 <= budget <= 10000 or not 5 <= clip <= 1000 or clip > budget:
        raise GuardError("总金额限 10–10000 USDT；单批金额限 5–1000 USDT 且不超过总额")
    if not 0 <= slip <= 50 or not -100 <= min_edge <= 1000:
        raise GuardError("滑点限 0–50bp；最低扣费开差范围无效")
    if not 0 <= spot_fee <= Decimal(".02") or not 0 <= future_fee <= Decimal(".02"):
        raise GuardError("手续费超出支持范围")
    validate_books(spot, future, now)
    buy_cap = floor(spot.asks[0][0] * (1 + slip / 10000), spot_rules.tick)
    sell_cap = ceil(future.bids[0][0] * (1 - slip / 10000), future_rules.tick)
    if anchor:
        buy_cap, sell_cap = min(buy_cap, dec(anchor["buy_cap"])), max(sell_cap, dec(anchor["sell_cap"]))
    if buy_cap < spot.asks[0][0] or sell_cap > future.bids[0][0]:
        raise GuardError("价格已离开确认范围，停止追价")
    step = common_step(spot_rules.step, future_rules.step)
    clip_price = max(buy_cap, future.bids[0][0])
    minimum = ceil(max(spot_rules.min_qty, future_rules.min_qty,
                       spot_rules.min_notional / buy_cap, future_rules.min_notional / sell_cap), step)
    if minimum * clip_price > clip or minimum * buy_cap * (1 + spot_fee) > budget:
        raise GuardError(f"交易所最小共同数量为 {text(minimum)} {spot_rules.base}，本批至少约 {text(ceil(minimum * clip_price, Decimal('.01')))} USDT；请自行调整金额，不会自动放大下单")
    # Consume at most 10% of visible depth within the confirmed price caps.
    quantity = floor(min(budget / (buy_cap * (1 + spot_fee)), clip / clip_price,
                         capacity(spot.asks, buy_cap, True) / 10, capacity(future.bids, sell_cap, False) / 10,
                         spot_rules.max_qty, future_rules.max_qty), step)
    if anchor:
        quantity = min(quantity, dec(anchor["quantity"]))
    spot_rules.check(quantity, buy_cap)
    future_rules.check(quantity, sell_cap)
    buy_vwap, buy_levels = sweep(spot.asks, quantity, buy_cap, True)
    sell_vwap, sell_levels = sweep(future.bids, quantity, sell_cap, False)
    worst_net = (sell_cap * (1 - future_fee) / (buy_cap * (1 + spot_fee)) - 1) * 10000
    if worst_net < min_edge:
        raise GuardError("限价边界扣除双腿手续费后，开差低于确认门槛")
    return dict(symbol=symbol, display_symbol=request["symbol"], base=spot_rules.base, time_in_force="FOK",
                quantity=text(quantity), common_step=text(step), buy_cap=text(buy_cap), sell_cap=text(sell_cap),
                spot_vwap=text(buy_vwap), futures_vwap=text(sell_vwap), spot_levels=buy_levels, futures_levels=sell_levels,
                spot_quote=text(quantity * buy_vwap), futures_quote=text(quantity * sell_vwap),
                worst_net_bps=text(worst_net), spot_fee_rate=text(spot_fee), futures_fee_rate=text(future_fee),
                max_single_leg_quote=text(quantity * clip_price), depth_participation="0.10",
                spot_update_id=spot.update_id, futures_update_id=future.update_id,
                book_skew_ms=round(abs(spot.received-future.received)*1000, 3),
                max_dispatch_skew_ms=50, max_book_age_ms=1000, atomic=False)
