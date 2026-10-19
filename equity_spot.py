"""Public, evidence-gated equity spot quotes. No alerts, credentials or orders.

Gate's API quotes use rebased platform units; Bybit quotes use raw xStock units
and must be divided by its current xstockMultiplier. Bitget rToken identity is
joined from stock-info + isReality instruments, never inferred from an r prefix.
Reviewed 2026-10-19. Sources are returned with each pair for user inspection.
"""
import math
import re

EXTRA_URLS = {
    'equity_gate_pairs': 'https://api.gateio.ws/api/v4/spot/currency_pairs',
    'equity_gate_coins': 'https://api.gateio.ws/api/v4/spot/currencies',
    'equity_bitget_instruments': 'https://api.bitget.com/api/v3/market/instruments?category=SPOT',
    'equity_bitget_stocks': 'https://api.bitget.com/api/v3/reality/market/stock-info',
    'equity_bybit_instruments': 'https://api.bybit.com/v5/market/instruments-info?category=spot',
    'equity_bybit_tickers': 'https://api.bybit.com/v5/market/tickers?category=spot',
}
METADATA_TTLS = {'equity_gate_pairs': 60, 'equity_gate_coins': 300,
                 'equity_bitget_instruments': 60, 'equity_bitget_stocks': 600,
                 'equity_bybit_instruments': 15}
VENUE_INPUTS = {'Gate': ('equity_gate_pairs', 'equity_gate_coins', 'gate_spot'),
                'Bitget': ('equity_bitget_instruments', 'equity_bitget_stocks', 'bitget_spot'),
                'Bybit': ('equity_bybit_instruments', 'equity_bybit_tickers')}
UNIT_SOURCES = {
    'Gate': 'https://www.gate.com/zh/help/trade/spot/101757/stock-token-rebase-adjustment-notice',
    'Bybit': 'https://www.bybit.com/en/help-center/article/FAQ-xStocks-on-Bybit',
    'Bitget': 'https://www.bitget.com/support/articles/12560603887610',
}
# Explicit issuer/venue-verified identities, not arbitrary X suffix stripping.
# SOL mint + current Gate category/name are checked again on every metadata refresh.
XSTOCKS = {
    'AAPLX': ('AAPL', 'Apple xStock', 'XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp'),
    'TSLAX': ('TSLA', 'Tesla xStock', 'XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB'),
    'NVDAX': ('NVDA', 'NVIDIA xStock', 'Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh'),
    'COINX': ('COIN', 'Coinbase xStock', 'Xs7ZdzSHLU9ftNJsii5fCeJhoRWSC32SQGzGQtePxNu'),
    'CRCLX': ('CRCL', 'Circle xStock', 'XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1'),
    'HOODX': ('HOOD', 'Robinhood xStock', 'XsvNBAYkrDRNhA7wPHQfX3ZUXZyZLdnCQDfHZ56bzpg'),
    'METAX': ('META', 'Meta xStock', 'Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu'),
    'GOOGLX': ('GOOGL', 'Alphabet xStock', 'XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN'),
    'AMZNX': ('AMZN', 'Amazon xStock', 'Xs3eBt7uRfJX8QUs4suhyU8p2M6DoUDrJyWBa8LLZsg'),
    'MSTRX': ('MSTR', 'MicroStrategy xStock', 'XsP7xzNPvEHS1m6qfanPUGjNmdnmsLKEoNAnHjdxxyZ'),
    'SPYX': ('SPY', 'SP500 xStock', 'XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W'),
    'QQQX': ('QQQ', 'Nasdaq xStock', 'Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ'),
}
BYBIT_REVIEWED = frozenset(('AAPLX', 'TSLAX', 'NVDAX', 'COINX', 'CRCLX', 'HOODX', 'METAX', 'GOOGLX', 'AMZNX'))


