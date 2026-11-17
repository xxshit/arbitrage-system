"""Bounded public spot evidence. No wallets, transfers, accounts or orders.

The broad screen is a liquidity/price screen, NOT a flow measurement. Flow is
measured only for selected symbols using closed exchange one-minute candles.
Taker quote delta is aggressor imbalance, never identifiable investor inflow.
"""
from collections import deque
from dataclasses import dataclass
import hashlib
import math
import threading

POLICY = "spot-flow-v1"
MAX_MARKETS = 2000
MAX_MANUAL = 20
MAX_SCOUTS = 12
MAX_FOCUS = MAX_MANUAL + MAX_SCOUTS
MAX_BARS = 90
EXCLUDED_BASES = frozenset(("USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDE", "USDD", "USDP", "USD1", "EUR", "AEUR"))
LIMITS = dict(manual=MAX_MANUAL, scouts=MAX_SCOUTS, recommendations=3,
              focus=MAX_FOCUS, markets=MAX_MARKETS, minute_bars=MAX_BARS,
              quote_stale_seconds=20, stream_stale_seconds=10,
              volume_ratio=2.5, minimum_5m_quote=100000,
              candidate_ratio=1.5, candidate_24h_quote=10000000,
              candidate_spread_bps=20, alert_cooldown_seconds=600)


