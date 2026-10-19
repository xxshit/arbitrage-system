"""Explain delayed public inputs without blaming unrelated exchange paths."""

SOURCE_FIELDS = {
    "futures_info": ("Binance", "合约", "目录"),
    "futures_books": ("Binance", "合约", "盘口"),
    "futures_24h": ("Binance", "合约", "成交额"),
    "funding": ("Binance", "合约", "基差/资费"),
    "funding_info": ("Binance", "合约", "结算周期"),
    "binance_spot": ("Binance", "现货", "盘口"),
    "binance_spot_24h": ("Binance", "现货", "成交额"),
    "gate_spot": ("Gate", "现货", "盘口"),
    "bitget_spot": ("Bitget", "现货", "盘口"),
    "binance_books": ("Binance", "合约", "盘口"),
    "binance_24h": ("Binance", "合约", "成交额"),
    "binance_funding": ("Binance", "合约", "基差/资费"),
    "binance_funding_info": ("Binance", "合约", "结算周期"),
    "binance_info": ("Binance", "合约", "目录"),
    "bybit_instruments": ("Bybit", "合约", "目录"),
    "bybit_tickers": ("Bybit", "合约", "盘口/资费"),
    "okx_instruments": ("OKX", "合约", "目录"),
    "okx_tickers": ("OKX", "合约", "盘口"),
    "okx_mark_prices": ("OKX", "合约", "标记价"),
    "okx_index_tickers": ("OKX", "合约", "指数价"),
    "okx_open_interest": ("OKX", "合约", "持仓量"),
}


def delay_reason(error):
    value = str(error).lower()
    if any(word in value for word in ("429", "418", "rate_limit", "10006", "10018", "10429")):
        return "上游限流，退避重试"
    if any(word in value for word in ("timeout", "timed out", "deadline")):
        return "请求超时"
    if "api_error" in value or "json" in value or "payload" in value:
        return "上游响应无效"
    return "连接或上游响应异常"


def snapshot_delays(groups, errors, observed_at, strategy):
    sources, affected = [], set()
    for name, error in errors.items():
        exchange, market, field = SOURCE_FIELDS.get(name, ("上游", "", "数据"))
        paths = []
        for group in groups:
            for row in group["rows"]:
                long, short = row["long_exchange"], row["short_exchange"]
                matches = (long == exchange if market == "现货" else short.startswith(exchange)) if strategy == "spot_futures" else exchange in (long, short)
                if matches:
                    identity = (group["symbol"], long, short)
                    affected.add(identity)
                    paths.append(identity)
        sources.append(dict(source=name, exchange=exchange, market=market, field=field,
                            label=exchange+market+field, reason=delay_reason(error),
                            observed_at_epoch=observed_at.get(name), affected_paths=len(paths),
                            examples=[dict(symbol=s, long_exchange=l, short_exchange=r) for s, l, r in paths[:6]]))
    return dict(sources=sources, affected_paths=len(affected), affected_symbols=len({p[0] for p in affected}))