def finite(value, positive=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and (not positive or value > 0) else None
    except (TypeError, ValueError):
        return None


def book(ask, bid):
    ask, bid = finite(ask, True), finite(bid, True)
    return (ask, bid) if ask is not None and bid is not None and ask >= bid else None


def rows(payload, provider):
    if provider == 'Gate':
        values = payload
    elif provider == 'Bitget':
        if not isinstance(payload, dict) or payload.get('code') != '00000':
            raise ValueError('Bitget公开接口未返回成功')
        values = payload.get('data')
    else:
        if not isinstance(payload, dict) or payload.get('retCode') != 0:
            raise ValueError('Bybit公开接口未返回成功')
        if payload.get('result', {}).get('nextPageCursor'):
            raise ValueError('Bybit现货目录不完整，须完成分页核验')
        values = payload.get('result', {}).get('list')
    if not isinstance(values, list) or not values or not all(isinstance(item, dict) for item in values):
        raise ValueError('公开产品目录或报价为空/格式异常')
    return values


def verified_mappings(exchange, data):
    if exchange == 'Bitget':
        instruments = rows(data['equity_bitget_instruments'], exchange)
        references = {item.get('symbol'): item for item in rows(data['equity_bitget_stocks'], exchange)}
        for item in instruments:
            ref = references.get(item.get('symbol'), {})
            code, raw, isin = ref.get('code'), item.get('symbol'), item.get('isin', '')
            if (item.get('category') != 'SPOT' or item.get('isReality') != 'yes' or item.get('status') != 'online'
                    or item.get('quoteCoin') != 'USDT' or item.get('instrumentType') not in ('stock', 'etf')
                    or not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{0,15}', code)
                    or not isinstance(raw, str) or not re.fullmatch(r'[A-Z0-9]+USDT', raw)
                    or raw != str(item.get('baseCoin', '')).upper() + 'USDT'
                    or not isinstance(isin, str) or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', isin)):
                continue
            yield dict(underlying=code, raw=raw, product='rToken', units=1.0,
                       unit_basis='平台每股单位', name=item.get('fullName') or ref.get('name') or code,
                       isin=isin, trading_period=ref.get('tradingPeriod'), weekend_tradable=ref.get('weekendTradable'))
    elif exchange == 'Gate':
        currencies = {item.get('currency'): item for item in rows(data['equity_gate_coins'], exchange)}
        for item in rows(data['equity_gate_pairs'], exchange):
            base, coin = item.get('base'), currencies.get(item.get('base'), {})
            identity = XSTOCKS.get(base)
            if not identity or item.get('quote') != 'USDT' or item.get('trade_status') != 'tradable':
                continue
            code, name, mint = identity
            if (item.get('id') != base + '_USDT' or item.get('base_name') != name or coin.get('name') != name
                    or 'xstocks' not in (coin.get('category') or []) or coin.get('delisted') is not False
                    or coin.get('trade_disabled') is not False
                    or not any(chain.get('name') == 'SOL' and chain.get('addr') == mint for chain in coin.get('chains', []))):
                continue
            yield dict(underlying=code, raw=item['id'], product='xStocks', units=1.0,
                       unit_basis='Gate复权后平台单位', name=name, mint=mint)
    elif exchange == 'Bybit':
        for item in rows(data['equity_bybit_instruments'], exchange):
            base = item.get('baseCoin')
            units = finite(item.get('xstockMultiplier'), True)
            if (base not in BYBIT_REVIEWED or item.get('symbolType') != 'xstocks' or item.get('status') != 'Trading'
                    or item.get('quoteCoin') != 'USDT' or item.get('symbol') != base + 'USDT' or units is None):
                continue
            code, name, _ = XSTOCKS[base]
            yield dict(underlying=code, raw=item['symbol'], product='xStocks', units=units,
                       unit_basis='Bybit实时乘数折股', name=name)


def build_snapshot(data, errors, observed, now, previous=None):
    """Independent venue failures retain old rows with old timestamps, not new prices."""
    previous = previous or {}
    groups, failures, coverage = {}, {}, {}
    info = data.get('futures_info')
    futures_info = info.get('symbols', []) if isinstance(info, dict) else []
    futures_info = futures_info if isinstance(futures_info, list) else []
    contracts = {item.get('symbol'): item for item in futures_info if isinstance(item, dict) and item.get('underlyingType') == 'EQUITY'
                 and item.get('status') == 'TRADING' and item.get('quoteAsset') == 'USDT'
                 and item.get('contractType') in ('TRADIFI_PERPETUAL', 'TRADFI_PERPETUAL', 'PERPETUAL')}
    def index_payload(key):
        values = data.get(key)
        return {item.get('symbol'): item for item in values if isinstance(item, dict)} if isinstance(values, list) else {}
    futures, funding = index_payload('futures_books'), index_payload('funding')
    interval_stamp = finite(observed.get('funding_info'))
    intervals = index_payload('funding_info') if not errors.get('funding_info') and interval_stamp is not None and -5 <= now-interval_stamp <= 65 else {}
    for exchange, inputs in VENUE_INPUTS.items():
        try:
            needed = (*inputs, 'futures_info', 'futures_books', 'funding')
            for key in needed:
                stamp = finite(observed.get(key))
                max_age = METADATA_TTLS.get(key, 3600 if key == 'futures_info' else 15) + (5 if key in METADATA_TTLS or key == 'futures_info' else 0)
                if key not in data or errors.get(key) or stamp is None or not -5 <= now-stamp <= max_age:
                    raise ValueError(f'{key}未就绪或已过期')
            if not contracts or not futures:
                raise ValueError('币安美股分类/合约盘口未就绪')
            quote_key = inputs[-1]
            quotes = {item.get('currency_pair' if exchange == 'Gate' else 'symbol'): item for item in rows(data[quote_key], exchange)}
            checked = min(observed[key] for key in inputs[:-1])
            accepted = []
            matched = 0
            for mapping in verified_mappings(exchange, data):
                code, raw, units = mapping['underlying'], mapping['raw'], mapping['units']
                contract_symbol = code + 'USDT'
                if contract_symbol not in contracts or contracts[contract_symbol].get('baseAsset') != code:
                    continue
                matched += 1
                quote, future, premium = quotes.get(raw, {}), futures.get(contract_symbol, {}), funding.get(contract_symbol, {})
                ask_field, bid_field = {'Gate': ('lowest_ask', 'highest_bid'), 'Bitget': ('askPr', 'bidPr'), 'Bybit': ('ask1Price', 'bid1Price')}[exchange]
                spot_book, future_book = book(quote.get(ask_field), quote.get(bid_field)), book(future.get('askPrice'), future.get('bidPrice'))
                if not spot_book or not future_book:
                    continue
                ask, bid = spot_book[0]/units, spot_book[1]/units
                if not all(math.isfinite(value) and value > 0 for value in (ask, bid)):
                    continue
                stamps = [observed[quote_key], observed['futures_books']]
                for value in (quote.get('ts'), future.get('time')):
                    if value is not None:
                        epoch = finite(value, True)
                        stamps.append(epoch/1000 if epoch is not None else 0)
                captured = min(stamps)
                if any(stamp > now+5 for stamp in stamps):
                    captured = 0
                mark, index, rate = finite(premium.get('markPrice'), True), finite(premium.get('indexPrice'), True), finite(premium.get('lastFundingRate'))
                basis = (mark-index)/index*100 if mark is not None and index is not None else None
                row = dict(symbol=code+'/USDT', long_exchange=exchange, short_exchange='Binance',
                           spot_symbol=raw.replace('_', '')[:-4]+'/USDT', futures_symbol=code+'/USDT',
                           long_ask=ask, long_bid=bid, raw_long_ask=spot_book[0], raw_long_bid=spot_book[1],
                           short_bid=future_book[1], short_ask=future_book[0], units_per_token=units,
                           unit_basis=mapping['unit_basis'], product=mapping['product'], product_name=mapping['name'],
                           basis=basis if basis is None or math.isfinite(basis) else None,
                           funding_rate=finite(rate*100) if rate is not None else None,
                           funding_interval_hours=finite(intervals.get(contract_symbol, {}).get('fundingIntervalHours'), True),
                           open_spread=(future_book[1]-ask)/ask*100, close_spread=(future_book[0]-bid)/bid*100,
                           captured_at_epoch=captured, verification_version=1, verified_at_epoch=checked,
                           isin=mapping.get('isin'), mint=mapping.get('mint'), data_error=None,
                           pairing_sources=[EXTRA_URLS[key] for key in inputs[:-1]]+[UNIT_SOURCES[exchange]],
                           trading_period=mapping.get('trading_period'), weekend_tradable=mapping.get('weekend_tradable'))
                if not all(math.isfinite(row[key]) for key in ('open_spread','close_spread')):
                    continue
                accepted.append(row)
            coverage[exchange] = dict(verified_pairs=matched, quoted_pairs=len(accepted), status='ok')
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            failures[exchange] = str(exc)
            accepted = [dict(row, data_error='上游未就绪，保留旧报价') for group in previous.get('symbols', [])
                        for row in group['rows'] if row['long_exchange'] == exchange]
            coverage[exchange] = dict(verified_pairs=None, quoted_pairs=len(accepted), status='stale')
        for row in accepted:
            groups.setdefault(row['symbol'], []).append(row)
    stamps = [row['captured_at_epoch'] for entries in groups.values() for row in entries]
    return dict(symbols=[dict(symbol=symbol, rows=entries) for symbol, entries in sorted(groups.items())],
                errors=failures, coverage=coverage, captured_at_epoch=min(stamps) if stamps else None,
                checked_at_epoch=now, stored=True)