def number(value):
    if isinstance(value, bool):
        raise ValueError("invalid_number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("invalid_number")
    return result


def contract(value):
    if not isinstance(value, str):
        raise ValueError("invalid_symbol")
    result = value.strip().upper().replace("/", "")
    if not result.endswith("USDT"):
        result += "USDT"
    if not 5 <= len(result) <= 30 or not result.isalnum():
        raise ValueError("invalid_symbol")
    return result


def catalogue(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("invalid_catalogue")
    rows = {}
    for item in payload["symbols"]:
        if (item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT"
                or item.get("isSpotTradingAllowed") is not True
                or item.get("baseAsset") in EXCLUDED_BASES):
            continue
        symbol = contract(item["symbol"])
        if symbol != item["symbol"] or symbol != item.get("baseAsset", "") + "USDT":
            continue
        rows[symbol] = item["baseAsset"]
    if not rows or len(rows) > MAX_MARKETS:
        raise ValueError("catalogue_empty_or_over_capacity")
    return rows


@dataclass(frozen=True, slots=True)
class Bar:
    start: int
    open: float
    high: float
    low: float
    close: float
    quote: float
    buy: float
    trades: int

    @classmethod
    def parse(cls, row, now):
        if not isinstance(row, (list, tuple)) or len(row) < 11:
            raise ValueError("invalid_candle")
        start, end = number(row[0])/1000, number(row[6])/1000
        values = tuple(number(row[i]) for i in (1, 2, 3, 4, 7, 10, 8))
        o, h, lo, c, q, buy, n = values
        if (not start.is_integer() or int(start) % 60 or end != start + 59.999
                or end > now or min(o, h, lo, c) <= 0 or lo > min(o, c)
                or h < max(o, c) or lo > h or q < 0 or not 0 <= buy <= q
                or n < 0 or not n.is_integer()):
            raise ValueError("invalid_or_unclosed_candle")
        return cls(int(start), o, h, lo, c, q, buy, int(n))


def rest_bars(rows, now):
    if not isinstance(rows, list) or len(rows) > MAX_BARS:
        raise ValueError("invalid_candles")
    result = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 11:
            raise ValueError("invalid_candle")
        if number(row[6])/1000 > now:
            continue  # An open candle is not a completed minute of volume.
        bar = Bar.parse(row, now)
        if bar.start in result:
            raise ValueError("duplicate_candle")
        result[bar.start] = bar
    return result


def ws_bar(payload, now):
    if not isinstance(payload, dict) or payload.get("e") != "kline":
        return None
    k = payload.get("k", {})
    if k.get("i") != "1m" or k.get("s") != payload.get("s"):
        raise ValueError("invalid_stream_symbol")
    at = number(payload["E"])/1000
    if not -2 <= now-at <= 10:
        raise ValueError("stale_stream")
    symbol = contract(payload["s"])
    # Unclosed messages refresh only the connection clock, not the closed bars.
    bar = None
    if k.get("x") is True:
        bar = Bar.parse([k["t"], k["o"], k["h"], k["l"], k["c"], 0,
                         k["T"], k["q"], k["n"], 0, k["Q"]], now)
    return symbol, at, bar


class FlowEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.catalog = {}
        self.catalog_at = 0
        self.market = {}
        self.prices = {}
        self.market_at = 0
        self.book_at = 0
        self.focus = ()
        self.bars = {}
        self.stream_at = {}
        self.seeded_at = {}
        self.live_closed = {}

    def set_catalog(self, payload, now):
        parsed = catalogue(payload)
        with self.lock:
            self.catalog, self.catalog_at = parsed, now
            self.market = {s: v for s, v in self.market.items() if s in parsed}
            self.prices = {s: v for s, v in self.prices.items() if s in parsed}

    def ingest_market(self, tickers, books, at, now, *, book_at=None):
        if not isinstance(tickers, list) or not isinstance(books, list):
            return False
        if not 0 <= now-at <= 45:
            return False
        with self.lock:
            new_book = book_at is not None and self.book_at < book_at <= now and now-book_at <= 20
            if (at <= self.market_at and not new_book) or now-self.catalog_at > 7200:
                return False
            # Copy scalar fields, never retain the market's decoded payload tree.
            quotes = {}
            if new_book:
                for item in books:
                    if item.get("symbol") not in self.catalog:
                        continue
                    try:
                        bid, ask = number(item["bidPrice"]), number(item["askPrice"])
                        if 0 < bid <= ask:
                            quotes[item["symbol"]] = (10000*(ask-bid)/((bid+ask)/2), book_at)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        continue
                self.book_at = book_at
            else:
                quotes = {s: (v["spread_bps"], v["quote_at"]) for s, v in self.market.items()
                          if v["quote_at"] is not None and 0 <= now-v["quote_at"] <= 20}
            if at <= self.market_at:
                # BBO updates every five seconds while 24h statistics use their
                # existing slower cache. Do not pretend either is a new trade.
                # These rows are owned by the engine under this lock. Readers
                # receive scalar copies, so BBO refresh need not clone every row.
                for symbol, value in self.market.items():
                    value["spread_bps"], value["quote_at"] = quotes.get(symbol, (None, None))
                return True
            latest = {}
            for item in tickers:
                symbol = item.get("symbol")
                if symbol not in self.catalog:
                    continue
                try:
                    price, volume, change = (number(item[k]) for k in ("lastPrice", "quoteVolume", "priceChangePercent"))
                    source_at = number(item["closeTime"])/1000
                    if price <= 0 or volume < 0 or not -2 <= now-source_at <= 45:
                        continue
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
                samples = self.prices.setdefault(symbol, deque(maxlen=17))
                bucket = int(source_at)//60
                if samples and samples[-1][0] == bucket:
                    samples[-1] = (bucket, price)
                elif not samples or samples[-1][0] < bucket:
                    samples.append((bucket, price))
                def move(minutes):
                    previous = next((p for b, p in samples if b == bucket-minutes), None)
                    return 100*(price/previous-1) if previous else None
                spread, quoted_at = quotes.get(symbol, (None, None))
                latest[symbol] = dict(contract=symbol, symbol=self.catalog[symbol]+"/USDT", price=price,
                    quote_24h=volume, change_24h=change, change_5m=move(5), change_15m=move(15),
                    spread_bps=spread, quote_at=quoted_at, market_at=source_at)
            self.market, self.market_at = latest, at
            return True

    def scouts(self, manual, now):
        with self.lock:
            if now-self.market_at > 45 or now-self.catalog_at > 7200:
                return []
            rows = [v for s, v in self.market.items() if s not in manual
                    and v["quote_24h"] >= LIMITS["candidate_24h_quote"]
                    and v["spread_bps"] is not None and v["spread_bps"] <= 20
                    and v["quote_at"] is not None and now-v["quote_at"] <= 20
                    and -10 <= v["change_24h"] <= 35]
            # Broad price screening is explicitly separate from verified flow scoring.
            rows.sort(key=lambda v: (v["change_5m"] if v["change_5m"] is not None else v["change_24h"]/24,
                                     math.log1p(v["quote_24h"]), v["contract"]), reverse=True)
            return [v["contract"] for v in rows[:MAX_SCOUTS]]

    def set_focus(self, symbols):
        selected = tuple(dict.fromkeys(symbols))
        if len(selected) > MAX_FOCUS:
            raise ValueError("focus_capacity")
        with self.lock:
            self.focus = selected
            for mapping in (self.bars, self.stream_at, self.seeded_at, self.live_closed):
                for symbol in tuple(mapping):
                    if symbol not in selected:
                        del mapping[symbol]

    def seed(self, symbol, rows, now):
        parsed = rest_bars(rows, now)
        with self.lock:
            if symbol not in self.focus:
                return
            # A delayed HTTP reply must not replace a newer closed stream candle.
            parsed.update(self.bars.get(symbol, {}))
            self.bars[symbol] = dict(sorted(parsed.items())[-MAX_BARS:])
            self.seeded_at[symbol] = now

    def needs_seed(self, symbol, now):
        """Check continuity without constructing an evidence tree just to discard it."""
        with self.lock:
            if symbol not in self.seeded_at:
                return True
            starts = sorted(self.bars.get(symbol, {}))[-65:]
            if len(starts) < 65 or any(b-a != 60 for a, b in zip(starts, starts[1:])):
                return True
            # Repair a missed final frame after a reconnect, even before the
            # next close creates a visible gap. An offline stream waits to return.
            return (now-(starts[-1]+60) > 75
                    and -2 <= now-self.stream_at.get(symbol, 0) <= 10)

    def ingest_stream(self, payload, now):
        parsed = ws_bar(payload, now)
        if parsed is None:
            return False
        symbol, at, bar = parsed
        with self.lock:
            if symbol not in self.focus or at <= self.stream_at.get(symbol, 0):
                return False
            self.stream_at[symbol] = at
            if bar:
                bars = self.bars.setdefault(symbol, {})
                old = bars.get(bar.start)
                if old is not None and old != bar:
                    # Conflicting closed candles invalidate the window until a fresh seed.
                    self.bars.pop(symbol, None)
                    self.seeded_at.pop(symbol, None)
                    self.live_closed.pop(symbol, None)
                    return False
                bars[bar.start] = bar
                while len(bars) > MAX_BARS:
                    del bars[min(bars)]
                self.live_closed[symbol] = max(bar.start, self.live_closed.get(symbol, 0))
            return True

    def inspect(self, symbol, now):
        with self.lock:
            market = dict(self.market.get(symbol, {}))
            bars = sorted(self.bars.get(symbol, {}).values(), key=lambda b: b.start)
            stream_at = self.stream_at.get(symbol)
            live_close = self.live_closed.get(symbol)
            cat_at = self.catalog_at
            supported = symbol in self.catalog
        result = dict(contract=symbol, symbol=market.get("symbol", symbol[:-4]+"/USDT"),
                      market=market, status="warming", status_reason="等待完整的65根分钟数据及实时连接",
                      stream_at=stream_at, evidence=None, candidate=False, score=None, kind=None,
                      can_notify=False)
        if not supported or now-cat_at > 7200:
            return dict(result, status="unavailable", status_reason="现货交易目录不可用或已过期")
        if not stream_at or not -2 <= now-stream_at <= 10:
            return dict(result, status="stale", status_reason="实时成交汇总中断；暂停新提醒和候选")
        tail = bars[-65:]
        if any(b.start-a.start != 60 for a, b in zip(tail, tail[1:])):
            return dict(result, status="gap", status_reason="分钟数据有缺口；不推算缺失成交量")
        if len(bars) < 65:
            return result
        if not 0 <= now-(tail[-1].start+60) <= 75:
            return dict(result, status="stale", status_reason="最新已收盘分钟过期")
        baseline, recent = tail[:-5], tail[-5:]
        volume = sum(b.quote for b in recent)
        buy = sum(b.buy for b in recent)
        baseline_quote = sum(b.quote for b in baseline)
        expected = baseline_quote/12
        ratio = volume/expected if expected > 0 else None
        share = buy/volume if volume > 0 else None
        move = 100*(recent[-1].close/recent[0].open-1)
        range_pct = 100*(max(b.high for b in recent)-min(b.low for b in recent))/recent[0].open
        drawdown = 100*(1-recent[-1].close/max(b.high for b in recent))
        change15 = 100*(tail[-1].close/tail[-15].open-1)
        positive = sum(b.buy*2 > b.quote for b in recent)
        fresh_book = market.get("quote_at") is not None and 0 <= now-market["quote_at"] <= 20
        liquid = (fresh_book and market.get("spread_bps", 1e9) <= 20
                  and -2 <= now-market.get("market_at", 0) <= 45 and market.get("quote_24h", 0) >= 10000000)
        evidence = dict(policy=POLICY, source="Binance Spot /api/v3/klines + @kline_1m", unit="USDT",
            window_start=recent[0].start, window_end=recent[-1].start+60,
            baseline_start=baseline[0].start, baseline_end=baseline[-1].start+60,
            candle_count=5, baseline_candle_count=60, quote_volume=volume,
            baseline_quote_volume=baseline_quote, expected_5m_quote=expected, volume_ratio=ratio,
            taker_buy_quote=buy, taker_sell_quote=volume-buy, taker_delta_quote=2*buy-volume,
            buy_share=share, positive_delta_minutes=positive, change_pct=move, change_15m_pct=change15,
            range_pct=range_pct, drawdown_pct=drawdown, spread_bps=market.get("spread_bps"),
            book_at=market.get("quote_at"), quote_24h=market.get("quote_24h"), stream_at=stream_at,
            thresholds=dict(LIMITS), interpretation="主动买卖差额不是资金净流入；公开成交不能识别主力身份；未使用链上转账。")
        enough = ratio is not None and volume >= 100000 and expected >= 10000
        volume_signal = enough and ratio >= 2.5
        accumulated = (volume_signal and share >= .60 and positive >= 3
                       and -.5 <= move <= 2.5 and range_pct <= 5 and drawdown <= 2 and liquid)
        candidate = (enough and ratio >= 1.5 and share >= .58 and positive >= 3
                     and -0.5 <= move <= 4 and -2 <= change15 <= 10 and drawdown <= 2 and liquid)
        score_parts = dict(volume=min(30, 15*math.log2(max(1, ratio or 1))),
                           aggressive_buy=max(0, min(30, ((share or 0)-.5)*150)),
                           persistence=4*positive, structure=max(0, 20-4*abs(move)-3*drawdown))
        risks = ["交易所成交证据仅覆盖币安USDT现货，未核验链上持有人、解锁或新闻。",
                 "疑似吸筹是结构推断，可能来自做市、套利或短期对倒；不等于主力建仓。"]
        if not liquid:
            risks.append("成交规模、买卖价差或盘口新鲜度未通过候选门槛。")
        if abs(move) > 4 or change15 > 10:
            risks.append("短时价格变化过大，可能处于追涨或抛售阶段。")
        evidence.update(score_parts=score_parts, risks=risks)
        return dict(result, status="live", status_reason="连续65根已收盘分钟完整；实时流在线",
                    evidence=evidence, candidate=bool(candidate), score=round(sum(score_parts.values()), 2),
                    kind="suspected_accumulation" if accumulated else "volume_expansion" if volume_signal else None,
                    can_notify=live_close is not None and live_close >= recent[-1].start)

    def overview(self, now):
        with self.lock:
            rows = self.market.values()
            return dict(exchange="Binance", market="USDT现货", catalogue_count=len(self.catalog),
                screened_count=len(self.market), up_24h=sum(v["change_24h"] > 0 for v in rows),
                down_24h=sum(v["change_24h"] < 0 for v in self.market.values()),
                quote_24h=sum(v["quote_24h"] for v in self.market.values()), at=self.market_at,
                stale=not self.market_at or now-self.market_at > 45 or now-self.catalog_at > 7200,
                focus_count=len(self.focus), retained_bars=sum(len(v) for v in self.bars.values()),
                scope="大盘仅筛查价格与24H成交活跃度；未对全部币订阅成交汇总。", limits=dict(LIMITS))


def notice(row, previous, now):
    """Return a frozen event; caller must commit it together with the next state."""
    if row["status"] != "live" or not row["can_notify"]:
        return None, previous
    kind, evidence = row["kind"], row["evidence"]
    if kind is None:
        return None, {**previous, "active": False}
    old_at = previous.get("at", 0)
    changed = not previous.get("active") or previous.get("kind") != kind
    stronger = evidence["volume_ratio"] >= previous.get("ratio", math.inf)*1.5
    if now-old_at < 600 or not (changed or stronger):
        return None, previous
    identity = hashlib.sha256(f'{POLICY}:{row["contract"]}:{kind}:{evidence["window_end"]}'.encode()).hexdigest()[:40]
    event = dict(id=identity, contract=row["contract"], symbol=row["symbol"], kind=kind,
                 type="market_flow", strategy="market_flow", observed_at=now,
                 evidence=evidence, title="疑似吸筹 · 主动买盘占优" if kind == "suspected_accumulation" else "现货放量",
                 notice_key="market-flow:"+identity, created_at_epoch=now)
    return event, dict(active=True, kind=kind, at=now, ratio=evidence["volume_ratio"])
