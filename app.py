import os
import random
import json
import time
import threading
import re
import html
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect, text, tuple_

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "local-development-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///arbitrage_hub.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class Strategy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mode = db.Column(db.String(40), nullable=False)
    symbol = db.Column(db.String(30), nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class AlertEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    strategy = db.Column(db.String(30), nullable=False, default="spot_futures", index=True)
    long_exchange = db.Column(db.String(30))
    short_exchange = db.Column(db.String(30))
    alert_type = db.Column(db.String(40), nullable=False, index=True)
    message = db.Column(db.String(255), nullable=False)
    open_spread = db.Column(db.Float)
    close_spread = db.Column(db.Float)
    basis = db.Column(db.Float)
    funding_rate = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class BasisTracking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    strategy = db.Column(db.String(30), nullable=False, default="spot_futures", index=True)
    direction = db.Column(db.String(10), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    opening_basis = db.Column(db.Float)
    last_recorded_level = db.Column(db.Float, nullable=False, default=1.0)
    max_basis = db.Column(db.Float, nullable=False)
    max_abs_basis = db.Column(db.Float, nullable=False)
    max_at = db.Column(db.DateTime, nullable=False)
    resolved_at = db.Column(db.DateTime)


class BasisExpansionLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tracking_id = db.Column(db.Integer, db.ForeignKey("basis_tracking.id"), nullable=False, index=True)
    level = db.Column(db.Float, nullable=False)
    observed_basis = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class LatestMarketSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False)
    long_exchange = db.Column(db.String(30), nullable=False)
    short_exchange = db.Column(db.String(30), nullable=False)
    long_ask = db.Column(db.Float, nullable=False)
    long_bid = db.Column(db.Float, nullable=False)
    short_bid = db.Column(db.Float, nullable=False)
    short_ask = db.Column(db.Float, nullable=False)
    basis = db.Column(db.Float, nullable=False)
    funding_rate = db.Column(db.Float, nullable=False)
    funding_interval_hours = db.Column(db.Float, nullable=False)
    next_funding_time = db.Column(db.String(30), nullable=False)
    spot_volume = db.Column(db.Float)
    futures_volume = db.Column(db.Float)
    futures_open_interest = db.Column(db.Float)
    open_spread = db.Column(db.Float, nullable=False)
    close_spread = db.Column(db.Float, nullable=False)
    captured_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("symbol", "long_exchange", name="uq_latest_market_symbol_exchange"),)


class FundingRateRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    funding_time = db.Column(db.BigInteger, nullable=False, index=True)
    funding_rate = db.Column(db.Float, nullable=False)
    __table_args__ = (db.UniqueConstraint("symbol", "funding_time", name="uq_funding_symbol_time"),)


class FuturesPriceHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    bucket_at = db.Column(db.BigInteger, nullable=False, index=True)
    price = db.Column(db.Float, nullable=False)
    __table_args__ = (db.UniqueConstraint("symbol", "bucket_at", name="uq_futures_price_history_symbol_bucket"),)


class LatestDualFuturesSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False)
    long_exchange = db.Column(db.String(30), nullable=False)
    short_exchange = db.Column(db.String(30), nullable=False)
    long_ask = db.Column(db.Float, nullable=False)
    long_bid = db.Column(db.Float, nullable=False)
    short_bid = db.Column(db.Float, nullable=False)
    short_ask = db.Column(db.Float, nullable=False)
    long_basis = db.Column(db.Float)
    short_basis = db.Column(db.Float)
    long_index = db.Column(db.Float)
    short_index = db.Column(db.Float)
    long_volume = db.Column(db.Float)
    short_volume = db.Column(db.Float)
    long_open_interest = db.Column(db.Float)
    short_open_interest = db.Column(db.Float)
    funding_difference = db.Column(db.Float)
    long_funding_rate = db.Column(db.Float)
    short_funding_rate = db.Column(db.Float)
    long_funding_interval_hours = db.Column(db.Float)
    short_funding_interval_hours = db.Column(db.Float)
    long_next_funding_time = db.Column(db.String(30))
    short_next_funding_time = db.Column(db.String(30))
    open_spread = db.Column(db.Float, nullable=False)
    close_spread = db.Column(db.Float, nullable=False)
    captured_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("symbol", "long_exchange", "short_exchange", name="uq_latest_dual_futures_path"),)


class DualFuturesPriceHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    exchange = db.Column(db.String(30), nullable=False, index=True)
    bucket_at = db.Column(db.BigInteger, nullable=False, index=True)
    price = db.Column(db.Float, nullable=False)
    __table_args__ = (db.UniqueConstraint("symbol", "exchange", "bucket_at", name="uq_dual_futures_price_history"),)


class IndexComponentSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exchange = db.Column(db.String(30), nullable=False)
    symbol = db.Column(db.String(30), nullable=False)
    components_json = db.Column(db.Text, nullable=False, default="[]")
    captured_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("exchange", "symbol", name="uq_index_component_exchange_symbol"),)


class ListingState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exchange = db.Column(db.String(30), nullable=False)
    symbol = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(30), nullable=False)
    first_seen_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    last_seen_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    __table_args__ = (db.UniqueConstraint("exchange", "symbol", name="uq_listing_state_exchange_symbol"),)


class ListingEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exchange = db.Column(db.String(30), nullable=False, index=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    event_type = db.Column(db.String(12), nullable=False, index=True)
    title = db.Column(db.String(500))
    source_url = db.Column(db.String(1000))
    announcement = db.Column(db.Boolean, default=False, nullable=False, index=True)
    effective_at = db.Column(db.DateTime)
    occurred_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class DailyHornSignal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_date = db.Column(db.String(10), nullable=False, index=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    timeframe = db.Column(db.String(8), nullable=False)
    price_change = db.Column(db.Float, nullable=False)
    oi_change = db.Column(db.Float, nullable=False)
    oi_value = db.Column(db.Float)
    ratio_change = db.Column(db.Float, nullable=False)
    ratio_value = db.Column(db.Float)
    cvd_change = db.Column(db.Float)
    cvd_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    score = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    __table_args__ = (db.UniqueConstraint("report_date", "symbol", "timeframe", name="uq_daily_horn_signal"),)


class LarkPushState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel = db.Column(db.String(40), nullable=False)
    symbol = db.Column(db.String(30), nullable=False)
    signal_key = db.Column(db.String(120), nullable=False)
    pushed_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("channel", "symbol", "signal_key", name="uq_lark_push_state"),)


class AutomationStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_key = db.Column(db.String(80), nullable=False, unique=True, index=True)
    label = db.Column(db.String(120), nullable=False)
    last_started_at = db.Column(db.DateTime)
    last_finished_at = db.Column(db.DateTime)
    last_success_at = db.Column(db.DateTime)
    last_error_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(1000))
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class TransferNetworkSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exchange = db.Column(db.String(30), nullable=False)
    symbol = db.Column(db.String(30), nullable=False)
    chains_json = db.Column(db.Text, nullable=False, default="[]")
    captured_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("exchange", "symbol", name="uq_transfer_network_exchange_symbol"),)


MARKETS = {
    "BTC/USDT": {"base": 67820.0, "funding": 0.0108},
    "ETH/USDT": {"base": 3625.0, "funding": 0.0182},
    "SOL/USDT": {"base": 154.8, "funding": -0.0041},
}
EXCHANGES = ["Binance", "OKX", "Bybit"]
SPOT_FUTURES_CACHE = {"snapshot": None, "expires_at": 0.0}
MARKET_PAYLOAD_CACHE = {}
SPOT_VIEW_CACHE = {"key": None, "symbols": None}
DUAL_VIEW_CACHE = {"key": None, "symbols": None}
FUNDING_HISTORY_CACHE = {}
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
BASIS_CANDIDATES = {}
PUMP_CANDIDATES = {}
QUOTE_VALIDATION_CACHE = {}
DUAL_ALERT_CANDIDATES = {}
DUAL_ALERT_VALUES = {}
RAPID_MOVE_HISTORY = {}
RAPID_MOVE_CANDIDATES = {}
RAPID_MOVE_ALERT_WINDOWS = {}
OKX_FUNDING_CACHE = {}
OKX_FUNDING_CURSOR = 0
BINANCE_OPEN_INTEREST_CACHE = {}
BINANCE_OPEN_INTEREST_CURSOR = 0
RWA_STOCK_SYMBOLS = set()
LAST_LISTING_SYNC_AT = 0.0
LISTING_SYNC_SECONDS = 30 * 60
ANNOUNCEMENT_SCAN_HOUR = 8
TRANSFER_NETWORK_SYNC_SECONDS = 15 * 60
LAST_ANNOUNCEMENT_SCAN_DATE = None
ANNOUNCEMENT_SOURCES = {
    "Binance": "https://www.binance.com/en/support/announcement/",
    "Bybit": "https://announcements.bybit.com/en/?category=delistings&page=1",
    "OKX": "https://www.okx.com/help/category/announcements",
    "Gate": "https://www.gate.com/announcements",
    "Bitget": "https://www.bitget.com/support/announcement-center",
}
INDEX_COMPONENT_CURSOR = 0
FUNDING_SYNC_CURSOR = 0
MARKET_REFRESH_SECONDS = 5
HORN_SCAN_HOUR = 8
LAST_HORN_SCAN_DATE = None
LAST_LARK_TREND_PUSH_DATE = None
INDEX_COMPONENT_REFRESH_SECONDS = 5
FUNDING_HISTORY_SYNC_SECONDS = 60
PRICE_BACKFILL_SYNC_SECONDS = 2 * 60
PRICE_HISTORY_BUCKET_SECONDS = 5 * 60
PRICE_HISTORY_RETENTION_SECONDS = 8 * 24 * 60 * 60
TREND_WINDOWS = {
    "change_5m": 5 * 60,
    "change_15m": 15 * 60,
    "change_30m": 30 * 60,
    "change_1h": 60 * 60,
    "change_4h": 4 * 60 * 60,
    "change_12h": 12 * 60 * 60,
    "change_24h": 24 * 60 * 60,
    "change_3d": 3 * 24 * 60 * 60,
    "change_7d": 7 * 24 * 60 * 60,
}
BACKGROUND_WORKERS_STARTED = False
MARKET_REFRESH_METRICS = {
    "network_seconds": 0.0,
    "processing_seconds": 0.0,
    "total_seconds": 0.0,
    "last_success_at": None,
    "last_error": None,
}
# 中文搜索只匹配交易所真实提供的中文币种名称，避免“币安币”这类译名误匹配 BNB。
COIN_ALIASES = {}


def get_json(url, timeout=4):
    request = Request(url, headers={"User-Agent": "ArbiScope/1.0", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_market_payloads(urls, cache_seconds=None, live_timeout=1.5):
    """低频资料走短期缓存；行情请求失败时复用最近有效值，绝不拖慢盘口快照。"""
    cache_seconds = cache_seconds or {}
    now = time.monotonic()
    results, errors, pending = {}, {}, {}
    for name, url in urls.items():
        cached = MARKET_PAYLOAD_CACHE.get(url)
        if cached and now - cached["fetched_at"] < cache_seconds.get(name, 0):
            results[name] = cached["payload"]
        else:
            pending[name] = url
    if pending:
        with ThreadPoolExecutor(max_workers=len(pending)) as executor:
            futures = {executor.submit(get_json, url, live_timeout): (name, url) for name, url in pending.items()}
            for future in as_completed(futures):
                name, url = futures[future]
                try:
                    payload = future.result()
                    results[name] = payload
                    MARKET_PAYLOAD_CACHE[url] = {"payload": payload, "fetched_at": now}
                except Exception as exc:
                    cached = MARKET_PAYLOAD_CACHE.get(url)
                    if cached:
                        results[name] = cached["payload"]
                        errors[name] = f"using cached payload: {exc}"
                    else:
                        errors[name] = str(exc)
    return results, errors


def valid_book(ask, bid):
    try:
        ask, bid = float(ask), float(bid)
        # A crossed top-of-book is an invalid or mismatched quote, never an arbitrage quote.
        return {"ask": ask, "bid": bid} if ask > 0 and bid > 0 and ask >= bid else None
    except (TypeError, ValueError):
        return None


def refresh_rwa_stock_symbols(instruments):
    """以 Binance 合约元数据的 TradFi/EQUITY 分类识别美股 RWA。"""
    global RWA_STOCK_SYMBOLS
    RWA_STOCK_SYMBOLS = {
        item.get("symbol") for item in instruments
        if item.get("contractType") == "TRADIFI_PERPETUAL" or item.get("underlyingType") == "EQUITY" or "TradFi" in item.get("underlyingSubType", [])
    }


def sync_binance_listing_events(instruments):
    current = {
        item.get("symbol"): item.get("status", "UNKNOWN") for item in instruments
        if item.get("symbol", "").endswith("USDT") and item.get("contractType") == "PERPETUAL"
    }
    if not current:
        return
    now = datetime.now()
    existing = {item.symbol: item for item in ListingState.query.filter_by(exchange="Binance").all()}
    if not existing:
        for symbol, status in current.items():
            db.session.add(ListingState(exchange="Binance", symbol=symbol, status=status, first_seen_at=now, last_seen_at=now, active=True))
        db.session.commit()
        return
    for symbol, status in current.items():
        item = existing.get(symbol)
        if not item:
            db.session.add(ListingState(exchange="Binance", symbol=symbol, status=status, first_seen_at=now, last_seen_at=now, active=True))
            db.session.add(ListingEvent(exchange="Binance", symbol=symbol, event_type="上架", occurred_at=now))
        else:
            item.status, item.last_seen_at, item.active = status, now, True
    for symbol, item in existing.items():
        if symbol not in current and item.active:
            item.active = False
            db.session.add(ListingEvent(exchange="Binance", symbol=symbol, event_type="下架", occurred_at=now))
    db.session.commit()


def sync_binance_listing_events_if_due(instruments):
    """Listing status hardly changes; do not rewrite every listing row on each 5s quote tick."""
    global LAST_LISTING_SYNC_AT
    now = time.time()
    if now - LAST_LISTING_SYNC_AT < LISTING_SYNC_SECONDS:
        return
    sync_binance_listing_events(instruments)
    LAST_LISTING_SYNC_AT = now


def announcement_symbols(title):
    """Only accept an explicit quote-pair in the title; never infer a coin from prose."""
    normalized = title.upper().replace("_", "").replace("-", "")
    return sorted({f"{base}/USDT" for base in re.findall(r"\b([A-Z0-9]{2,15})(?:USDT|USDC)\b", normalized)})


def scan_exchange_announcements():
    """Read the official announcement landing pages once daily and persist listing notices."""
    for exchange, url in ANNOUNCEMENT_SOURCES.items():
        try:
            request_obj = Request(url, headers={"User-Agent": "Mozilla/5.0 ArbiScope/1.0"})
            with urlopen(request_obj, timeout=8) as response:
                page = response.read().decode("utf-8", errors="ignore")
        except Exception:
            continue
        fragments = re.findall(r">([^<>]{6,260}(?:delist|listing|list)[^<>]{0,180})<", page, flags=re.IGNORECASE)
        titles = set()
        for fragment in fragments:
            title = re.sub(r"\s+", " ", html.unescape(fragment)).strip()
            lowered = title.lower()
            if len(title) >= 8 and ("delist" in lowered or "listing" in lowered or " list " in f" {lowered} "):
                titles.add(title[:500])
        for title in list(titles)[:80]:
            lowered = title.lower()
            event_type = "下架" if "delist" in lowered else "上架"
            for symbol in announcement_symbols(title):
                exists = ListingEvent.query.filter_by(exchange=exchange, symbol=symbol, event_type=event_type, title=title, announcement=True).first()
                if not exists:
                    db.session.add(ListingEvent(exchange=exchange, symbol=symbol, event_type=event_type, title=title, source_url=url, announcement=True, occurred_at=datetime.now()))
        db.session.commit()


def announced_delisted_symbols():
    return {item.symbol for item in ListingEvent.query.filter_by(event_type="下架").all()}


def mark_announced_delistings(groups):
    delisted = announced_delisted_symbols()
    for group in groups:
        group["delisting_announced"] = group["symbol"] in delisted


AUTOMATION_LABELS = {
    "announcement_scan": "上下架公告抓取",
    "daily_horn_scan": "日报趋势扫描",
    "daily_lark_trend_push": "日报趋势推送",
    "thought_analysis_push": "思路分析盯盘推送",
    "transfer_network_sync": "充提网络同步",
    "index_component_sync": "指数成分同步",
}


def mark_automation_status(task_key, state, error=None, label=None):
    now = datetime.now()
    status = AutomationStatus.query.filter_by(task_key=task_key).first()
    if not status:
        status = AutomationStatus(task_key=task_key, label=label or AUTOMATION_LABELS.get(task_key, task_key))
        db.session.add(status)
    status.label = label or AUTOMATION_LABELS.get(task_key, status.label)
    status.updated_at = now
    if state == "started":
        status.last_started_at = now
    elif state == "success":
        status.last_finished_at = now
        status.last_success_at = now
        status.last_error = None
    elif state == "error":
        status.last_finished_at = now
        status.last_error_at = now
        status.last_error = str(error)[:1000] if error else "unknown error"
    db.session.commit()


def automation_payload(task_key):
    status = AutomationStatus.query.filter_by(task_key=task_key).first()

    def fmt(value):
        return value.replace(tzinfo=timezone.utc).astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S") if value else None

    return {
        "task_key": task_key,
        "label": AUTOMATION_LABELS.get(task_key, task_key),
        "last_started_at": fmt(status.last_started_at) if status else None,
        "last_finished_at": fmt(status.last_finished_at) if status else None,
        "last_success_at": fmt(status.last_success_at) if status else (datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d 08:00:00") if task_key == "daily_lark_trend_push" and lark_daily_trend_already_pushed(datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")) else None),
        "last_error_at": fmt(status.last_error_at) if status else None,
        "last_error": status.last_error if status else None,
        "ran_today": bool((status and status.last_success_at and status.last_success_at.astimezone(SHANGHAI_TZ).date() == datetime.now(SHANGHAI_TZ).date()) or (task_key == "daily_lark_trend_push" and lark_daily_trend_already_pushed(datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")))),
    }


def automation_statuses(*task_keys):
    return {key: automation_payload(key) for key in task_keys}


def as_available(value, default=False):
    if value is None:
        return default
    return str(value).lower() in {"true", "1", "yes", "open", "available", "normal"}


def store_transfer_networks(exchange, networks):
    if not networks:
        return
    symbols = list(networks)
    existing = {(item.exchange, item.symbol): item for item in TransferNetworkSnapshot.query.filter(TransferNetworkSnapshot.exchange == exchange, TransferNetworkSnapshot.symbol.in_(symbols)).all()}
    now = datetime.now()
    for symbol, chains in networks.items():
        payload = json.dumps(chains, ensure_ascii=False)
        item = existing.get((exchange, symbol))
        if item:
            item.chains_json, item.captured_at = payload, now
        else:
            db.session.add(TransferNetworkSnapshot(exchange=exchange, symbol=symbol, chains_json=payload, captured_at=now))
    db.session.commit()


def refresh_public_transfer_networks():
    """Public chain availability for Gate and Bitget; Binance requires a signed account API."""
    try:
        gate_payload = get_json("https://api.gateio.ws/api/v4/spot/currencies", timeout=8)
        gate_networks = {}
        for item in gate_payload:
            base = str(item.get("currency", "")).split("_", 1)[0]
            chains = item.get("chains") or []
            if not base or not chains:
                continue
            gate_networks[f"{base}/USDT"] = [{
                "name": chain.get("name") or chain.get("chain") or "Unknown",
                "deposit_open": not bool(chain.get("deposit_disabled", item.get("deposit_disabled", False))),
                "withdraw_open": not bool(chain.get("withdraw_disabled", item.get("withdraw_disabled", False))),
            } for chain in chains]
        store_transfer_networks("Gate", gate_networks)
    except Exception:
        db.session.rollback()
    try:
        bitget_payload = get_json("https://api.bitget.com/api/v2/spot/public/coins", timeout=8)
        bitget_networks = {}
        for item in bitget_payload.get("data", []):
            base = item.get("coin") or item.get("coinName")
            if not base:
                continue
            chains = item.get("chains") or item.get("chainList") or []
            bitget_networks[f"{base}/USDT"] = [{
                "name": chain.get("chain") or chain.get("chainName") or "Unknown",
                "deposit_open": as_available(chain.get("rechargeable", chain.get("depositable"))),
                "withdraw_open": as_available(chain.get("withdrawable")),
            } for chain in chains]
        store_transfer_networks("Bitget", bitget_networks)
    except Exception:
        db.session.rollback()


def enrich_transfer_networks(groups):
    pairs = [group["symbol"] for group in groups]
    records = TransferNetworkSnapshot.query.filter(TransferNetworkSnapshot.symbol.in_(pairs)).all()
    indexed = {(item.exchange, item.symbol): json.loads(item.chains_json or "[]") for item in records}
    for group in groups:
        for row in group["rows"]:
            exchange = row["long_exchange"]
            if exchange == "Binance":
                row["transfer_networks"] = []
                row["transfer_status_source"] = "需要只读账户 API"
            else:
                row["transfer_networks"] = indexed.get((exchange, group["symbol"]), [])
                row["transfer_status_source"] = "公开接口"


def is_rwa_stock_pair(symbol):
    return symbol.replace("/", "").replace("-", "") in RWA_STOCK_SYMBOLS


def refresh_binance_open_interest(contracts):
    """Binance 公开 OI 接口按合约返回，后台分批缓存为美元名义价值。"""
    global BINANCE_OPEN_INTEREST_CURSOR
    now = time.time()
    pending = [symbol for symbol in sorted(contracts) if now - BINANCE_OPEN_INTEREST_CACHE.get(symbol, {}).get("updated_at", 0) >= 60]
    if not pending:
        return
    batch_size = min(5, len(pending))
    start = BINANCE_OPEN_INTEREST_CURSOR % len(pending)
    batch = (pending + pending)[start:start + batch_size]
    BINANCE_OPEN_INTEREST_CURSOR = (start + len(batch)) % len(pending)

    def fetch(symbol):
        return symbol, get_json("https://fapi.binance.com/fapi/v1/openInterest?" + urlencode({"symbol": symbol}), timeout=1)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch, symbol) for symbol in batch]
        for future in as_completed(futures):
            try:
                symbol, payload = future.result()
                BINANCE_OPEN_INTEREST_CACHE[symbol] = {"contracts": float(payload.get("openInterest", 0) or 0), "updated_at": now}
            except Exception:
                continue


def sync_funding_history(symbols):
    now = datetime.now(SHANGHAI_TZ)
    start_day = now.date() - timedelta(days=29)
    start_time = int(datetime.combine(start_day, datetime.min.time(), tzinfo=SHANGHAI_TZ).timestamp() * 1000)
    end_time = int(now.timestamp() * 1000)
    latest_times = {symbol: timestamp for symbol, timestamp in db.session.query(FundingRateRecord.symbol, func.max(FundingRateRecord.funding_time)).filter(FundingRateRecord.symbol.in_(symbols)).group_by(FundingRateRecord.symbol).all()}
    existing_keys = {(symbol, timestamp) for symbol, timestamp in db.session.query(FundingRateRecord.symbol, FundingRateRecord.funding_time).filter(FundingRateRecord.symbol.in_(symbols), FundingRateRecord.funding_time >= start_time).all()}
    pending = list(symbols)

    def fetch_history(symbol):
        recent_start = max(start_time, (latest_times.get(symbol) or start_time) - 24 * 60 * 60 * 1000)
        url = "https://fapi.binance.com/fapi/v1/fundingRate?" + urlencode({"symbol": symbol, "startTime": recent_start, "endTime": end_time, "limit": 1000})
        return symbol, get_json(url, timeout=4)

    if pending:
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(fetch_history, symbol) for symbol in pending]
            for future in as_completed(futures):
                try:
                    symbol, history = future.result()
                    for item in history:
                        key = (symbol, item["fundingTime"])
                        if key not in existing_keys:
                            db.session.add(FundingRateRecord(symbol=symbol, funding_time=item["fundingTime"], funding_rate=float(item["fundingRate"]) * 100))
                            existing_keys.add(key)
                except Exception:
                    pass
        cutoff = int(datetime.combine(now.date() - timedelta(days=30), datetime.min.time(), tzinfo=SHANGHAI_TZ).timestamp() * 1000)
        FundingRateRecord.query.filter(FundingRateRecord.funding_time < cutoff).delete(synchronize_session=False)
        db.session.commit()

    cutoff = int(datetime.combine(now.date() - timedelta(days=30), datetime.min.time(), tzinfo=SHANGHAI_TZ).timestamp() * 1000)
    FundingRateRecord.query.filter(FundingRateRecord.funding_time < cutoff).delete(synchronize_session=False)
    db.session.commit()
    stored_rows = FundingRateRecord.query.filter(FundingRateRecord.symbol.in_(symbols), FundingRateRecord.funding_time >= start_time, FundingRateRecord.funding_time <= end_time).all()
    stored_by_symbol = {}
    for item in stored_rows:
        stored_by_symbol.setdefault(item.symbol, []).append(item)
    output = {}
    for symbol in symbols:
        rows = sorted(stored_by_symbol.get(symbol, []), key=lambda item: item.funding_time)
        by_date = {}
        for item in rows:
            funding_date = datetime.fromtimestamp(item.funding_time / 1000, tz=timezone.utc).astimezone(SHANGHAI_TZ).date()
            by_date[funding_date] = by_date.get(funding_date, 0.0) + item.funding_rate
        output[symbol] = {
            "previous": rows[-1].funding_rate if rows else None,
            "day_1": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(1)),
            "day_3": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(3)),
            "day_7": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(7)),
            "day_30": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(30)),
        }
    return output


def funding_statistics(symbols):
    now = datetime.now(SHANGHAI_TZ)
    start_day = now.date() - timedelta(days=29)
    start_time = int(datetime.combine(start_day, datetime.min.time(), tzinfo=SHANGHAI_TZ).timestamp() * 1000)
    end_time = int(now.timestamp() * 1000)
    stored_rows = FundingRateRecord.query.filter(FundingRateRecord.symbol.in_(symbols), FundingRateRecord.funding_time >= start_time, FundingRateRecord.funding_time <= end_time).all()
    stored_by_symbol = {}
    for item in stored_rows:
        stored_by_symbol.setdefault(item.symbol, []).append(item)
    output = {}
    for symbol in symbols:
        rows = sorted(stored_by_symbol.get(symbol, []), key=lambda item: item.funding_time)
        by_date = {}
        for item in rows:
            funding_date = datetime.fromtimestamp(item.funding_time / 1000, tz=timezone.utc).astimezone(SHANGHAI_TZ).date()
            by_date[funding_date] = by_date.get(funding_date, 0.0) + item.funding_rate
        output[symbol] = {"previous": rows[-1].funding_rate if rows else None, "day_1": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(1)) if rows else None, "day_3": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(3)) if rows else None, "day_7": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(7)) if rows else None, "day_30": sum(by_date.get(now.date() - timedelta(days=offset), 0.0) for offset in range(30)) if rows else None}
    return output


def enrich_funding_statistics(groups):
    statistics = funding_statistics([group["symbol"].replace("/", "") for group in groups])
    for group in groups:
        stats = statistics.get(group["symbol"].replace("/", ""), {})
        for row in group["rows"]:
            row.update({"funding_previous": stats.get("previous"), "funding_24h": stats.get("day_1", 0.0), "funding_3d": stats.get("day_3", 0.0), "funding_7d": stats.get("day_7", 0.0), "funding_30d": stats.get("day_30", 0.0)})


def contract_mid_price(group):
    row = group["rows"][0]
    return (row["short_bid"] + row["short_ask"]) / 2


def capture_price_history(groups):
    now = int(time.time())
    bucket_at = now // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    symbols = [group["symbol"] for group in groups]
    existing = {
        item.symbol for item in FuturesPriceHistory.query.filter(
            FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at == bucket_at
        ).all()
    }
    for group in groups:
        if group["symbol"] not in existing:
            db.session.add(FuturesPriceHistory(symbol=group["symbol"], bucket_at=bucket_at, price=contract_mid_price(group)))
    cutoff = now - PRICE_HISTORY_RETENTION_SECONDS
    FuturesPriceHistory.query.filter(FuturesPriceHistory.bucket_at < cutoff).delete(synchronize_session=False)
    db.session.commit()


def enrich_price_changes(groups):
    if not groups:
        return
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    target_buckets = [now_bucket - seconds for seconds in TREND_WINDOWS.values()]
    symbols = [group["symbol"] for group in groups]
    history = FuturesPriceHistory.query.filter(
        FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at.in_(target_buckets)
    ).all()
    points = {(item.symbol, item.bucket_at): item.price for item in history}
    for group in groups:
        current = contract_mid_price(group)
        for key, seconds in TREND_WINDOWS.items():
            previous = points.get((group["symbol"], now_bucket - seconds))
            change = (current - previous) / previous * 100 if previous else None
            for row in group["rows"]:
                row[key] = change


def format_funding_time(timestamp_ms):
    if not timestamp_ms:
        return None
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).astimezone(SHANGHAI_TZ).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return None


def parse_funding_time(value):
    """将已展示的上海时区结算时间恢复为可比较的时间点。"""
    if not value:
        return None
    try:
        now = datetime.now(SHANGHAI_TZ)
        parsed = datetime.strptime(f"{now.year}-{value}", "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)
        if parsed < now - timedelta(days=180):
            parsed = parsed.replace(year=parsed.year + 1)
        elif parsed > now + timedelta(days=180):
            parsed = parsed.replace(year=parsed.year - 1)
        return parsed
    except (TypeError, ValueError):
        return None


def next_settlement_boundary(interval_hours):
    """按资金费周期的上海时区整点网格推算下一次结算。"""
    try:
        interval_seconds = float(interval_hours) * 60 * 60
        if interval_seconds <= 0:
            return None
        now = datetime.now(SHANGHAI_TZ)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_seconds = (now - day_start).total_seconds()
        next_slot = int(elapsed_seconds // interval_seconds) + 1
        return day_start + timedelta(seconds=next_slot * interval_seconds)
    except (TypeError, ValueError):
        return None


def enrich_next_funding_net(groups):
    """按下一次实际发生的结算事件计算对冲后的资金费净收益。"""
    for group in groups:
        for row in group["rows"]:
            events = []
            # 结算配对统一按周期的整点边界：同为 8H 即视为同一档结算，避免各接口的 nextFundingTime 锚点不一致。
            long_time = next_settlement_boundary(row.get("long_funding_interval_hours")) or parse_funding_time(row.get("long_next_funding_time"))
            short_time = next_settlement_boundary(row.get("short_funding_interval_hours")) or parse_funding_time(row.get("short_next_funding_time"))
            if long_time and row.get("long_funding_rate") is not None:
                events.append((long_time, "long", row["long_funding_rate"]))
            if short_time and row.get("short_funding_rate") is not None:
                events.append((short_time, "short", row["short_funding_rate"]))
            if not events:
                row["funding_difference"] = None
                row["funding_settlement_label"] = "结算时间待同步"
                continue
            next_time = min(item[0] for item in events)
            settling = [item for item in events if abs((item[0] - next_time).total_seconds()) < 60]
            # 多仓：正资费支付、负资费收取；空仓：正资费收取、负资费支付。
            row["funding_difference"] = sum(-rate if side == "long" else rate for _, side, rate in settling)
            row["funding_settlement_label"] = f"{'双方结算' if len(settling) == 2 else ('仅多端结算' if settling[0][1] == 'long' else '仅空端结算')} · {next_time.strftime('%m-%d %H:%M')}"


def normalize_index_source(name):
    normalized = "".join(char for char in str(name or "").lower() if char.isalnum())
    aliases = {"okex": "okx", "binancefuture": "binancefutures", "binancefutures": "binancefutures"}
    return aliases.get(normalized, normalized)


def normalize_index_weight(value):
    try:
        weight = float(value)
        return weight / 100 if weight > 1 else weight
    except (TypeError, ValueError):
        return None


def parse_index_components(exchange, payload):
    if exchange == "Binance":
        raw_components = payload.get("constituents", [])
    elif exchange == "Bybit":
        raw_components = payload.get("result", {}).get("components", [])
    else:
        raw_components = payload.get("data", {}).get("components", [])
    components = []
    for item in raw_components:
        weight = normalize_index_weight(item.get("weight", item.get("wgt")))
        name = item.get("exchange", item.get("exch"))
        if name and weight is not None:
            components.append({"name": str(name), "source": normalize_index_source(name), "weight": weight})
    return components


def fetch_index_components(exchange, symbol):
    compact_symbol = symbol.replace("/", "")
    if exchange == "Binance":
        url = "https://fapi.binance.com/fapi/v1/constituents?" + urlencode({"symbol": compact_symbol})
    elif exchange == "Bybit":
        url = "https://api.bybit.com/v5/market/index-price-components?" + urlencode({"indexName": compact_symbol})
    else:
        url = "https://www.okx.com/api/v5/market/index-components?" + urlencode({"index": symbol.replace("/", "-")})
    payload = get_json(url, timeout=8)
    return parse_index_components(exchange, payload)


def refresh_index_components():
    """后台分批拉取指数成分；页面、排序和翻页均只读本地数据库。"""
    global INDEX_COMPONENT_CURSOR
    paths = LatestDualFuturesSnapshot.query.with_entities(
        LatestDualFuturesSnapshot.symbol, LatestDualFuturesSnapshot.long_exchange, LatestDualFuturesSnapshot.short_exchange
    ).all()
    contracts = sorted(
        {(exchange, symbol) for symbol, long_exchange, short_exchange in paths for exchange in (long_exchange, short_exchange)},
        key=lambda item: (item[1], item[0]),
    )
    if not contracts:
        return
    now = datetime.now()
    existing = {(item.exchange, item.symbol): item for item in IndexComponentSnapshot.query.all()}
    # 指数成分是低频基础资料：首次拉取后永久保存，后续页面只读数据库。
    # 新出现的合约才会进入待拉取队列；如需人工重建，由后续维护操作显式触发。
    pending = [key for key in contracts if key not in existing]
    if not pending:
        return
    batch_size = min(20, len(pending))
    start = INDEX_COMPONENT_CURSOR % len(pending)
    batch = (pending + pending)[start:start + batch_size]
    INDEX_COMPONENT_CURSOR = (start + len(batch)) % len(pending)
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_index_components, exchange, symbol): (exchange, symbol) for exchange, symbol in batch}
        for future in as_completed(futures):
            exchange, symbol = futures[future]
            try:
                components = future.result()
            except Exception:
                continue
            item = existing.get((exchange, symbol))
            if item:
                item.components_json = json.dumps(components, ensure_ascii=False)
                item.captured_at = now
            else:
                db.session.add(IndexComponentSnapshot(exchange=exchange, symbol=symbol, components_json=json.dumps(components, ensure_ascii=False), captured_at=now))
    db.session.commit()


def enrich_dual_index_overlap(groups):
    keys = {(exchange, group["symbol"]) for group in groups for row in group["rows"] for exchange in (row["long_exchange"], row["short_exchange"])}
    snapshots = IndexComponentSnapshot.query.filter(
        tuple_(IndexComponentSnapshot.exchange, IndexComponentSnapshot.symbol).in_(keys)
    ).all() if keys else []
    components_by_contract = {}
    for item in snapshots:
        try:
            components_by_contract[(item.exchange, item.symbol)] = json.loads(item.components_json)
        except (TypeError, ValueError):
            components_by_contract[(item.exchange, item.symbol)] = []
    for group in groups:
        for row in group["rows"]:
            long_components = components_by_contract.get((row["long_exchange"], group["symbol"]))
            short_components = components_by_contract.get((row["short_exchange"], group["symbol"]))
            row["long_index_components"] = long_components
            row["short_index_components"] = short_components
            if long_components is None or short_components is None:
                row["index_overlap"] = None
                row["index_status"] = "成分待同步"
                continue
            long_weights = {item["source"]: item["weight"] for item in long_components}
            short_weights = {item["source"]: item["weight"] for item in short_components}
            shared_sources = set(long_weights) & set(short_weights)
            row["index_overlap"] = sum(min(long_weights[source], short_weights[source]) for source in shared_sources) * 100
            row["index_status"] = "ok" if long_components and short_components else "指数成分不可用"


def enrich_dual_basis_references(groups):
    for group in groups:
        bases = {}
        for row in group["rows"]:
            bases[row["long_exchange"]] = row.get("long_basis")
            bases[row["short_exchange"]] = row.get("short_basis")
        for exchange, key in (("Binance", "binance_basis"), ("Bybit", "bybit_basis"), ("OKX", "okx_basis")):
            group[key] = bases.get(exchange)


def dual_contract_basis(contract):
    mark, index = contract.get("mark"), contract.get("index")
    return (mark - index) / index * 100 if mark and index else None


def refresh_okx_funding(inst_ids):
    global OKX_FUNDING_CURSOR
    now = time.time()
    pending = [inst_id for inst_id in sorted(inst_ids) if now - OKX_FUNDING_CACHE.get(inst_id, {}).get("updated_at", 0) >= 5 * 60]
    if not pending:
        return
    # OKX 的资费接口按合约返回。首轮需要尽快填满缓存，但仍控制在公开接口的安全并发范围内。
    batch_size = min(20, len(pending))
    start = OKX_FUNDING_CURSOR % len(pending)
    batch = (pending + pending)[start:start + batch_size]
    OKX_FUNDING_CURSOR = (start + len(batch)) % len(pending)

    def fetch(inst_id):
        payload = get_json("https://www.okx.com/api/v5/public/funding-rate?" + urlencode({"instId": inst_id}), timeout=1)
        data = payload.get("data", [])
        return inst_id, data[0] if data else None

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch, inst_id) for inst_id in batch]
        for future in as_completed(futures):
            try:
                inst_id, item = future.result()
                if item:
                    OKX_FUNDING_CACHE[inst_id] = {
                        "funding_rate": float(item.get("fundingRate", 0)) * 100,
                        "funding_interval_hours": max(1, round((int(item.get("nextFundingTime", 0)) - int(item.get("fundingTime", 0))) / 3_600_000)) if item.get("nextFundingTime") and item.get("fundingTime") else 8,
                        "next_funding_time": format_funding_time(item.get("nextFundingTime")),
                        "updated_at": now,
                    }
            except Exception:
                continue


def save_latest_dual_futures_snapshot(groups):
    captured_at = datetime.now()
    current_keys = {(group["symbol"], row["long_exchange"], row["short_exchange"]) for group in groups for row in group["rows"]}
    existing = {(item.symbol, item.long_exchange, item.short_exchange): item for item in LatestDualFuturesSnapshot.query.all()}
    for key, item in existing.items():
        if key not in current_keys:
            db.session.delete(item)
    for group in groups:
        for row in group["rows"]:
            key = (group["symbol"], row["long_exchange"], row["short_exchange"])
            values = {field: row.get(field) for field in (
                "long_ask", "long_bid", "short_bid", "short_ask", "long_basis", "short_basis", "long_index", "short_index", "long_volume", "short_volume", "long_open_interest", "short_open_interest", "funding_difference",
                "long_funding_rate", "short_funding_rate", "long_funding_interval_hours", "short_funding_interval_hours",
                "long_next_funding_time", "short_next_funding_time", "open_spread", "close_spread",
            )}
            values["captured_at"] = captured_at
            if key in existing:
                for field, value in values.items():
                    setattr(existing[key], field, value)
            else:
                db.session.add(LatestDualFuturesSnapshot(symbol=group["symbol"], long_exchange=row["long_exchange"], short_exchange=row["short_exchange"], **values))
    db.session.commit()


def capture_dual_futures_price_history(groups):
    now = int(time.time())
    bucket_at = now // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    prices = {}
    for group in groups:
        for row in group["rows"]:
            prices[(group["symbol"], row["long_exchange"])] = (row["long_ask"] + row["long_bid"]) / 2
            prices[(group["symbol"], row["short_exchange"])] = (row["short_ask"] + row["short_bid"]) / 2
    if not prices:
        return
    existing = {(item.symbol, item.exchange) for item in DualFuturesPriceHistory.query.filter_by(bucket_at=bucket_at).all()}
    for (symbol, exchange), price in prices.items():
        if (symbol, exchange) not in existing:
            db.session.add(DualFuturesPriceHistory(symbol=symbol, exchange=exchange, bucket_at=bucket_at, price=price))
    cutoff = now - PRICE_HISTORY_RETENTION_SECONDS
    DualFuturesPriceHistory.query.filter(DualFuturesPriceHistory.bucket_at < cutoff).delete(synchronize_session=False)
    db.session.commit()


def enrich_dual_futures_price_changes(groups):
    if not groups:
        return
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    target_buckets = [now_bucket - seconds for seconds in TREND_WINDOWS.values()]
    symbols = [group["symbol"] for group in groups]
    history = DualFuturesPriceHistory.query.filter(
        DualFuturesPriceHistory.symbol.in_(symbols), DualFuturesPriceHistory.bucket_at.in_(target_buckets)
    ).all()
    points = {(item.symbol, item.exchange, item.bucket_at): item.price for item in history}
    for group in groups:
        for row in group["rows"]:
            for side, exchange, current in (
                ("long", row["long_exchange"], (row["long_ask"] + row["long_bid"]) / 2),
                ("short", row["short_exchange"], (row["short_ask"] + row["short_bid"]) / 2),
            ):
                for key, seconds in TREND_WINDOWS.items():
                    previous = points.get((group["symbol"], exchange, now_bucket - seconds))
                    row[f"{side}_{key}"] = (current - previous) / previous * 100 if previous else None


def enrich_dual_binance_reference(groups):
    if not groups:
        return
    symbols = [group["symbol"] for group in groups]
    snapshot_rows = LatestMarketSnapshot.query.filter(LatestMarketSnapshot.symbol.in_(symbols)).all()
    references = {}
    for item in snapshot_rows:
        references.setdefault(item.symbol, {"basis": item.basis, "price": (item.short_ask + item.short_bid) / 2})
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    target_buckets = [now_bucket - seconds for seconds in TREND_WINDOWS.values()]
    history = FuturesPriceHistory.query.filter(
        FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at.in_(target_buckets)
    ).all()
    points = {(item.symbol, item.bucket_at): item.price for item in history}
    for group in groups:
        reference = references.get(group["symbol"], {})
        binance_row = next((row for row in group["rows"] if row["short_exchange"] == "Binance"), None)
        current_price = reference.get("price") or ((binance_row["short_ask"] + binance_row["short_bid"]) / 2 if binance_row else None)
        group["binance_basis"] = reference.get("basis") if reference else (binance_row.get("short_basis") if binance_row else None)
        for key, seconds in TREND_WINDOWS.items():
            previous = points.get((group["symbol"], now_bucket - seconds))
            group[f"binance_{key}"] = (current_price - previous) / previous * 100 if current_price and previous else None


def load_latest_dual_futures_snapshot():
    rows = LatestDualFuturesSnapshot.query.order_by(LatestDualFuturesSnapshot.symbol, LatestDualFuturesSnapshot.long_exchange, LatestDualFuturesSnapshot.short_exchange).all()
    if not rows:
        return None
    groups = {}
    captured_at = max(item.captured_at for item in rows)
    for item in rows:
        row = {field: getattr(item, field) for field in (
            "long_exchange", "short_exchange", "long_ask", "long_bid", "short_bid", "short_ask", "long_basis", "short_basis", "long_index", "short_index", "long_volume", "short_volume", "long_open_interest", "short_open_interest",
            "funding_difference", "long_funding_rate", "short_funding_rate", "long_funding_interval_hours", "short_funding_interval_hours",
            "long_next_funding_time", "short_next_funding_time", "open_spread", "close_spread",
        )}
        groups.setdefault(item.symbol, []).append(row)
    elapsed = max(0.0, (datetime.now() - captured_at).total_seconds())
    return {"symbols": [{"symbol": symbol, "rows": entries} for symbol, entries in groups.items()], "errors": {}, "updated_at": captured_at.strftime("%H:%M:%S"), "next_refresh_in_seconds": max(0, int(MARKET_REFRESH_SECONDS - elapsed + 0.999)), "stored": True}


def fetch_price_history_from_binance(symbol):
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    start_bucket = now_bucket - PRICE_HISTORY_RETENTION_SECONDS
    start_ms, end_ms = start_bucket * 1000, now_bucket * 1000
    rows = []
    while start_ms < end_ms:
        url = "https://fapi.binance.com/fapi/v1/klines?" + urlencode({
            "symbol": symbol.replace("/", ""), "interval": "5m", "startTime": start_ms, "endTime": end_ms - 1, "limit": 1500,
        })
        payload = get_json(url)
        if not payload:
            break
        rows.extend((int(item[0] // 1000), float(item[4])) for item in payload if int(item[0] // 1000) < now_bucket)
        next_start = int(payload[-1][0]) + PRICE_HISTORY_BUCKET_SECONDS * 1000
        if next_start <= start_ms:
            break
        start_ms = next_start
    return symbol, rows


def price_history_integrity(groups):
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    cutoff = now_bucket - 7 * 24 * 60 * 60
    symbols = [group["symbol"] for group in groups]
    counts = dict(
        db.session.query(FuturesPriceHistory.symbol, func.count(FuturesPriceHistory.id)).filter(
            FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at >= cutoff
        ).group_by(FuturesPriceHistory.symbol).all()
    )
    target_buckets = [now_bucket - seconds for seconds in TREND_WINDOWS.values()]
    target_points = {
        (item.symbol, item.bucket_at) for item in FuturesPriceHistory.query.filter(
            FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at.in_(target_buckets)
        ).all()
    }
    missing = []
    for symbol in symbols:
        has_all_trend_points = all((symbol, bucket_at) in target_points for bucket_at in target_buckets)
        if counts.get(symbol, 0) < 2000 or not has_all_trend_points:
            missing.append(symbol)
    return {"total": len(symbols), "complete": len(symbols) - len(missing), "missing_symbols": missing}


def backfill_price_history(groups, batch_size=2):
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    integrity = price_history_integrity(groups)
    batch = integrity["missing_symbols"][:batch_size]
    if not batch:
        return
    fetched = []
    with ThreadPoolExecutor(max_workers=batch_size) as executor:
        futures = [executor.submit(fetch_price_history_from_binance, symbol) for symbol in batch]
        for future in as_completed(futures):
            try:
                fetched.append(future.result())
            except Exception:
                continue
    if not fetched:
        return
    symbols = [symbol for symbol, _ in fetched]
    existing = {
        (item.symbol, item.bucket_at) for item in FuturesPriceHistory.query.filter(
            FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at >= now_bucket - PRICE_HISTORY_RETENTION_SECONDS
        ).all()
    }
    for symbol, rows in fetched:
        for bucket_at, price in rows:
            if (symbol, bucket_at) not in existing:
                db.session.add(FuturesPriceHistory(symbol=symbol, bucket_at=bucket_at, price=price))
    db.session.commit()


def create_alert(symbol, alert_type, message, row, strategy="spot_futures", long_exchange=None, short_exchange="Binance"):
    recent = AlertEvent.query.filter_by(
        symbol=symbol,
        strategy=strategy,
        alert_type=alert_type,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
    ).order_by(AlertEvent.created_at.desc()).first()
    current_open = float(row.get("open_spread") or 0)
    current_basis = float(row.get("basis") or 0)
    if "basis" in alert_type:
        current_abs = abs(current_basis)
        recent_abs = abs(recent.basis or 0) if recent else None
    else:
        current_abs = abs(current_open)
        recent_abs = abs(recent.open_spread or 0) if recent else None
    if recent and current_abs < recent_abs + 0.2:
        return
    recent_window = datetime.now() - timedelta(minutes=30)
    recent_same_type = AlertEvent.query.filter(
        AlertEvent.symbol == symbol,
        AlertEvent.strategy == strategy,
        AlertEvent.alert_type == alert_type,
        AlertEvent.created_at >= recent_window,
    ).all()
    if recent_same_type:
        if "basis" in alert_type:
            peak_abs = max(abs(item.basis or 0) for item in recent_same_type)
        else:
            peak_abs = max(abs(item.open_spread or 0) for item in recent_same_type)
        if current_abs < peak_abs + 0.2:
            return
    # 扩大后的确认信号作为新的时间线节点保留；界面会按币种聚合，避免重复卡片。
    db.session.add(AlertEvent(symbol=symbol, strategy=strategy, long_exchange=long_exchange, short_exchange=short_exchange, alert_type=alert_type, message=message, open_spread=row["open_spread"], close_spread=row["close_spread"], basis=row["basis"], funding_rate=row["funding_rate"]))


def rapid_move_alerts(key, metric, value):
    """Return confirmed 30s absolute-expansion triggers with a 30-minute widening window."""
    now = time.time()
    history = [(at, observed) for at, observed in RAPID_MOVE_HISTORY.get((key, metric), []) if now - at <= 65]
    history.append((now, value))
    RAPID_MOVE_HISTORY[(key, metric)] = history
    triggered = []
    seconds = 30
    baseline = next((observed for at, observed in history if now - at >= seconds - 5), None)
    expanded = abs(value) - abs(baseline) if baseline is not None else 0.0
    for threshold in ((1.0,) if expanded >= 1.0 else (0.5,)):
        candidate_key = (key, metric, seconds, threshold)
        active = baseline is not None and expanded >= threshold
        RAPID_MOVE_CANDIDATES[candidate_key] = RAPID_MOVE_CANDIDATES.get(candidate_key, 0) + 1 if active else 0
        if active and RAPID_MOVE_CANDIDATES[candidate_key] == 2:
            window_key = (key, metric)
            window = RAPID_MOVE_ALERT_WINDOWS.get(window_key)
            current_abs = abs(value)
            expired = not window or now - window["started_at"] >= 30 * 60
            widened = bool(window and current_abs >= window["max_abs"] + 0.2)
            if expired or widened:
                RAPID_MOVE_ALERT_WINDOWS[window_key] = {"started_at": now, "max_abs": current_abs}
                triggered.append((seconds, threshold, expanded))
    return triggered


def track_basis(symbol, row, active_by_symbol, active_by_key, strategy="spot_futures"):
    basis = row["basis"]
    absolute = abs(basis)
    direction = "positive" if basis > 0 else "negative"
    candidate_key = (strategy, symbol)
    candidate = BASIS_CANDIDATES.get(candidate_key, {"count": 0, "direction": direction})
    if absolute >= 1 and candidate["direction"] == direction:
        candidate["count"] += 1
    elif absolute >= 1:
        candidate = {"count": 1, "direction": direction}
    else:
        BASIS_CANDIDATES.pop(candidate_key, None)
        active = active_by_symbol.get((strategy, symbol))
        if active and absolute < 0.8:
            active.resolved_at = datetime.now()
        return
    BASIS_CANDIDATES[candidate_key] = candidate
    if candidate["count"] < 2:
        return
    active = active_by_key.get((strategy, symbol, direction))
    now = datetime.now()
    if not active:
        active = BasisTracking(symbol=symbol, strategy=strategy, direction=direction, opening_basis=basis, last_recorded_level=1.0, max_basis=basis, max_abs_basis=absolute, max_at=now)
        db.session.add(active)
        db.session.flush()
        active_by_symbol[(strategy, symbol)] = active
        active_by_key[(strategy, symbol, direction)] = active
        db.session.add(BasisExpansionLog(tracking_id=active.id, level=1.0, observed_basis=basis))
        if strategy == "spot_futures":
            create_alert(symbol, "basis_threshold", "基差连续两次越过 ±1% 阈值", row)
    if absolute > active.max_abs_basis:
        active.max_abs_basis, active.max_basis, active.max_at = absolute, basis, now
    while absolute >= round(active.last_recorded_level + 0.2, 1):
        active.last_recorded_level = round(active.last_recorded_level + 0.2, 1)
        db.session.add(BasisExpansionLog(tracking_id=active.id, level=active.last_recorded_level, observed_basis=basis))


def confirmed_spot_book(exchange, symbol):
    try:
        if exchange == "Gate":
            payload = get_json("https://api.gateio.ws/api/v4/spot/order_book?" + urlencode({"currency_pair": f"{symbol[:-4]}_USDT", "limit": 1}), timeout=0.25)
            asks, bids = payload.get("asks", []), payload.get("bids", [])
            return valid_book(asks[0][0] if asks else None, bids[0][0] if bids else None)
        if exchange == "Bitget":
            payload = get_json("https://api.bitget.com/api/v2/spot/market/orderbook?" + urlencode({"symbol": symbol, "limit": 1}), timeout=0.25)
            book = payload.get("data", {})
            asks, bids = book.get("asks", []), book.get("bids", [])
            return valid_book(asks[0][0] if asks else None, bids[0][0] if bids else None)
        payload = get_json("https://api.binance.com/api/v3/depth?" + urlencode({"symbol": symbol, "limit": 5}), timeout=0.25)
        asks, bids = payload.get("asks", []), payload.get("bids", [])
        return valid_book(asks[0][0] if asks else None, bids[0][0] if bids else None)
    except Exception:
        return None


def validate_spot_alert_quote(symbol, row, alert_type):
    """报警前以单币种一档订单簿复核，过滤聚合 ticker 的错误价、延迟价与插针。"""
    cache_key = (symbol, row["long_exchange"], alert_type)
    cached = QUOTE_VALIDATION_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < (5 if cached[1] else 120):
        return cached[1]
    spot_book = confirmed_spot_book(row["long_exchange"], symbol.replace("/", ""))
    try:
        future = get_json("https://fapi.binance.com/fapi/v1/ticker/bookTicker?" + urlencode({"symbol": symbol.replace("/", "")}), timeout=0.25)
        futures_book = valid_book(future.get("askPrice"), future.get("bidPrice"))
    except Exception:
        futures_book = None
    if not spot_book or not futures_book:
        QUOTE_VALIDATION_CACHE[cache_key] = (time.time(), False)
        return False
    open_spread = (futures_book["bid"] - spot_book["ask"]) / spot_book["ask"] * 100
    close_spread = (futures_book["ask"] - spot_book["bid"]) / spot_book["bid"] * 100
    observed_close = row.get("close_spread")
    # 复核报价必须与快照同向、同量级；超过 0.4% 的偏离按失真报价处理。
    if abs(open_spread - row["open_spread"]) > 0.4 or (observed_close is not None and abs(close_spread - observed_close) > 0.4):
        QUOTE_VALIDATION_CACHE[cache_key] = (time.time(), False)
        return False
    if abs(open_spread - close_spread) >= 0.6:
        QUOTE_VALIDATION_CACHE[cache_key] = (time.time(), False)
        return False
    result = True if alert_type.startswith("rapid_") else (open_spread > 1 if alert_type == "futures_pump" else open_spread < -1)
    QUOTE_VALIDATION_CACHE[cache_key] = (time.time(), result)
    return result


def evaluate_alerts(groups):
    active_trackings = BasisTracking.query.filter_by(resolved_at=None).all()
    active_by_symbol = {(item.strategy or "spot_futures", item.symbol): item for item in active_trackings}
    active_by_key = {(item.strategy or "spot_futures", item.symbol, item.direction): item for item in active_trackings}
    for group in groups:
        symbol = group["symbol"]
        first_row = group["rows"][0]
        track_basis(symbol, first_row, active_by_symbol, active_by_key)
        for row in group["rows"]:
            path_key = ("spot_futures", symbol, row["long_exchange"], "Binance")
            for metric, value in (("开差", float(row.get("open_spread") or 0)), ("基差", float(row.get("basis") or 0))):
                for seconds, threshold, expanded in rapid_move_alerts(path_key, metric, value):
                    alert_type = f"rapid_{'spread' if metric == '开差' else 'basis'}"
                    if validate_spot_alert_quote(symbol, row, alert_type):
                        create_alert(symbol, alert_type, f"{row['long_exchange']} 现货与 Binance 合约：{seconds} 秒内{metric}绝对值扩大 {expanded:.3f}%（阈值 {threshold:.1f}%）", row, "spot_futures", row["long_exchange"], "Binance")
        futures_rows = [row for row in group["rows"] if row["open_spread"] > 1 and abs(row["open_spread"] - row["close_spread"]) < 0.6]
        spot_rows = [row for row in group["rows"] if row["open_spread"] < -1 and abs(row["open_spread"] - row["close_spread"]) < 0.6]
        active_pump_key = None
        if futures_rows:
            row = max(futures_rows, key=lambda item: item["open_spread"])
            active_pump_key = (symbol, "futures_pump", row["long_exchange"])
        elif spot_rows:
            row = min(spot_rows, key=lambda item: item["open_spread"])
            active_pump_key = (symbol, "spot_pump", row["long_exchange"])

        # 合约/现货拉升与基差异动一样需要连续两次采样确认：交易所路径、方向或阈值任一项失效即重置，单次插针不会写入报警。
        for candidate_key in list(PUMP_CANDIDATES):
            if candidate_key[0] == symbol and candidate_key != active_pump_key:
                PUMP_CANDIDATES.pop(candidate_key, None)
        if active_pump_key:
            PUMP_CANDIDATES[active_pump_key] = PUMP_CANDIDATES.get(active_pump_key, 0) + 1
            if PUMP_CANDIDATES[active_pump_key] >= 2:
                alert_type, exchange = active_pump_key[1], active_pump_key[2]
                label = "合约拉升" if alert_type == "futures_pump" else "现货拉升"
                if validate_spot_alert_quote(symbol, row, alert_type):
                    create_alert(symbol, alert_type, f"{exchange} 现货与 Binance 合约出现确认后的{label}", row, "spot_futures", row["long_exchange"], "Binance")
                else:
                    PUMP_CANDIDATES.pop(active_pump_key, None)
    db.session.commit()


def evaluate_dual_alerts(groups):
    """期多期空报警：连续两次确认，之后仅在绝对值扩大 0.2% 时覆盖同币种报警。"""
    active_trackings = BasisTracking.query.filter_by(resolved_at=None).all()
    active_by_symbol = {(item.strategy or "spot_futures", item.symbol): item for item in active_trackings}
    active_by_key = {(item.strategy or "spot_futures", item.symbol, item.direction): item for item in active_trackings}
    for group in groups:
        symbol = group["symbol"]
        bn_row = next((row for row in group["rows"] if row["long_exchange"] == "Binance" or row["short_exchange"] == "Binance"), None)
        if bn_row:
            bn_basis = bn_row.get("long_basis") if bn_row["long_exchange"] == "Binance" else bn_row.get("short_basis")
            if bn_basis is not None:
                track_basis(symbol, {**bn_row, "basis": bn_basis, "funding_rate": bn_row.get("short_funding_rate")}, active_by_symbol, active_by_key, "futures_futures")
        active_keys = set()
        for row in group["rows"]:
            path = (symbol, row["long_exchange"], row["short_exchange"])
            bn_basis = row.get("long_basis") if row["long_exchange"] == "Binance" else row.get("short_basis") if row["short_exchange"] == "Binance" else None
            alert_row = {**row, "basis": bn_basis if bn_basis is not None else 0.0, "funding_rate": row.get("short_funding_rate")}
            for metric, value in (("开差", float(row.get("open_spread") or 0)), ("BN 基差", float(bn_basis or 0))):
                if metric == "BN 基差" and bn_basis is None:
                    continue
                for seconds, threshold, expanded in rapid_move_alerts(("futures_futures", *path), metric, value):
                    create_alert(symbol, f"rapid_{'spread' if metric == '开差' else 'basis'}", f"{row['long_exchange']} 合约与 {row['short_exchange']} 合约：{seconds} 秒内{metric}绝对值扩大 {expanded:.3f}%（阈值 {threshold:.1f}%）", alert_row, "futures_futures", row["long_exchange"], row["short_exchange"])
            checks = []
            if abs(float(row.get("open_spread") or 0)) >= 1:
                checks.append(("dual_spread_threshold", "开差"))
            if bn_basis is not None and abs(float(bn_basis)) >= 1:
                checks.append(("dual_basis_threshold", "Binance 基差"))
            for alert_type, label in checks:
                key = (*path, alert_type)
                active_keys.add(key)
                observed = float(bn_basis if alert_type == "dual_basis_threshold" else row.get("open_spread") or 0)
                previous = DUAL_ALERT_VALUES.get(key)
                DUAL_ALERT_VALUES[key] = observed
                DUAL_ALERT_CANDIDATES[key] = DUAL_ALERT_CANDIDATES.get(key, 0) + 1
                # A single bad quote must never create a futures/futures alert.
                # The same path and threshold need to survive two consecutive snapshots.
                stable = previous is not None and previous * observed > 0 and abs(previous - observed) <= 0.6
                if DUAL_ALERT_CANDIDATES[key] < 2 or not stable:
                    continue
                message = f"{row['long_exchange']} 合约与 {row['short_exchange']} 合约出现确认后的{label}异动"
                create_alert(symbol, alert_type, message, alert_row, "futures_futures", row["long_exchange"], row["short_exchange"])
        for key in [item for item in DUAL_ALERT_CANDIDATES if item[0] == symbol and item not in active_keys]:
            DUAL_ALERT_CANDIDATES.pop(key, None)
            DUAL_ALERT_VALUES.pop(key, None)
    db.session.commit()


def enrich_basis_openings(groups, strategy):
    symbols = [group["symbol"] for group in groups]
    if not symbols:
        return
    trackings = BasisTracking.query.filter(BasisTracking.strategy == strategy, BasisTracking.symbol.in_(symbols)).order_by(BasisTracking.started_at.desc()).all()
    latest = {}
    for item in trackings:
        latest.setdefault(item.symbol, item)
    tracking_ids = [item.id for item in latest.values()]
    logs_by_tracking = {}
    if tracking_ids:
        for log in BasisExpansionLog.query.filter(BasisExpansionLog.tracking_id.in_(tracking_ids)).order_by(BasisExpansionLog.created_at).all():
            logs_by_tracking.setdefault(log.tracking_id, []).append(log)
    for group in groups:
        tracking = latest.get(group["symbol"])
        if not tracking:
            group["basis_opening"] = None
            continue
        logs = logs_by_tracking.get(tracking.id, [])
        opening_basis = tracking.opening_basis if tracking.opening_basis is not None else (logs[0].observed_basis if logs else tracking.max_basis)
        group["basis_opening"] = {
            "opened_at": tracking.started_at.strftime("%m-%d %H:%M:%S"),
            "opened_basis": opening_basis,
            "max_basis": tracking.max_basis,
            "max_at": tracking.max_at.strftime("%m-%d %H:%M:%S"),
            "open_count": len(logs),
        }


def save_latest_market_snapshot(groups):
    captured_at = datetime.now()
    symbols = [group["symbol"] for group in groups]
    existing = {
        (item.symbol, item.long_exchange): item for item in LatestMarketSnapshot.query.filter(
            LatestMarketSnapshot.symbol.in_(symbols)
        ).all()
    }
    updates, inserts = [], []
    for group in groups:
        for row in group["rows"]:
            key = (group["symbol"], row["long_exchange"])
            item = existing.get(key)
            values = {"short_exchange": row["short_exchange"], "long_ask": row["long_ask"], "long_bid": row["long_bid"], "short_bid": row["short_bid"], "short_ask": row["short_ask"], "basis": row["basis"], "funding_rate": row["funding_rate"], "funding_interval_hours": row["funding_interval_hours"], "next_funding_time": row["next_funding_time"], "spot_volume": row.get("spot_volume"), "futures_volume": row.get("futures_volume"), "futures_open_interest": row.get("futures_open_interest"), "open_spread": row["open_spread"], "close_spread": row["close_spread"], "captured_at": captured_at}
            if item:
                updates.append({"id": item.id, **values})
            else:
                inserts.append({"symbol": group["symbol"], "long_exchange": row["long_exchange"], **values})
    if updates:
        db.session.bulk_update_mappings(LatestMarketSnapshot, updates)
    if inserts:
        db.session.bulk_insert_mappings(LatestMarketSnapshot, inserts)
    db.session.commit()


def load_latest_market_snapshot():
    rows = LatestMarketSnapshot.query.order_by(LatestMarketSnapshot.symbol, LatestMarketSnapshot.long_exchange).all()
    if not rows:
        return None
    groups = {}
    captured_at = max(item.captured_at for item in rows)
    for item in rows:
        groups.setdefault(item.symbol, []).append({"long_exchange": item.long_exchange, "long_ask": item.long_ask, "long_bid": item.long_bid, "short_exchange": item.short_exchange, "short_bid": item.short_bid, "short_ask": item.short_ask, "basis": item.basis, "funding_rate": item.funding_rate, "funding_interval_hours": item.funding_interval_hours, "next_funding_time": item.next_funding_time, "spot_volume": item.spot_volume, "futures_volume": item.futures_volume, "futures_open_interest": item.futures_open_interest, "open_spread": item.open_spread, "close_spread": item.close_spread})
    elapsed = max(0.0, (datetime.now() - captured_at).total_seconds())
    next_refresh_in_seconds = max(0, int(MARKET_REFRESH_SECONDS - elapsed + 0.999))
    return {"symbols": [{"symbol": symbol, "rows": entries} for symbol, entries in groups.items()], "errors": {}, "updated_at": captured_at.strftime("%H:%M:%S"), "next_refresh_in_seconds": next_refresh_in_seconds, "stored": True}


def spot_futures_snapshot():
    global MARKET_REFRESH_METRICS
    started_at = time.perf_counter()
    stage_at = started_at
    urls = {
        "futures_info": "https://fapi.binance.com/fapi/v1/exchangeInfo",
        "futures_books": "https://fapi.binance.com/fapi/v1/ticker/bookTicker",
        "futures_24h": "https://fapi.binance.com/fapi/v1/ticker/24hr",
        "funding": "https://fapi.binance.com/fapi/v1/premiumIndex",
        "funding_info": "https://fapi.binance.com/fapi/v1/fundingInfo",
        "binance_spot": "https://api.binance.com/api/v3/ticker/bookTicker",
        "binance_spot_24h": "https://api.binance.com/api/v3/ticker/24hr",
        "gate_spot": "https://api.gateio.ws/api/v4/spot/tickers",
        "bitget_spot": "https://api.bitget.com/api/v2/spot/market/tickers",
    }
    results, errors = fetch_market_payloads(urls, {
        "futures_info": 3600, "funding_info": 60, "futures_24h": 20,
        "binance_spot_24h": 20,
    })
    network_seconds = time.perf_counter() - started_at

    required = {"futures_info", "futures_books", "funding"}
    if not required.issubset(results):
        raise RuntimeError("Binance 合约行情暂时不可用，请稍后刷新。")

    refresh_rwa_stock_symbols(results["futures_info"].get("symbols", []))
    sync_binance_listing_events_if_due(results["futures_info"].get("symbols", []))
    futures_books = {item["symbol"]: valid_book(item.get("askPrice"), item.get("bidPrice")) for item in results["futures_books"]}
    futures_volumes = {item["symbol"]: float(item.get("quoteVolume", 0) or 0) for item in results.get("futures_24h", [])}
    funding = {item["symbol"]: item for item in results["funding"]}
    intervals = {item["symbol"]: item.get("fundingIntervalHours", 8) for item in results.get("funding_info", [])}
    spot_books = {"Binance": {}, "Gate": {}, "Bitget": {}}
    spot_volumes = {"Binance": {}, "Gate": {}, "Bitget": {}}
    for item in results.get("binance_spot", []):
        book = valid_book(item.get("askPrice"), item.get("bidPrice"))
        if book:
            spot_books["Binance"][item["symbol"]] = book
    for item in results.get("binance_spot_24h", []):
        if item.get("symbol", "").endswith("USDT"):
            spot_volumes["Binance"][item["symbol"]] = float(item.get("quoteVolume", 0) or 0)
    for item in results.get("gate_spot", []):
        pair = item.get("currency_pair", "").replace("_", "")
        book = valid_book(item.get("lowest_ask"), item.get("highest_bid"))
        if book and pair.endswith("USDT"):
            spot_books["Gate"][pair] = book
            spot_volumes["Gate"][pair] = float(item.get("quote_volume", 0) or 0)
    for item in results.get("bitget_spot", {}).get("data", []):
        book = valid_book(item.get("askPr"), item.get("bidPr"))
        if book:
            spot_books["Bitget"][item.get("symbol")] = book
            spot_volumes["Bitget"][item.get("symbol")] = float(item.get("usdtVolume", 0) or 0)

    rows_by_symbol = []
    symbols = sorted(item["symbol"] for item in results["futures_info"].get("symbols", []) if item.get("status") == "TRADING" and item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT")
    refresh_binance_open_interest(symbols)
    oi_finished_at = time.perf_counter()
    for symbol in symbols:
        futures_book, funding_item = futures_books.get(symbol), funding.get(symbol)
        if not futures_book or not funding_item:
            continue
        rows = []
        for exchange in ("Binance", "Gate", "Bitget"):
            book = spot_books[exchange].get(symbol)
            if not book:
                continue
            open_spread = (futures_book["bid"] - book["ask"]) / book["ask"] * 100
            close_spread = (futures_book["ask"] - book["bid"]) / book["bid"] * 100
            contract_basis = (float(funding_item["markPrice"]) - float(funding_item["indexPrice"])) / float(funding_item["indexPrice"]) * 100
            oi_contracts = BINANCE_OPEN_INTEREST_CACHE.get(symbol, {}).get("contracts")
            rows.append({"long_exchange": exchange, "long_ask": book["ask"], "long_bid": book["bid"], "short_exchange": "Binance 合约", "short_bid": futures_book["bid"], "short_ask": futures_book["ask"], "basis": contract_basis, "funding_rate": float(funding_item["lastFundingRate"]) * 100, "funding_interval_hours": intervals.get(symbol, 8), "next_funding_time": datetime.fromtimestamp(int(funding_item["nextFundingTime"]) / 1000, tz=timezone.utc).astimezone().strftime("%m-%d %H:%M"), "spot_volume": spot_volumes[exchange].get(symbol), "futures_volume": futures_volumes.get(symbol), "futures_open_interest": oi_contracts * float(funding_item.get("markPrice", 0) or 0) if oi_contracts is not None else None, "open_spread": open_spread, "close_spread": close_spread})
        if rows:
            rows_by_symbol.append({"symbol": f"{symbol[:-4]}/USDT", "rows": rows})
    rows_finished_at = time.perf_counter()
    evaluate_alerts(rows_by_symbol)
    alerts_finished_at = time.perf_counter()
    save_latest_market_snapshot(rows_by_symbol)
    snapshot_finished_at = time.perf_counter()
    capture_price_history(rows_by_symbol)
    total_seconds = time.perf_counter() - started_at
    MARKET_REFRESH_METRICS = {
        "network_seconds": round(network_seconds, 3),
        "processing_seconds": round(total_seconds - network_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "last_success_at": datetime.now().strftime("%H:%M:%S"),
        "last_error": None,
        "stages": {
            "open_interest": round(oi_finished_at - stage_at - network_seconds, 3),
            "build_rows": round(rows_finished_at - oi_finished_at, 3),
            "alerts": round(alerts_finished_at - rows_finished_at, 3),
            "snapshot_write": round(snapshot_finished_at - alerts_finished_at, 3),
            "history_write": round(total_seconds - (snapshot_finished_at - started_at), 3),
        },
    }
    return {"symbols": rows_by_symbol, "errors": errors, "updated_at": datetime.now().strftime("%H:%M:%S")}


def dual_futures_snapshot():
    urls = {
        "binance_books": "https://fapi.binance.com/fapi/v1/ticker/bookTicker",
        "binance_24h": "https://fapi.binance.com/fapi/v1/ticker/24hr",
        "binance_funding": "https://fapi.binance.com/fapi/v1/premiumIndex",
        "binance_funding_info": "https://fapi.binance.com/fapi/v1/fundingInfo",
        "binance_info": "https://fapi.binance.com/fapi/v1/exchangeInfo",
        "bybit_instruments": "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000",
        "bybit_tickers": "https://api.bybit.com/v5/market/tickers?category=linear",
        "okx_instruments": "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
        "okx_tickers": "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
        "okx_mark_prices": "https://www.okx.com/api/v5/public/mark-price?instType=SWAP",
        "okx_index_tickers": "https://www.okx.com/api/v5/market/index-tickers?quoteCcy=USDT",
        "okx_open_interest": "https://www.okx.com/api/v5/public/open-interest?instType=SWAP",
    }
    results, errors = fetch_market_payloads(urls, {
        "binance_24h": 20, "binance_funding_info": 60, "binance_info": 3600,
        "bybit_instruments": 3600, "okx_instruments": 3600,
    })
    required = {"binance_books", "binance_funding", "bybit_instruments", "bybit_tickers", "okx_instruments", "okx_tickers", "okx_mark_prices", "okx_index_tickers"}
    if not required.issubset(results):
        raise RuntimeError("期多期空行情暂时未能完成同步")

    refresh_rwa_stock_symbols(results.get("binance_info", {}).get("symbols", []))
    binance_books = {item["symbol"]: valid_book(item.get("askPrice"), item.get("bidPrice")) for item in results["binance_books"]}
    binance_volumes = {item["symbol"]: float(item.get("quoteVolume", 0) or 0) for item in results.get("binance_24h", [])}
    binance_funding = {item["symbol"]: item for item in results["binance_funding"]}
    binance_intervals = {item["symbol"]: item.get("fundingIntervalHours", 8) for item in results.get("binance_funding_info", [])}
    refresh_binance_open_interest(binance_books.keys())
    binance = {}
    for symbol, book in binance_books.items():
        funding = binance_funding.get(symbol)
        if not book or not funding or not symbol.endswith("USDT"):
            continue
        mark = float(funding.get("markPrice", 0) or 0)
        oi_contracts = BINANCE_OPEN_INTEREST_CACHE.get(symbol, {}).get("contracts")
        binance[symbol] = {**book, "mark": mark, "index": float(funding.get("indexPrice", 0) or 0), "volume": binance_volumes.get(symbol), "open_interest": oi_contracts * mark if oi_contracts is not None else None, "funding_rate": float(funding.get("lastFundingRate", 0) or 0) * 100, "funding_interval_hours": binance_intervals.get(symbol, 8), "next_funding_time": format_funding_time(funding.get("nextFundingTime"))}

    bybit_intervals = {
        item.get("symbol"): max(1, float(item.get("fundingInterval", 480) or 480) / 60)
        for item in results["bybit_instruments"].get("result", {}).get("list", [])
        if item.get("status") == "Trading" and item.get("contractType") == "LinearPerpetual" and item.get("settleCoin") == "USDT"
    }
    bybit_allowed = set(bybit_intervals)
    bybit = {}
    for item in results["bybit_tickers"].get("result", {}).get("list", []):
        symbol = item.get("symbol")
        book = valid_book(item.get("ask1Price"), item.get("bid1Price"))
        if symbol not in bybit_allowed or not book:
            continue
        bybit[symbol] = {**book, "mark": float(item.get("markPrice", 0) or 0), "index": float(item.get("indexPrice", 0) or 0), "volume": float(item.get("turnover24h", 0) or 0), "open_interest": float(item.get("openInterestValue", 0) or 0), "funding_rate": float(item.get("fundingRate", 0) or 0) * 100, "funding_interval_hours": bybit_intervals.get(symbol, 8), "next_funding_time": format_funding_time(item.get("nextFundingTime"))}

    okx_allowed = {
        item.get("instId") for item in results["okx_instruments"].get("data", [])
        if item.get("state") == "live" and item.get("settleCcy") == "USDT" and item.get("instId", "").endswith("-USDT-SWAP")
    }
    okx_marks = {item.get("instId"): float(item.get("markPx", 0) or 0) for item in results["okx_mark_prices"].get("data", [])}
    okx_indexes = {item.get("instId", "").replace("-", ""): float(item.get("idxPx", 0) or 0) for item in results["okx_index_tickers"].get("data", [])}
    okx_open_interest = {item.get("instId"): float(item.get("oiUsd", 0) or 0) for item in results.get("okx_open_interest", {}).get("data", [])}
    okx = {}
    for item in results["okx_tickers"].get("data", []):
        inst_id = item.get("instId")
        book = valid_book(item.get("askPx"), item.get("bidPx"))
        if inst_id not in okx_allowed or not book:
            continue
        symbol = inst_id.replace("-", "").replace("SWAP", "")
        cached_funding = OKX_FUNDING_CACHE.get(inst_id, {})
        okx[symbol] = {**book, "mark": okx_marks.get(inst_id), "index": okx_indexes.get(inst_id.replace("-SWAP", "").replace("-", "")), "volume": float(item.get("volCcy24h", 0) or 0), "open_interest": okx_open_interest.get(inst_id), "funding_rate": cached_funding.get("funding_rate"), "funding_interval_hours": cached_funding.get("funding_interval_hours"), "next_funding_time": cached_funding.get("next_funding_time"), "okx_inst_id": inst_id}
    refresh_okx_funding([item["okx_inst_id"] for item in okx.values()])
    for contract in okx.values():
        cached_funding = OKX_FUNDING_CACHE.get(contract["okx_inst_id"], {})
        contract.update({key: cached_funding.get(key) for key in ("funding_rate", "funding_interval_hours", "next_funding_time")})

    contracts = {"Binance": binance, "Bybit": bybit, "OKX": okx}
    paths = (("Bybit", "Binance"), ("OKX", "Binance"), ("Bybit", "OKX"))
    groups = []
    all_symbols = sorted(set(binance) | set(bybit) | set(okx))
    for symbol in all_symbols:
        rows = []
        for long_exchange, short_exchange in paths:
            long_contract, short_contract = contracts[long_exchange].get(symbol), contracts[short_exchange].get(symbol)
            if not long_contract or not short_contract:
                continue
            # 个别交易所会出现相同代码对应不同标的的合约。两端中间价相差超过 50% 时，
            # 不把它当作真实价差机会，避免将代码碰撞误报为“主力拉升”。
            long_mid = (long_contract["ask"] + long_contract["bid"]) / 2
            short_mid = (short_contract["ask"] + short_contract["bid"]) / 2
            price_ratio = short_mid / long_mid if long_mid else 0
            if not 0.5 <= price_ratio <= 1.5:
                continue
            long_basis, short_basis = dual_contract_basis(long_contract), dual_contract_basis(short_contract)
            long_funding, short_funding = long_contract.get("funding_rate"), short_contract.get("funding_rate")
            rows.append({
                "long_exchange": long_exchange, "short_exchange": short_exchange,
                "long_ask": long_contract["ask"], "long_bid": long_contract["bid"], "short_bid": short_contract["bid"], "short_ask": short_contract["ask"],
                "long_basis": long_basis, "short_basis": short_basis,
                "long_index": long_contract.get("index"), "short_index": short_contract.get("index"),
                "long_volume": long_contract.get("volume"), "short_volume": short_contract.get("volume"),
                "long_open_interest": long_contract.get("open_interest"), "short_open_interest": short_contract.get("open_interest"),
                "funding_difference": None,
                "long_funding_rate": long_funding, "short_funding_rate": short_funding,
                "long_funding_interval_hours": long_contract.get("funding_interval_hours"), "short_funding_interval_hours": short_contract.get("funding_interval_hours"),
                "long_next_funding_time": long_contract.get("next_funding_time"), "short_next_funding_time": short_contract.get("next_funding_time"),
                "open_spread": (short_contract["bid"] - long_contract["ask"]) / long_contract["ask"] * 100,
                "close_spread": (short_contract["ask"] - long_contract["bid"]) / long_contract["bid"] * 100,
            })
        if rows:
            groups.append({"symbol": f"{symbol[:-4]}/USDT", "rows": rows})
    enrich_next_funding_net(groups)
    evaluate_dual_alerts(groups)
    save_latest_dual_futures_snapshot(groups)
    return {"symbols": groups, "errors": errors, "updated_at": datetime.now().strftime("%H:%M:%S")}


def opportunities():
    rows = []
    for symbol, details in MARKETS.items():
        quotes = {exchange: details["base"] * (1 + random.uniform(-0.0025, 0.0025)) for exchange in EXCHANGES}
        buy_exchange = min(quotes, key=quotes.get)
        sell_exchange = max(quotes, key=quotes.get)
        buy, sell = quotes[buy_exchange], quotes[sell_exchange]
        spread = (sell / buy - 1) * 100
        estimated = spread - 0.12
        rows.append({
            "symbol": symbol,
            "buy_exchange": buy_exchange,
            "sell_exchange": sell_exchange,
            "buy_price": round(buy, 2),
            "sell_price": round(sell, 2),
            "spread": round(spread, 3),
            "estimated_profit": round(estimated, 3),
            "funding": details["funding"],
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        })
    return sorted(rows, key=lambda item: item["estimated_profit"], reverse=True)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/dashboard")
def dashboard():
    items = opportunities()
    active = Strategy.query.filter_by(enabled=True).count()
    return jsonify({
        "opportunities": items,
        "summary": {
            "active_strategies": active,
            "best_spread": items[0]["spread"],
            "markets_scanned": len(items) * len(EXCHANGES),
            "mode": "现多期空 · 公开 API",
        },
    })


@app.get("/api/spot-futures")
def spot_futures():
    page = max(request.args.get("page", 1, type=int), 1)
    page_size = 30
    binance_spot_only = request.args.get("binance_spot_only") == "1"
    funding_interval = request.args.get("funding_interval", "all").upper()
    if funding_interval not in {"ALL", "1H", "4H", "8H"}:
        funding_interval = "ALL"
    raw_symbol_query = "".join(request.args.get("symbol", "").upper().split())
    symbol_query = raw_symbol_query.replace("/", "").replace("-", "")
    sort_by = request.args.get("sort_by", "open_spread")
    sort_direction = request.args.get("sort_direction", "desc")
    if sort_by not in {"basis", "funding_rate", "funding_previous", "funding_24h", "funding_3d", "funding_7d", "funding_30d", "change_5m", "change_15m", "change_30m", "change_1h", "change_12h", "change_24h", "change_3d", "change_7d", "open_spread", "close_spread"}:
        sort_by = "open_spread"
    if sort_direction not in {"asc", "desc"}:
        sort_direction = "desc"
    snapshot = load_latest_market_snapshot()
    if not snapshot:
        return jsonify({"error": "行情正在进行首轮同步，请稍后刷新。"}), 503
    global SPOT_VIEW_CACHE
    snapshot_key = snapshot["updated_at"]
    if SPOT_VIEW_CACHE["key"] != snapshot_key:
        enrich_funding_statistics(snapshot["symbols"])
        enrich_price_changes(snapshot["symbols"])
        enrich_basis_openings(snapshot["symbols"], "spot_futures")
        enrich_transfer_networks(snapshot["symbols"])
        SPOT_VIEW_CACHE = {"key": snapshot_key, "symbols": snapshot["symbols"]}
    mark_announced_delistings(SPOT_VIEW_CACHE["symbols"])
    symbols = [group for group in SPOT_VIEW_CACHE["symbols"] if not is_rwa_stock_pair(group["symbol"])]
    if funding_interval != "ALL":
        interval_hours = int(funding_interval[:-1])
        symbols = [
            group for group in symbols
            if int(float(group["rows"][0].get("funding_interval_hours") or 0)) == interval_hours
        ]
    if binance_spot_only:
        symbols = [group for group in symbols if any(row["long_exchange"] == "Binance" for row in group["rows"])]
    if symbol_query:
        full_pair_search = "/" in raw_symbol_query or symbol_query.endswith("USDT")
        symbols = [
            group for group in symbols
            if (group["symbol"].upper().replace("/", "") == symbol_query if full_pair_search else group["symbol"].upper().split("/", 1)[0] == symbol_query)
        ]
    def sort_value(group):
        if sort_by in {"open_spread", "close_spread"}:
            return max(row[sort_by] for row in group["rows"])
        return group["rows"][0][sort_by]

    sortable_symbols = [group for group in symbols if sort_value(group) is not None]
    missing_symbols = [group for group in symbols if sort_value(group) is None]
    symbols = sorted(sortable_symbols, key=sort_value, reverse=sort_direction == "desc") + missing_symbols
    total = len(symbols)
    pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, pages)
    start = (page - 1) * page_size
    page_symbols = symbols[start:start + page_size]
    payload = {**snapshot, "page": page, "pages": pages, "page_size": page_size, "total_symbols": total, "binance_spot_only": binance_spot_only, "funding_interval": funding_interval, "symbol_query": symbol_query, "sort_by": sort_by, "sort_direction": sort_direction, "symbols": page_symbols}
    return jsonify(payload)


@app.get("/api/dual-futures")
def dual_futures():
    page = max(request.args.get("page", 1, type=int), 1)
    page_size = 30
    raw_symbol_query = "".join(request.args.get("symbol", "").upper().split())
    symbol_query = raw_symbol_query.replace("/", "").replace("-", "")
    trend_sort_keys = {f"binance_{key}" for key in TREND_WINDOWS}
    allowed_sort_keys = {"open_spread", "close_spread", "funding_difference", "binance_basis", "bybit_basis", "okx_basis", *trend_sort_keys}
    sort_by = request.args.get("sort_by", "open_spread")
    sort_direction = request.args.get("sort_direction", "desc")
    if sort_by not in allowed_sort_keys:
        sort_by = "open_spread"
    if sort_direction not in {"asc", "desc"}:
        sort_direction = "desc"
    snapshot = load_latest_dual_futures_snapshot()
    if not snapshot:
        return jsonify({"error": "期多期空行情正在进行首轮同步，请稍后刷新。"}), 503
    global DUAL_VIEW_CACHE
    snapshot_key = snapshot["updated_at"]
    if DUAL_VIEW_CACHE["key"] != snapshot_key:
        enrich_dual_binance_reference(snapshot["symbols"])
        enrich_dual_basis_references(snapshot["symbols"])
        enrich_next_funding_net(snapshot["symbols"])
        enrich_dual_index_overlap(snapshot["symbols"])
        enrich_basis_openings(snapshot["symbols"], "futures_futures")
        DUAL_VIEW_CACHE = {"key": snapshot_key, "symbols": snapshot["symbols"]}
    mark_announced_delistings(DUAL_VIEW_CACHE["symbols"])
    symbols = [group for group in DUAL_VIEW_CACHE["symbols"] if not is_rwa_stock_pair(group["symbol"])]
    if symbol_query:
        full_pair_search = "/" in raw_symbol_query or symbol_query.endswith("USDT")
        symbols = [
            group for group in symbols
            if (group["symbol"].upper().replace("/", "") == symbol_query if full_pair_search else group["symbol"].upper().split("/", 1)[0].startswith(symbol_query))
        ]
    def sort_value(group):
        if sort_by in {"binance_basis", *trend_sort_keys}:
            return group.get(sort_by)
        values = [row.get(sort_by) for row in group["rows"] if row.get(sort_by) is not None]
        return max(values) if values else None

    sortable_symbols = [group for group in symbols if sort_value(group) is not None]
    missing_symbols = [group for group in symbols if sort_value(group) is None]
    symbols = sorted(sortable_symbols, key=sort_value, reverse=sort_direction == "desc") + missing_symbols
    total = len(symbols)
    pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, pages)
    start = (page - 1) * page_size
    return jsonify({**snapshot, "page": page, "pages": pages, "page_size": page_size, "total_symbols": total, "symbol_query": symbol_query, "sort_by": sort_by, "sort_direction": sort_direction, "symbols": symbols[start:start + page_size]})


@app.get("/api/symbol-suggestions")
def symbol_suggestions():
    query = "".join(request.args.get("q", "").strip().upper().split())
    if not query:
        return jsonify({"items": []})
    compact_query = query.replace("/", "").replace("-", "")
    live_pairs = sorted({item.symbol.upper() for item in LatestMarketSnapshot.query.with_entities(LatestMarketSnapshot.symbol).all() if not is_rwa_stock_pair(item.symbol)})
    pairs = set(live_pairs)
    for base in COIN_ALIASES:
        pairs.add(f"{base}/USDT")

    matches = []
    for pair in pairs:
        base = pair.split("/", 1)[0]
        chinese_name = COIN_ALIASES.get(base, "")
        searchable = f"{base}{pair.replace('/', '')}{chinese_name}".upper()
        if compact_query not in searchable:
            continue
        live = pair in live_pairs
        label = f"{chinese_name} · {pair}" if chinese_name else pair
        starts_with = base.startswith(compact_query) or chinese_name.startswith(query)
        matches.append({"symbol": pair, "label": label, "name": chinese_name, "live": live, "starts_with": starts_with})

    prefix_matches = [item for item in matches if item["starts_with"]]
    matches = prefix_matches or matches
    matches.sort(key=lambda item: (not item["live"], item["symbol"]))
    return jsonify({"items": [{key: value for key, value in item.items() if key != "starts_with"} for item in matches[:12]]})


@app.get("/api/data-integrity")
def data_integrity():
    snapshot = load_latest_market_snapshot()
    if not snapshot:
        return jsonify({"ready": False, "error": "尚未完成首轮行情同步。"}), 503
    integrity = price_history_integrity(snapshot["symbols"])
    return jsonify({"ready": True, "latest_snapshot": snapshot["updated_at"], "price_history": integrity})


@app.get("/api/refresh-diagnostics")
def refresh_diagnostics():
    return jsonify({"target_seconds": MARKET_REFRESH_SECONDS, **MARKET_REFRESH_METRICS})


def percent_delta(current, previous):
    return (current - previous) / previous * 100 if previous else None


def directional_consistency(values, direction):
    changes = [percent_delta(float(current), float(previous)) for previous, current in zip(values, values[1:])]
    valid = [change for change in changes if change is not None]
    return (sum(change > 0 for change in valid) if direction == "up" else sum(change < 0 for change in valid)) / len(valid) if valid else 0.0


def fetch_horn_metrics(symbol, timeframe):
    raw_symbol = symbol.replace("/", "")
    try:
        window = 50 if timeframe == "30m" else 25
        oi = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": timeframe, "limit": window}), timeout=6)
        ratios = get_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?" + urlencode({"symbol": raw_symbol, "period": timeframe, "limit": window}), timeout=6)
        klines = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": timeframe, "limit": window + 1}), timeout=6)
        closed = klines[:-1]
        if len(oi) < 2 or len(ratios) < 2 or len(closed) < 2:
            return None
        oi_value = float(oi[-1].get("sumOpenInterestValue", 0) or 0)
        ratio_value = float(ratios[-1].get("longShortRatio", 0) or 0)
        oi_change = percent_delta(oi_value, float(oi[0].get("sumOpenInterestValue", 0) or 0))
        ratio_change = percent_delta(ratio_value, float(ratios[0].get("longShortRatio", 0) or 0))
        price_change = percent_delta(float(closed[-1][4]), float(closed[0][4]))
        cvd_change = sum((2 * float(row[10]) - float(row[7])) for row in closed)
        if None in (oi_change, ratio_change, price_change):
            return None
        score = (min(price_change / 30, 1) * 15 + directional_consistency([row[4] for row in closed], "up") * 15 + min(oi_change / 30, 1) * 15 + directional_consistency([row.get("sumOpenInterestValue", 0) for row in oi], "up") * 15 + min(abs(ratio_change) / 25, 1) * 15 + directional_consistency([row.get("longShortRatio", 0) for row in ratios], "down") * 15 + (10 if cvd_change > 0 else 0))
        return {"symbol": symbol, "timeframe": timeframe, "price_change": price_change, "oi_change": oi_change, "oi_value": oi_value, "ratio_change": ratio_change, "ratio_value": ratio_value, "cvd_change": cvd_change, "cvd_confirmed": cvd_change > 0, "score": round(score, 1)}
    except Exception:
        return None


def scan_daily_horn_signals():
    """Run once each morning: price up, account ratio down, OI up; CVD is a confirmation label."""
    snapshot = load_latest_market_snapshot()
    if not snapshot:
        return 0
    enrich_price_changes(snapshot["symbols"])
    candidates = []
    for group in snapshot["symbols"]:
        if is_rwa_stock_pair(group["symbol"]):
            continue
        row = group["rows"][0]
        for timeframe, field in (("30m", "change_24h"), ("4h", "change_7d")):
            momentum = row.get(field)
            if momentum and momentum > 0:
                candidates.append((momentum, group["symbol"], timeframe))
    candidates = sorted(candidates, reverse=True)[:80]
    signals = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_horn_metrics, symbol, timeframe) for _, symbol, timeframe in candidates]
        for future in as_completed(futures):
            item = future.result()
            if item and item["price_change"] > 0 and item["oi_change"] > 0 and item["ratio_change"] < 0:
                signals.append(item)
    report_date = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    DailyHornSignal.query.filter_by(report_date=report_date).delete()
    selected = [item for timeframe in ("30m", "4h") for item in sorted((value for value in signals if value["timeframe"] == timeframe), key=lambda value: value["score"], reverse=True)[:20]]
    for item in selected:
        db.session.add(DailyHornSignal(report_date=report_date, **item))
    db.session.commit()
    return len(selected)


def daily_lark_trend_candidates(report_date):
    grouped = {}
    for item in DailyHornSignal.query.filter_by(report_date=report_date).order_by(DailyHornSignal.score.desc()).all():
        grouped.setdefault(item.symbol, []).append(item)
    candidates = []
    for symbol, items in grouped.items():
        primary = max(items, key=lambda item: item.score)
        resonance = primary.score + (12 if len(items) > 1 else 0)
        candidates.append((resonance, symbol, items))
    return sorted(candidates, reverse=True)[:3]


def trend_key_levels(symbol, timeframe):
    try:
        rows = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": symbol.replace("/", ""), "interval": timeframe, "limit": 31}), timeout=6)[:-1]
        if len(rows) < 12:
            return None
        support = min(float(row[3]) for row in rows[-12:])
        resistance = max(float(row[2]) for row in rows[-20:])
        return support, resistance
    except Exception:
        return None


def compact_trend_judgement(items, support, resistance):
    frames = {item.timeframe: item for item in items}
    primary = frames.get("4h") or frames.get("30m")
    resonance = len(items) > 1
    cvd_ok = all(item.cvd_confirmed for item in items)
    core = "30M 与 4H 同向共振，价格、OI 与人数比结构完整。" if resonance else f"{primary.timeframe.upper()} 结构成立，暂未形成双周期共振。"
    flow = "主动买入 CVD 同步确认，延续性偏强。" if cvd_ok else "CVD 尚未确认，需防止冲高后的量价背离。"
    levels = f"守住 {support:.6g} 偏多结构延续；接近 {resistance:.6g} 观察放量突破或承压。" if support and resistance else "关键位数据暂未同步，重点观察近期平台高低点。"
    return core + flow + levels


LARK_CARD_COLORS = {
    "cus-bull": {"light_mode": "rgba(20, 138, 82, 1)", "dark_mode": "rgba(74, 222, 128, 1)"},
    "cus-bull-soft": {"light_mode": "rgba(14, 116, 144, 1)", "dark_mode": "rgba(45, 212, 191, 1)"},
    "cus-watch": {"light_mode": "rgba(185, 109, 0, 1)", "dark_mode": "rgba(251, 191, 36, 1)"},
    "cus-bear": {"light_mode": "rgba(196, 51, 51, 1)", "dark_mode": "rgba(248, 113, 113, 1)"},
    "cus-muted": {"light_mode": "rgba(100, 116, 139, 1)", "dark_mode": "rgba(148, 163, 184, 1)"},
}


def lark_score_color(score):
    if score >= 90:
        return "cus-bull"
    if score >= 75:
        return "cus-bull-soft"
    if score >= 60:
        return "cus-watch"
    return "cus-bear"


def lark_signed_color(value):
    if value is None or value == 0:
        return "cus-muted"
    return "cus-bull" if value > 0 else "cus-bear"


def lark_dot_label(text, color):
    return f"<font color='{color}'>● {text}</font>"


def lark_cvd_label(value):
    if value is None:
        return lark_dot_label("暂无", "cus-muted")
    return lark_dot_label("上涨" if value > 0 else "下跌", lark_signed_color(value))


def lark_large_value(value):
    if value is None:
        return "暂无"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


def lark_ratio_value(value):
    return "暂无" if value is None else f"{value:.4f}"


def lark_plain_value(value, decimals=4, suffix=""):
    if value is None:
        return "暂无"
    return f"{value:+.{decimals}f}{suffix}"


def lark_price_value(value):
    return "暂无" if value is None else f"{value:.8f}"


def lark_compact_number(value):
    if value is None:
        return "暂无"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.0f}"


def lark_trend_card(markdowns):
    """单列灰底卡片：保留聊天中的紧凑感，同时允许段内文字使用自定义 RGBA。"""
    if isinstance(markdowns, str):
        markdowns = [markdowns]
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "style": {"color": LARK_CARD_COLORS}},
            "body": {
                "padding": "8px 8px 8px 8px",
                "elements": [{
                    "tag": "interactive_container",
                    "width": "fill",
                    "height": "auto",
                    "background_style": "grey",
                    "has_border": False,
                    "corner_radius": "8px",
                    "padding": "10px 12px 10px 12px",
                    "elements": [{"tag": "markdown", "content": markdown, "text_size": "normal"}],
                } for markdown in markdowns],
            },
        },
    }


def send_daily_lark_trend_report():
    webhook = os.getenv("LARK_DAILY_TREND_WEBHOOK", "").strip()
    report_date = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    if not webhook:
        return False
    candidates = daily_lark_trend_candidates(report_date)
    sections = []
    hydrated = False
    for index, (resonance, symbol, items) in enumerate(candidates, 1):
        for item in items:
            if item.oi_value is None or item.ratio_value is None:
                fresh = fetch_horn_metrics(symbol, item.timeframe)
                if fresh:
                    item.oi_value = fresh.get("oi_value")
                    item.ratio_value = fresh.get("ratio_value")
                    item.oi_change = fresh.get("oi_change", item.oi_change)
                    item.ratio_change = fresh.get("ratio_change", item.ratio_change)
                    item.price_change = fresh.get("price_change", item.price_change)
                    item.cvd_change = fresh.get("cvd_change", item.cvd_change)
                    item.cvd_confirmed = fresh.get("cvd_confirmed", item.cvd_confirmed)
                    item.score = fresh.get("score", item.score)
                    hydrated = True
        rows = {item.timeframe: item for item in items}
        primary_timeframe = "4h" if "4h" in rows else "30m"
        levels = trend_key_levels(symbol, primary_timeframe)
        support, resistance = levels if levels else (None, None)

        def metric_line(timeframe):
            item = rows.get(timeframe)
            if not item:
                return f"近{timeframe.upper()}：暂无完整结构"
            price_color = lark_signed_color(item.price_change)
            oi_color = lark_signed_color(item.oi_change)
            ratio_color = lark_signed_color(item.ratio_change)
            return (
                f"近{timeframe.upper()}：价格 <font color='{price_color}'>{item.price_change:+.2f}%</font>"
                f"｜持仓 <font color='{oi_color}'>{item.oi_change:+.2f}%</font>"
                f"｜多空人数比 <font color='{ratio_color}'>{item.ratio_change:+.2f}%</font>"
                f"｜CVD {lark_cvd_label(item.cvd_change)}"
            )

        levels_text = f"向下看 {support:.6g}｜向上看 {resistance:.6g}" if support and resistance else "关键位暂未同步"
        sections.append("\n".join([
            f"{lark_dot_label('⬆ 看涨 / ' + ('较强' if resonance >= 85 else '观察'), 'cus-bull')}",
            f"**{index}. {symbol}**　{lark_dot_label(f'结构分 {resonance:.1f}', lark_score_color(resonance))}",
            f"时间：{report_date} 08:00",
            metric_line("30m"),
            metric_line("4h"),
            f"判断：{compact_trend_judgement(items, support, resistance)}",
            f"关键位：{levels_text}",
            f"COINGLASS：[https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}](https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')})",
        ]))
    if not sections:
        sections.append(
            f"{lark_dot_label('趋势盯盘', 'cus-muted')}\n"
            f"<font color='cus-muted'>{report_date} · 今日无完整共振候选，继续观察，不强行给出方向。</font>"
        )
    if hydrated:
        db.session.commit()
    try:
        request_obj = Request(webhook, data=json.dumps(lark_trend_card(sections), ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "ArbiScope/1.0"})
        with urlopen(request_obj, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result.get("code", 0) == 0 or result.get("StatusCode", 0) == 0
    except Exception:
        return False


def lark_daily_trend_already_pushed(report_date):
    return LarkPushState.query.filter_by(channel="daily_trend", symbol="ALL", signal_key=report_date).first() is not None


def mark_lark_daily_trend_pushed(report_date):
    if not lark_daily_trend_already_pushed(report_date):
        db.session.add(LarkPushState(channel="daily_trend", symbol="ALL", signal_key=report_date))
        db.session.commit()


@app.get("/api/daily-report/trends")
def daily_report_trends():
    snapshot = load_latest_market_snapshot()
    if not snapshot:
        return jsonify({"updated_at": None, "rising": [], "falling": [], "horn_30m": [], "horn_4h": []})
    enrich_price_changes(snapshot["symbols"])
    rows = [
        {"symbol": group["symbol"], "change_24h": group["rows"][0].get("change_24h"), "change_7d": group["rows"][0].get("change_7d")}
        for group in snapshot["symbols"] if not is_rwa_stock_pair(group["symbol"])
    ]
    valid = [item for item in rows if item["change_24h"] is not None]
    report_date = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    horn_rows = DailyHornSignal.query.filter_by(report_date=report_date).order_by(DailyHornSignal.score.desc()).all()
    signal_payload = lambda item: {"symbol": item.symbol, "timeframe": item.timeframe, "price_change": item.price_change, "oi_change": item.oi_change, "oi_value": item.oi_value, "ratio_change": item.ratio_change, "ratio_value": item.ratio_value, "cvd_confirmed": item.cvd_confirmed, "score": item.score}
    return jsonify({"updated_at": snapshot["updated_at"], "rising": sorted(valid, key=lambda item: item["change_24h"], reverse=True)[:20], "falling": sorted(valid, key=lambda item: item["change_24h"])[:20], "horn_30m": [signal_payload(item) for item in horn_rows if item.timeframe == "30m"], "horn_4h": [signal_payload(item) for item in horn_rows if item.timeframe == "4h"], "automation_status": automation_statuses("daily_horn_scan", "daily_lark_trend_push")})


THOUGHT_WATCHLIST = {
    "AKE/USDT": {
        "entry": 0.00085,
        "entry_time": "2026-07-16 19:07",
        "fallback": {
            "support": 0.000763,
            "resistance": 0.0009878,
            "oi_value": 47099459.34,
            "oi_change_pct": 79.4,
            "ratio_value": 0.4,
            "ratio_change_pct": 0.55,
            "cvd": 34382209.74,
            "change_30m": 13.77,
            "change_4h": 65.97,
        },
    },
    "US/USDT": {
        "entry": None,
        "entry_time": "重点反转观察",
        "fallback": {},
    },
    "T/USDT": {
        "entry": 0.0045,
        "entry_time": "2026-07-17 11:00-13:00 区间",
        "side": "short",
        "fallback": {},
    },
}


def thought_snapshot(symbol):
    config = THOUGHT_WATCHLIST[symbol]
    raw_symbol = symbol.replace("/", "")
    entry = config.get("entry")
    entry_time = config.get("entry_time") or "重点观察"
    fallback_overrides = config.get("fallback") or {}
    now = datetime.now(SHANGHAI_TZ)
    fallback = {
        "symbol": symbol,
        "entry": entry,
        "entry_time": entry_time,
        "last": None,
        "profit_pct": None,
        "support": None,
        "resistance": None,
        "oi_value": None,
        "oi_change_pct": None,
        "ratio_value": None,
        "ratio_change_pct": None,
        "cvd": None,
        "change_30m": None,
        "change_4h": None,
        "funding_rate": None,
        "basis": None,
        "validation": {},
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "fallback",
    }
    fallback.update(fallback_overrides)
    try:
        ticker = get_json("https://fapi.binance.com/fapi/v1/ticker/24hr?" + urlencode({"symbol": raw_symbol}), timeout=8)
        k30 = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "30m", "limit": 60}), timeout=8)
        k4h = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "4h", "limit": 30}), timeout=8)
        premium = get_json("https://fapi.binance.com/fapi/v1/premiumIndex?" + urlencode({"symbol": raw_symbol}), timeout=8)
        oi = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": 50}), timeout=8)
        ratios = get_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": 50}), timeout=8)
        last = float(ticker.get("lastPrice", 0) or 0)
        support = min(float(row[3]) for row in k30[-12:])
        resistance = max(float(row[2]) for row in k30[-20:])
        oi_first = float(oi[0].get("sumOpenInterestValue", 0) or 0)
        oi_last = float(oi[-1].get("sumOpenInterestValue", 0) or 0)
        ratio_first = float(ratios[0].get("longShortRatio", 0) or 0)
        ratio_last = float(ratios[-1].get("longShortRatio", 0) or 0)
        closed30 = k30[:-1] if len(k30) > 2 else k30
        closed4h = k4h[:-1] if len(k4h) > 2 else k4h
        cvd = sum((2 * float(row[10]) - float(row[7])) for row in closed30)
        def window_metrics(candle_count):
            window_rows = closed30[-candle_count:]
            oi_window = oi[-candle_count:]
            ratio_window = ratios[-candle_count:]
            if len(window_rows) < candle_count or len(oi_window) < candle_count or len(ratio_window) < candle_count:
                return {"price_change": None, "oi_change": None, "ratio_change": None, "cvd": None, "volume": None, "volume_ratio": None}
            price_change = percent_delta(float(window_rows[-1][4]), float(window_rows[0][1]))
            oi_change = percent_delta(float(oi_window[-1].get("sumOpenInterestValue", 0) or 0), float(oi_window[0].get("sumOpenInterestValue", 0) or 0))
            ratio_change = percent_delta(float(ratio_window[-1].get("longShortRatio", 0) or 0), float(ratio_window[0].get("longShortRatio", 0) or 0))
            cvd_value = sum((2 * float(row[10]) - float(row[7])) for row in window_rows)
            volume = sum(float(row[7]) for row in window_rows)
            prior_rows = closed30[-(candle_count + 10):-candle_count] if len(closed30) >= candle_count + 10 else []
            prior_average = (sum(float(row[7]) for row in prior_rows) / len(prior_rows) * candle_count) if prior_rows else None
            volume_ratio = volume / prior_average if prior_average else None
            return {"price_change": price_change, "oi_change": oi_change, "ratio_change": ratio_change, "cvd": cvd_value, "volume": volume, "volume_ratio": volume_ratio}
        index_price = float(premium.get("indexPrice", 0) or 0)
        mark_price = float(premium.get("markPrice", 0) or 0)
        return {
            **fallback,
            "last": last,
            "profit_pct": percent_delta(last, entry) if entry else None,
            "support": support,
            "resistance": resistance,
            "oi_value": oi_last,
            "oi_change_pct": percent_delta(oi_last, oi_first),
            "ratio_value": ratio_last,
            "ratio_change_pct": percent_delta(ratio_last, ratio_first),
            "cvd": cvd,
            "change_30m": percent_delta(float(closed30[-1][4]), float(closed30[-13][4])),
            "change_4h": percent_delta(float(closed4h[-1][4]), float(closed4h[-8][4])),
            "funding_rate": float(premium.get("lastFundingRate", 0) or 0) * 100,
            "basis": percent_delta(mark_price, index_price) if index_price else None,
            "validation": {"30m": window_metrics(1), "1h": window_metrics(2), "2h": window_metrics(4)},
            "source": "live",
        }
    except Exception:
        return thought_snapshot_from_db(symbol, fallback)


def thought_snapshot_from_db(symbol, fallback):
    context = thought_market_context(symbol)
    if not context:
        return fallback
    entry = fallback.get("entry")
    futures_mid = context.get("last")
    return {
        **fallback,
        **context,
        "profit_pct": percent_delta(futures_mid, entry) if futures_mid and entry else None,
        "support": None,
        "resistance": None,
        "oi_change_pct": None,
        "ratio_value": None,
        "ratio_change_pct": None,
        "cvd": None,
        "change_30m": None,
        "change_4h": None,
        "validation": {},
        "source": "db_fallback",
    }


def thought_market_context(symbol):
    market_rows = LatestMarketSnapshot.query.filter_by(symbol=symbol).order_by(LatestMarketSnapshot.captured_at.desc()).all()
    if not market_rows:
        return None
    preferred = next((row for row in market_rows if row.long_exchange == "Binance"), None) or market_rows[0]
    futures_mid = ((preferred.short_bid or 0) + (preferred.short_ask or 0)) / 2 if preferred.short_bid and preferred.short_ask else None
    spot_volume = max([row.spot_volume or 0 for row in market_rows] or [0])
    futures_volume = preferred.futures_volume
    volume_ratio = (futures_volume / spot_volume) if futures_volume and spot_volume else None
    return {
        "last": futures_mid,
        "oi_value": preferred.futures_open_interest,
        "funding_rate": preferred.funding_rate,
        "basis": preferred.basis,
        "open_spread": preferred.open_spread,
        "close_spread": preferred.close_spread,
        "spot_volume": spot_volume,
        "futures_volume": futures_volume,
        "futures_spot_volume_ratio": volume_ratio,
        "updated_at": preferred.captured_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def ake_thought_snapshot():
    return thought_snapshot("AKE/USDT")


def thought_watch_snapshots():
    return [thought_snapshot(symbol) for symbol in THOUGHT_WATCHLIST]


def thought_push_direction(analysis):
    validation = analysis.get("validation") or {}
    checks = [validation.get(key) or {} for key in ("30m", "1h", "2h")]
    valid = [item for item in checks if item.get("price_change") is not None and item.get("oi_change") is not None and item.get("ratio_change") is not None and item.get("cvd") is not None]
    if len(valid) < 3:
        return thought_db_fallback_direction(analysis)
    volume_spike = any((item.get("volume_ratio") or 0) >= 2.5 for item in valid)
    funding_negative = analysis.get("funding_rate") is not None and analysis.get("funding_rate") < 0
    basis_opened = analysis.get("basis") is not None and abs(analysis.get("basis")) >= 1.0
    contract_premium_alive = (
        analysis.get("funding_rate") is not None and analysis.get("funding_rate") > 0
        and analysis.get("basis") is not None and analysis.get("basis") > 0
    )
    if volume_spike and funding_negative and basis_opened:
        return "distribution"
    bullish_count = sum(item["price_change"] >= 0.8 and item["oi_change"] >= 1.0 and item["ratio_change"] <= -0.3 and item["cvd"] > 0 for item in valid)
    bearish_count = sum(item["price_change"] <= -0.8 and item["oi_change"] >= 1.0 and item["ratio_change"] >= 0.3 and item["cvd"] < 0 for item in valid)
    if bullish_count == 3:
        return "bullish"
    if bearish_count == 3 and not contract_premium_alive:
        return "bearish"
    reversal_count = sum(item["price_change"] <= -0.8 and item["cvd"] < 0 for item in valid)
    pressure = (
        any(item["oi_change"] <= -1.0 for item in valid)
        or (analysis.get("funding_rate") is not None and analysis.get("funding_rate") < 0)
        or (analysis.get("basis") is not None and abs(analysis.get("basis")) >= 1.0)
    )
    if reversal_count >= 2 and pressure and not contract_premium_alive:
        return "reversal"
    return thought_db_fallback_direction(analysis)


def thought_db_fallback_direction(analysis):
    context = thought_market_context(analysis.get("symbol"))
    if context:
        analysis = {**analysis, **{key: value for key, value in context.items() if value is not None}}
    symbol = analysis.get("symbol")
    funding = analysis.get("funding_rate")
    basis = analysis.get("basis")
    oi_value = analysis.get("oi_value") or 0
    volume_ratio = analysis.get("futures_spot_volume_ratio") or 0
    open_spread = analysis.get("open_spread")
    if symbol == "AKE/USDT" and funding is not None and basis is not None:
        if funding > 0 and basis > 0 and oi_value >= 10_000_000 and volume_ratio >= 20:
            return "bullish_db_watch"
    if symbol == "T/USDT" and funding is not None and basis is not None:
        if funding < 0 and basis <= -1.0 and (open_spread is None or open_spread <= -1.0):
            return "bearish_db_watch"
    return None


def thought_signal_key(analysis, direction):
    last = analysis.get("last") or 0
    resistance = analysis.get("resistance") or 0
    support = analysis.get("support") or 0
    if direction == "bullish" and resistance and last >= resistance * 0.995:
        return "bullish-near-breakout"
    if direction in {"bearish", "reversal", "distribution"} and support and last <= support * 1.005:
        return "bearish-near-breakdown"
    return f"{direction}-resonance"


def thought_lark_message(analysis, direction):
    if analysis.get("source") == "db_fallback" or direction in {"bullish_db_watch", "bearish_db_watch"}:
        return thought_lark_db_fallback_message(analysis, direction)
    validation = analysis.get("validation") or {}
    def row(label, key):
        item = validation.get(key) or {}
        return (
            f"近{label}：价格 {lark_plain_value(item.get('price_change'), 2, '%')}，"
            f"持仓 {lark_plain_value(item.get('oi_change'), 2, '%')}，"
            f"多空人数比 {lark_plain_value(item.get('ratio_change'), 2, '%')}，"
            f"CVD {lark_compact_number(item.get('cvd'))}，"
            f"成交额 {lark_compact_number(item.get('volume'))}，"
            f"放量倍数 {lark_plain_value(item.get('volume_ratio'), 2, 'x')}"
        )
    direction_text = "看涨/转强" if direction == "bullish" else ("看跌/做空观察" if direction == "bearish" else ("出货三件套预警" if direction == "distribution" else "涨势反转预警"))
    direction_color = "cus-bull" if direction == "bullish" else "cus-bear"
    direction_icon = "● ⬆" if direction == "bullish" else "● ⬇"
    title = f"{analysis['symbol'].split('/')[0]}思路盯盘：{'向上突破确认' if direction == 'bullish' else ('做空机会确认' if direction == 'bearish' else ('出货三件套确认' if direction == 'distribution' else '高位反转预警'))}"
    support = analysis.get("support")
    resistance = analysis.get("resistance")
    judgement = (
        f"价格重新靠近或站上 {lark_price_value(resistance)}，近 30M/1H/2H 出现价格转强、持仓增加、多空人数比下降、CVD 上涨的犄型共振，说明主力仍在向上推而不是立即出货。"
        if direction == "bullish"
        else (
            f"价格跌向或跌破 {lark_price_value(support)}，近 30M/1H/2H 出现价格走弱、持仓增加、多空人数比回升、CVD 转负，说明空头主动性增强；若反抽无法收回支撑，可按做空机会观察。"
            if direction == "bearish"
            else (
                f"出现大量放量、BN 资金费转负、BN 基差打开三件套。我的判断：这更接近主力出货/强制换手预警，不一定立刻追空，但必须停止按普通洗盘理解；后续重点看放量后是否跌破支撑、CVD 是否继续转负、反抽是否无力。"
                if direction == "distribution"
                else f"高涨幅后近端结构开始转弱，CVD 转负并伴随持仓/资金费/基差中的至少一项恶化；这不等于立刻做空，但需要按涨势反转预警处理。"
            )
        )
    )
    return "\n".join([
        f"方向：<font color='{direction_color}'>{direction_icon} {direction_text}</font>",
        title,
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}，BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}，BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        row("30m", "30m"),
        row("1H", "1h"),
        row("2H", "2h"),
        f"判断：{judgement}",
        f"关键位：向下看 {lark_price_value(support)}（理由：近 12 根 30M K 线低点形成的近端支撑，跌破说明短线结构转弱）；向上看 {lark_price_value(resistance)}（理由：近 20 根 30M K 线高点形成的压力位，突破后才算向上确认）。",
        f"K线：https://www.coinglass.com/tv/zh/Binance_{analysis['symbol'].replace('/', '')}",
    ])


def thought_lark_db_fallback_message(analysis, direction):
    context = thought_market_context(analysis.get("symbol"))
    if context:
        analysis = {**analysis, **{key: value for key, value in context.items() if value is not None}}
    symbol = analysis["symbol"]
    bullish = direction == "bullish_db_watch"
    direction_text = "看涨 / 多头结构仍在" if bullish else "看跌 / 空头结构仍在"
    direction_color = "cus-bull" if bullish else "cus-bear"
    direction_icon = "● 上" if bullish else "● 下"
    if bullish:
        judgement = (
            "BN 资费仍为正、BN 基差仍为正，且 Binance 合约成交量明显大于现货成交量，说明合约端多头结构还没有失效。"
            "这类信号不是完整犄型共振，因为当前缺少 CVD 与多空人数比确认；但它足够提醒我们继续盯是否重新放量上推。"
        )
        key_levels = "向下看最近回调低点是否被放量跌破；向上看前高附近是否重新放量突破。缺少实时 K 线关键位时，以 CoinGlass 图表为准。"
    else:
        judgement = (
            "BN 资费为负、BN 基差明显负向打开，现多期空与期多期空结构都偏向空头压力。"
            "这和 T 的做空思路一致：若反弹无法修复负基差和负资费，更像诱多后的下行延续。"
            "当前缺少 CVD 与多空人数比确认，所以只作为结构提醒，不当成完整共振。"
        )
        key_levels = "向上看 0.0045-0.0047 一带能否重新站稳；向下看前低与放量跌破后的延续性。"
    return "\n".join([
        f"方向：<font color='{direction_color}'>{direction_icon} {direction_text}</font>",
        f"{symbol.split('/')[0]}思路盯盘：结构观察提醒",
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}，BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}，BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        f"持仓：{lark_compact_number(analysis.get('oi_value'))}，合约成交额：{lark_compact_number(analysis.get('futures_volume'))}，现货成交额：{lark_compact_number(analysis.get('spot_volume'))}",
        f"合约/现货量比：{lark_plain_value(analysis.get('futures_spot_volume_ratio'), 2, 'x')}，开差：{lark_plain_value(analysis.get('open_spread'), 4, '%')}，平差：{lark_plain_value(analysis.get('close_spread'), 4, '%')}",
        f"判断：{judgement}",
        f"关键位：{key_levels}",
        "备注：这是 MySQL 快照降级提醒，缺少 CVD 与多空人数比确认；等完整指标恢复后，仍以完整共振规则为准。",
        f"K线：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def send_thought_analysis_push():
    webhook = os.getenv("LARK_THOUGHT_ANALYSIS_WEBHOOK", "").strip()
    if not webhook:
        return False
    sections = []
    push_records = []
    for analysis in thought_watch_snapshots():
        if analysis.get("source") not in {"live", "db_fallback"}:
            continue
        direction = thought_push_direction(analysis)
        if not direction:
            continue
        signal_key = thought_signal_key(analysis, direction)
        existing = LarkPushState.query.filter_by(channel="thought_analysis", symbol=analysis["symbol"], signal_key=signal_key).first()
        if existing and (datetime.now() - existing.pushed_at).total_seconds() < 6 * 3600:
            continue
        sections.append(thought_lark_message(analysis, direction))
        push_records.append((existing, analysis, signal_key))
    if not sections:
        return False
    payload = lark_trend_card(sections)
    try:
        request_obj = Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "ArbiScope/1.0"})
        with urlopen(request_obj, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not (result.get("code", 0) == 0 or result.get("StatusCode", 0) == 0):
            return False
        for existing, analysis, signal_key in push_records:
            if existing:
                existing.pushed_at = datetime.now()
            else:
                db.session.add(LarkPushState(channel="thought_analysis", symbol=analysis["symbol"], signal_key=signal_key))
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


@app.post("/api/daily-report/thoughts/test-push")
def test_thought_analysis_push():
    webhook = os.getenv("LARK_THOUGHT_ANALYSIS_WEBHOOK", "").strip()
    if not webhook:
        return jsonify({"ok": False, "error": "LARK_THOUGHT_ANALYSIS_WEBHOOK 未配置"}), 400
    analysis = ake_thought_snapshot()
    if analysis.get("source") != "live":
        return jsonify({"ok": False, "error": "AKE 实时数据暂时不可用"}), 503
    direction = thought_push_direction(analysis) or "bullish"
    payload = lark_trend_card([thought_lark_message(analysis, direction)])
    try:
        request_obj = Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "ArbiScope/1.0"})
        with urlopen(request_obj, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        ok = result.get("code", 0) == 0 or result.get("StatusCode", 0) == 0
        return jsonify({"ok": ok, "direction": direction, "result": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


def thought_us_item(us):
    return {
        "symbol": us["symbol"],
        "trade_side": "观察",
        "trade_status": "新高拉盘 / 反转盯盘",
        "entry": us["entry"],
        "entry_time": us["entry_time"],
        "exit": None,
        "exit_time": None,
        "last": us["last"],
        "profit_pct": us["profit_pct"],
        "realized_profit_pct": None,
        "support": us["support"],
        "resistance": us["resistance"],
        "oi_value": us["oi_value"],
        "oi_change_pct": us["oi_change_pct"],
        "ratio_value": us["ratio_value"],
        "ratio_change_pct": us["ratio_change_pct"],
        "cvd": us["cvd"],
        "change_30m": us["change_30m"],
        "change_4h": us["change_4h"],
        "funding_rate": us["funding_rate"],
        "basis": us["basis"],
        "validation": us.get("validation") or {},
        "source": us["source"],
        "screenshot_url": None,
        "thought_summary": "US 先按“新高无明显上方套牢盘 + 筹码可能集中 + 后续必有派发/砸盘窗口”盯盘。现在不能因为涨多就提前做空；真正的提醒点是放量失败、资金费转负、基差打开、OI/CVD/多空人数比出现出货共振。",
        "user_mistakes": ["当前暂无已验证交易，先不统计你的判断偏差；后续如果提前做空或过早否定主升，会单独复盘。"],
        "assistant_mistakes": ["需要避免因高涨幅本身过早给出反转结论；新高币上方阻力弱，主力可以继续左手倒右手推高。"],
        "thesis_win_rate": {"wins": 0, "losses": 0, "pending": 1, "rate": 0.0, "note": "US 为新增重点观察，等待第一次反转/延续验证。"},
        "my_thesis": "你的主线思路：US 是 2025 年 12 月前后上线的新币，不算老币；上市后长期下跌，早期手里有货的散户大概率已经抛掉。最近约一个半月涨幅很大并不断破新高，上方几乎没有历史套牢阻力，筹码可能更集中在主力手里。只要主力愿意，可以通过左手倒右手继续拉盘；但涨幅越大，后面某个时刻一定会有派发和砸盘窗口。",
        "assistant_thesis": "我的验证思路：官方资料显示 US 总量 100 亿，TGE 流通约 22.2%，投资人与核心贡献者 TGE 全锁并有 1 年 cliff；这说明 2026 年 12 月附近会有更大的解锁压力，但当前阶段更像“流通盘被交易结构控制”的盘口博弈。短线不适合只因新高去空，重点盯：高位放量不涨、CVD 转弱、OI 异常扩张后滞涨、资金费由正转负、基差/开差突然拉开、以及解锁/质押释放窗口。",
        "challenge_points": [
            "需要警惕：涨幅过大不等于马上能空，强庄币可能继续逼空或高位横盘很久。",
            "反转确认：必须等价格跌破近端支撑后反抽失败，或出现放量冲高回落、CVD 背离、OI 异常变化等组合证据。",
            "官方面约束：US 的团队/投资人 cliff 主要在 TGE 后 1 年，当前月度释放更偏社区/基金会；它不是马上到巨额 VC cliff 的结构，但后续 2026-12 附近要单独拉高风险等级。"
        ],
        "validation_view": "US 当前进入“继续拉升 vs 高位派发”盯盘：只要价格继续新高、回调缩量、OI 不塌、CVD 不持续转负，就不能急着判空；若出现大量放量 + 资金费转负 + 基差打开，或放量冲高失败后 CVD/OI 同步走坏，直接按出货三件套预警推送，并附带分析。",
        "take_profit": [
            "当前无持仓记录，不做止盈计划；这里只记录反转观察。",
            "如果后续出现做空机会，优先等跌破支撑后的反抽失败，而不是第一根阴线追空。",
        ],
        "stop_loss": [
            "若尝试做空，价格重新站回跌破位并且 CVD 转正，应按反转失败处理。",
            "如果 OI 继续扩张但价格重新突破压力位，说明仍可能逼空，不应硬扛空单。",
        ],
        "review_notes": [
            "官方资料记录：US 是 Talus Network 的 Sui 原生资产，总量 100 亿；TGE 流通约 22.2%，团队和投资人 TGE 全锁，1 年 cliff 后线性释放。",
            "盘口推演记录：上市后长期下跌会清洗早期散户，新高后上方套牢盘弱，主力继续拉升的阻力可能很小；但涨幅越大，后面派发/砸盘越需要重点盯。",
            "反转触发框架：价格走弱 + CVD 转负 + OI/资金费/基差价差至少一项恶化；若大量放量、资金费转负、基差打开同时出现，提升为出货三件套预警。",
            "执行约束：只在多项证据共振时推送，不因单次回调或插针提醒。",
        ],
    }


def thought_t_item(t):
    short_profit = percent_delta(t["entry"], t["last"]) if t.get("last") and t.get("entry") else None
    return {
        "symbol": t["symbol"],
        "trade_side": "做空",
        "trade_status": "持仓中",
        "entry": t["entry"],
        "entry_time": t["entry_time"],
        "exit": None,
        "exit_time": None,
        "last": t["last"],
        "profit_pct": short_profit,
        "realized_profit_pct": None,
        "support": t["support"],
        "resistance": t["resistance"],
        "oi_value": t["oi_value"],
        "oi_change_pct": t["oi_change_pct"],
        "ratio_value": t["ratio_value"],
        "ratio_change_pct": t["ratio_change_pct"],
        "cvd": t["cvd"],
        "change_30m": t["change_30m"],
        "change_4h": t["change_4h"],
        "funding_rate": t["funding_rate"],
        "basis": t["basis"],
        "validation": t.get("validation") or {},
        "source": t["source"],
        "screenshot_url": "/static/thoughts/t_coinglass_20260717.png",
        "thought_summary": "T 是做空持仓思路：前期犄型主升后出现负资费、负基差和结算周期缩短；这波反弹即使 CVD 上涨，只要多空人数比跟着持仓一起上涨，也更像反弹诱多里的空单布局。",
        "user_mistakes": ["风险点：负资费会增加做空持仓成本，若价格横盘不跌，不能只靠负资费继续硬扛。"],
        "assistant_mistakes": ["需要持续验证 0.0045 是否真正反抽失败，不能只因为前期出货迹象就忽略二次吸筹可能。"],
        "thesis_win_rate": {"wins": 0, "losses": 0, "pending": 1, "rate": 0.0, "note": "T 为新增做空思路，等待后续验证。"},
        "my_thesis": "你的主线思路：T 在 7 月 11 日到 7 月 12 日出现持仓涨、多空人数比跌、CVD 涨的典型犄型走势，币价从约 0.003 拉到约 0.006，接近翻倍。拉升过程中放量拉基差，资费跟随基差走，在结算时顶满，且结算周期从 4H 变成 1H。你认为这是主力出货换手信号。后续价格缩量下跌，中间几次小反弹依然维持 1H 结算，多头每小时可以收资费，更像诱多。当前这波反弹虽然 CVD 在涨，但多空人数比也跟着持仓一起涨，你认为这不是健康主升，而是更多账户追多、主力借反弹布置空单；所以 0.0045 附近做空胜率更大。",
        "assistant_thesis": "我的验证思路：你的空单逻辑是连贯的，核心不是单纯看跌，而是看到前期主升后的出货换手特征：高位放量、基差打开、资费极端化、结算周期缩短、随后缩量下跌。对于 T，CVD 上涨不能机械解释为看多；如果 CVD 涨的同时 OI 上升、多空人数比也上升，说明反弹中有更多账户站到多头一侧，可能给主力空单提供对手盘。若价格不能有效站回 0.0045-0.0047，且负资费、负基差持续，反弹更偏诱多。风险点是：若价格放量站稳 0.0047 上方，基差修复、负资费缓和，并且后续下跌无法延续，空单逻辑才需要降级。",
        "challenge_points": [
            "需要警惕：负资金费会让做空方付费，如果价格长时间横盘不跌，持仓成本会变高。",
            "反证条件：价格放量重新站稳 0.0045 上方，CVD 转正，基差从 -1% 以下快速修复，说明这次可能不是诱多而是重新吸筹。",
            "执行重点：不要只因为资费负就加空，必须看价格是否反抽失败、量能是否衰减、持仓是否配合。"
        ],
        "validation_view": "T 当前按做空持仓盯盘：入场约 0.0045。继续看 0.0045-0.0047 是否反抽失败；CVD 上涨不单独否定空头逻辑，关键看它是否伴随 OI 与多空人数比同步上涨。若三者同步上涨但价格无法站稳，且负资费、负基差持续，更像诱多中的空单布局；若放量站稳 0.0047 上方并修复基差，空单逻辑降级。",
        "take_profit": [
            "第一观察：若价格从 0.0045 下方继续走弱，先看前低附近是否放量承接。",
            "若跌破前低且 CVD 继续转负，可保留部分空单看下跌延续。",
        ],
        "stop_loss": [
            "若价格放量站稳 0.0045 上方，且 CVD 转正、基差快速修复，应视为空单逻辑减弱。",
            "若负资费维持但价格不跌反涨，说明空头拥挤，不能只靠资费继续硬扛。",
        ],
        "review_notes": [
            "新增做空思路：T 0.0045 附近建立空单。",
            "核心依据：前期犄型主升后出现基差、资费、结算周期异常，疑似出货换手；后续缩量下跌与多次小反弹更像诱多。",
            "后续验证：重点跟踪价格是否反抽失败；若 CVD 上涨但 OI 与多空人数比也同步上涨，需要优先按诱多/主力布空假设观察，而不是机械看多。",
        ],
    }


@app.get("/api/daily-report/thoughts")
def daily_report_thoughts():
    ake = ake_thought_snapshot()
    us = thought_snapshot("US/USDT")
    t = thought_snapshot("T/USDT")
    return jsonify({
        "updated_at": ake["updated_at"],
        "items": [{
            "symbol": ake["symbol"],
            "trade_side": "做多",
            "trade_status": "已平仓",
            "entry": ake["entry"],
            "entry_time": ake["entry_time"],
            "exit": 0.00092,
            "exit_time": "2026-07-17",
            "last": ake["last"],
            "profit_pct": percent_delta(0.00092, ake["entry"]),
            "realized_profit_pct": percent_delta(0.00092, ake["entry"]),
            "support": ake["support"],
            "resistance": ake["resistance"],
            "oi_value": ake["oi_value"],
            "oi_change_pct": ake["oi_change_pct"],
            "ratio_value": ake["ratio_value"],
            "ratio_change_pct": ake["ratio_change_pct"],
            "cvd": ake["cvd"],
            "change_30m": ake["change_30m"],
            "change_4h": ake["change_4h"],
            "funding_rate": ake["funding_rate"],
            "basis": ake["basis"],
            "validation": ake.get("validation") or {},
            "source": ake["source"],
            "screenshot_url": "/static/thoughts/ake_coinglass_20260716.png",
            "thought_summary": "AKE 的核心结论：0.00085 做多思路已验证，0.00092 止盈偏早。小市值山寨一天数倍主升时，普通压力/支撑参考价值下降，核心应看放量上涨、缩量回调、现货量是否跟不上合约量、OI/CVD 是否支持主力继续在合约端兑现。",
            "user_mistakes": [
                "0.00092 全部止盈偏早：7 月 17 日 0 点到 1 点的回调更可能只是主升途中的缩量洗盘/诱导平多，不应只因触及短线保护区就全退。",
                "正资费不能单独解释成主力一定在诱空，只能作为合约多头拥挤和结构偏强的证据之一。",
            ],
            "assistant_mistakes": [
                "之前对 AKE 的看空推送过早，忽略了正资费 + 正基差代表合约溢价仍在。",
                "0.000918-0.0009435 的保护止损区设计太机械，把短线回调当成趋势失效，没有结合小市值山寨数倍主升时压力位弱化、放量上涨/缩量下跌和合约兑现路径。",
                "以后 AKE/US 这类盯盘币，只要正资费和正基差同时存在，且回调缩量、OI 未塌、CVD 未持续转负，就阻止普通看空/反转推送。",
            ],
            "thesis_win_rate": {"wins": 2, "losses": 0, "pending": 1, "rate": 100.0, "note": "AKE 已按用户思路完成一次盈利止盈；样本仍少，只作为当前复盘统计。"},
            "my_thesis": "你的主线思路：犄型走势必须是持仓上涨，同时多空人数比呈对称下跌，符合主力做多、散户做空；如果 CVD 也上涨，更有力说明主动买入资金在推进。AKE 从底部约 0.00018 拉到 0.0009 以上，已经接近 5 倍，但过程中没有明显放大量、持仓也没掉，所以 0.00092 左右的小回调更可能是诱导散户平多，而不是主力已经完成出货。真正出货更可能是狂暴放量、持仓异常变化、基差/价差拉开，并且资金费被打成负数。",
            "assistant_thesis": "我的验证思路：这次 0.00092 止盈是盈利交易，但从复盘角度看可能偏保守。后续不追认旧仓，只找新机会；如果回调不破关键支撑、持仓不塌、CVD 不持续转负，随后重新放量上破，说明诱导平多后再拉的概率提高。真正出货预警需要多条件确认：巨量冲高或砸盘、OI 快速回落或异常扩张后价格滞涨、CVD 背离、基差/价差明显拉开、资金费转负或快速恶化。",
            "challenge_points": [
                "不完全认可：把正资金费率直接理解为主力希望散户做空，这个推断证据不足。正资金费只能说明多头付费，是否为主力诱空还需要头部账户、成交量和后续价格确认。",
                "需要警惕：如果价格继续上冲但 CVD 走平或下滑，说明主动买入不足，原来的多头延续逻辑会减弱。",
                "需要修正：多空人数比如果开始回升，说明散户空头拥挤度下降，不能继续按“散户持续做空给主力接多单”这一条单独判断。",
                "新的风控边界：资金费转负、基差拉开、价差拉开很适合做出货预警，但不能单独作为唯一证据；高位横盘派发也可能在资金费还没明显转负时发生。"
            ],
            "validation_view": "已止盈后进入二次机会盯盘：多头机会看犄型再共振，即价格转强、持仓增加、多空人数比下降、CVD 上涨；空头机会看结构失效，即价格走弱、持仓增加、多空人数比回升、CVD 转负。若大量放量、资金费转负、基差打开同时出现，直接按出货三件套预警推送，并附带分析。",
            "take_profit": [
                "已执行：0.00092 左右止盈，约相对 0.00085 入场获得 8% 左右收益，本次交易按盈利完成记录。",
                "复盘修正：这次 0.00092 止盈可能偏早。若后续仍无放量出货、持仓不掉、基差/价差未异常拉开、资金费未恶化，可以考虑保留底仓或等待二次确认，而不是小回调直接全平。",
                f"{max(ake['resistance'] or 0, ake['entry'] * 1.16):.8f} 附近：第一止盈区，约等于你的入场价上方 16% 且接近近期 30M 压力位，适合先减一部分锁住利润。",
                f"{ake['entry'] * 1.29:.8f}-{ake['entry'] * 1.35:.8f}：第二止盈区，只有突破第一压力后，成交量、持仓、CVD 继续同步上行才看这里。",
                f"{ake['entry'] * 1.47:.8f} 附近：小仓位博延续区，只适合在回踩不跌回第一压力位且 OI 不快速回落时保留。",
            ],
            "stop_loss": [
                f"{ake['entry'] * 1.08:.8f}-{ake['entry'] * 1.11:.8f}：以后不再定义为强止损，只作为浮盈保护/减仓参考；若回调缩量、OI 不塌、CVD 不持续转负，不能因此全平。",
                f"{ake['entry'] * 0.99:.8f}-{ake['entry']:.8f}：只在放量下跌、CVD 转负、OI 快速掉落或异常扩张后滞涨时，才升级为结构风险区。",
                f"{ake['support']:.8f} 附近：趋势失效必须结合量能和合约端证据，不能只看价格跌破；若跌破伴随放量砸盘、资费转负、基差/价差异常打开，才按主力出货处理。",
            ],
            "review_notes": [
                "已验证：用户在 0.00085 做多、0.00092 左右止盈，方向判断正确，犄型走势这次确实给出了有效的偏多线索。",
                "止盈检讨：从底部约 0.00018 到 0.0009 以上已经约 5 倍，但没有明显放大量、持仓没掉，说明主力未必已经进入出货段；0.00092 附近的小回调可能更像诱导平多，完全止盈可能错过主升延续。",
                "正确点：价格、CVD、持仓共振上行，确实支持原先的犄型延续假设。",
                "出货预警框架：真正要防主力出货，应重点盯狂暴放量、持仓掉落或异常扩张后滞涨、CVD 背离、基差/价差拉开、资金费快速转负；这些条件越多共振，越接近出货确认。",
                "强提醒规则：AKE 若同时出现大量放量、资金费转负、基差打开，必须提醒，并按主力出货/强制换手预警给出分析。",
                "新增观察：用户认为大涨前的回调可能是诱导别人平多。后续要验证回调是否只洗出短线多头，而不是主力派发；判断重点是回调时 OI 是否稳定、CVD 是否快速转负、关键支撑是否被有效跌破。",
                "后续任务：继续盯 AKE 的新多/新空机会。多头按犄型再共振推送；空头按跌破支撑后的新空共振推送，不因单根 K 线波动提醒。",
                "需要修正：不能只盯多空人数比下跌；当前窗口首尾已经小幅回升，说明散户空头进一步拥挤的条件变弱。",
                "后续验证：如果价格创新高但 CVD 不再创新高，或者 OI 上升但价格滞涨，要把判断从吸筹延续切换为高位换手/派发风险。",
            ],
        }, thought_us_item(us), thought_t_item(t)]
    })


@app.get("/api/daily-report/listings")
def daily_report_listings():
    cutoff = datetime.now() - timedelta(days=30)
    events = ListingEvent.query.filter(ListingEvent.occurred_at >= cutoff).order_by(ListingEvent.occurred_at.desc()).limit(100).all()
    return jsonify({"events": [{"exchange": item.exchange, "symbol": item.symbol if "/" in item.symbol else (item.symbol[:-4] + "/USDT" if item.symbol.endswith("USDT") else item.symbol), "type": item.event_type, "title": item.title, "source_url": item.source_url, "occurred_at": item.occurred_at.strftime("%m-%d %H:%M:%S"), "effective_at": item.effective_at.strftime("%Y-%m-%d %H:%M UTC+8") if item.effective_at else None} for item in events], "automation_status": automation_statuses("announcement_scan")})


TOKEN_HEDGE_PROFILES = [
    {
        "symbol": "LAB/USDT",
        "name": "LAB",
        "status": "样本复盘",
        "risk_level": "极高",
        "official_facts": [
            "已确认有交易所上线公告与官方空投/交易活动文档；公开资料能确认空投、交易激励、质押池等参与路径。",
            "已确认官方 Telegram 曾公布 14D / 60D / 180D 质押池，且可在 claim portal 查看质押状态。",
            "暂未从官方公开页面确认“0.2 质押价”这一精确细节，需要后续从活动页面快照、链上记录或用户截图继续补证。",
        ],
        "market_thesis": "LAB 是“低成本参与者 + 高位合约套保 + 主力拉爆套保空单 + 解锁前砸回低位”的典型观察样本。后续遇到类似币，不能只看现货涨幅，要同时看质押/空投成本、解锁时间、合约持仓、负资费和基差。",
        "watch_rules": [
            "活动/质押成本远低于二级价格，且上线后合约深度快速变厚，要默认存在大量套保空单。",
            "如果价格被拉到活动成本的数十倍，同时 OI 暴涨、资金费极端、基差失真，要警惕主力正在收割套保盘。",
            "解锁前若出现放量滞涨、负资费、基差/价差异常拉开，优先标为高位派发/砸盘风险。",
        ],
        "sources": [
            {"label": "LBank LAB 上线公告", "url": "https://www.lbk.pub/support/articles/2050601904370614272"},
            {"label": "LAB Season 1 Airdrop", "url": "https://docs.lab.pro/lab-loyalty-airdrop/season-1-loyalty-airdrop"},
            {"label": "LAB Season 2 Trading Airdrop", "url": "https://docs.lab.pro/lab-loyalty-airdrop/season-2-trading-airdrop"},
            {"label": "LAB 质押池公告", "url": "https://t.me/s/lab_trade/450"},
        ],
    },
    {
        "symbol": "US/USDT",
        "name": "Talus Network",
        "status": "重点盯盘",
        "risk_level": "高",
        "official_facts": [
            "官方文档显示 $US 是 Talus Network 的 Sui 原生资产，总量 10,000,000,000，固定总量。",
            "官方解锁结构显示 TGE 约 22.2% 流通；投资人与核心贡献者在 TGE 无流通，1 年 cliff 后按月线性释放。",
            "官方质押计划存在 3 / 6 / 12 个月期限，初始 APY 曾为 160% / 240% / 360%，新质押 APY 会按存入规模动态调整。",
            "交易所公告显示 US 在 2025-12-11 前后集中上线多个现货市场，Binance USUSDT 永续合约于 2025-12-12 18:45（UTC+8）附近上线。",
        ],
        "market_thesis": "US 上线后长期下跌，早期散户筹码可能已被充分清洗；近期约一个半月持续走强并破新高，上方历史套牢盘弱，筹码集中时主力确实可以继续左手倒右手拉盘。现在不能因涨多直接做空，重点盯未来放量失败、负资费、基差打开、OI/CVD 走坏的反转窗口。",
        "watch_rules": [
            "延续信号：新高后回调缩量、OI 不塌、CVD 不持续转负、资金费/基差没有极端恶化，可以继续按强庄延续看。",
            "出货三件套：大量放量 + 资金费转负 + 基差打开；若同时价格冲高失败或跌破近端结构，直接升级为趋势分析推送。",
            "解锁日历：月度社区/基金会释放需要持续记录；2026-12 附近的 1 年 cliff 后投资人/核心贡献者释放，要提前单独拉高风险等级。",
        ],
        "sources": [
            {"label": "Talus $US 官方说明", "url": "https://docs.talus.foundation/token/us"},
            {"label": "官方分配与解锁", "url": "https://docs.talus.foundation/token/us/allocations-and-unlock-schedule"},
            {"label": "官方 $US Staking", "url": "https://docs.talus.foundation/token/staking"},
            {"label": "XT US 上线公告", "url": "https://xtsupport.zendesk.com/hc/en-us/articles/53216586567577-XT-Announcement-US-Talus-Network-Pre-Market-Trading-Closed-Upcoming-Spot-Listing"},
            {"label": "Binance 永续上线记录", "url": "https://www.chaincatcher.com/en/article/2228915"},
        ],
    },
]


@app.get("/api/token-hedge/profiles")
def token_hedge_profiles():
    return jsonify({
        "updated_at": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "profiles": TOKEN_HEDGE_PROFILES,
    })


@app.get("/api/automation-status")
def automation_status_api():
    keys = request.args.get("keys", "")
    task_keys = [key.strip() for key in keys.split(",") if key.strip()] or list(AUTOMATION_LABELS)
    return jsonify({"statuses": automation_statuses(*task_keys), "updated_at": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")})


@app.get("/api/gainers-losers")
def gainers_losers():
    period = request.args.get("period", "change_24h")
    allowed = {"change_5m", "change_15m", "change_30m", "change_1h", "change_4h", "change_12h", "change_24h"}
    if period not in allowed:
        period = "change_24h"
    snapshot = load_latest_market_snapshot()
    if not snapshot:
        return jsonify({"period": period, "rising": [], "falling": [], "updated_at": None})
    enrich_price_changes(snapshot["symbols"])
    rows = [{
        "symbol": group["symbol"],
        "change": group["rows"][0].get(period),
        "price": contract_mid_price(group),
        "volume_24h": group["rows"][0].get("futures_volume"),
    } for group in snapshot["symbols"] if not is_rwa_stock_pair(group["symbol"])]
    rows = [row for row in rows if row["change"] is not None]
    return jsonify({"period": period, "updated_at": snapshot["updated_at"], "rising": sorted(rows, key=lambda row: row["change"], reverse=True)[:50], "falling": sorted(rows, key=lambda row: row["change"])[:50]})


@app.get("/api/symbol-detail")
def symbol_detail():
    symbol = request.args.get("symbol", "").upper().replace("-", "/")
    if not symbol.endswith("/USDT"):
        symbol = symbol.replace("USDT", "") + "/USDT"
    start = request.args.get("start")
    end = request.args.get("end")
    spot_rows = LatestMarketSnapshot.query.filter_by(symbol=symbol).all()
    dual_rows = LatestDualFuturesSnapshot.query.filter_by(symbol=symbol).all()
    spot = [{"exchange": row.long_exchange, "bid": row.long_bid, "ask": row.long_ask, "mid": (row.long_bid + row.long_ask) / 2, "volume_24h": row.spot_volume} for row in spot_rows]
    futures = {}
    for row in dual_rows:
        futures.setdefault(row.long_exchange, {"exchange": row.long_exchange, "bid": row.long_bid, "ask": row.long_ask, "mid": (row.long_bid + row.long_ask) / 2, "basis": row.long_basis, "index": row.long_index, "volume_24h": row.long_volume, "open_interest": row.long_open_interest})
        futures.setdefault(row.short_exchange, {"exchange": row.short_exchange, "bid": row.short_bid, "ask": row.short_ask, "mid": (row.short_bid + row.short_ask) / 2, "basis": row.short_basis, "index": row.short_index, "volume_24h": row.short_volume, "open_interest": row.short_open_interest})
    bn_spot = next((row for row in spot_rows if row.long_exchange == "Binance"), None)
    if bn_spot:
        futures.setdefault("Binance", {"exchange": "Binance", "bid": bn_spot.short_bid, "ask": bn_spot.short_ask, "mid": (bn_spot.short_bid + bn_spot.short_ask) / 2, "basis": bn_spot.basis, "index": None, "volume_24h": bn_spot.futures_volume, "open_interest": bn_spot.futures_open_interest})
    query = FundingRateRecord.query.filter_by(symbol=symbol.replace("/", ""))
    try:
        if start:
            query = query.filter(FundingRateRecord.funding_time >= int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=SHANGHAI_TZ).timestamp() * 1000))
        if end:
            query = query.filter(FundingRateRecord.funding_time < int((datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1)).timestamp() * 1000))
    except ValueError:
        pass
    funding_rows = list(reversed(query.order_by(FundingRateRecord.funding_time.desc()).limit(500).all()))
    funding = [{"time": datetime.fromtimestamp(row.funding_time / 1000, tz=timezone.utc).astimezone(SHANGHAI_TZ).strftime("%m-%d %H:%M"), "date": datetime.fromtimestamp(row.funding_time / 1000, tz=timezone.utc).astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d"), "rate": row.funding_rate} for row in funding_rows]
    funding_daily = {}
    for item in funding:
        funding_daily[item["date"]] = funding_daily.get(item["date"], 0.0) + item["rate"]
    component = IndexComponentSnapshot.query.filter_by(exchange="Binance", symbol=symbol.replace("/", "")).first()
    trends = {}
    bn_futures = futures.get("Binance")
    if bn_futures:
        current = bn_futures["mid"]
        now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
        points = {item.bucket_at: item.price for item in FuturesPriceHistory.query.filter_by(symbol=symbol).filter(FuturesPriceHistory.bucket_at.in_([now_bucket - seconds for seconds in TREND_WINDOWS.values()])).all()}
        for key, seconds in TREND_WINDOWS.items():
            previous = points.get(now_bucket - seconds)
            trends[key] = (current - previous) / previous * 100 if previous else None
    return jsonify({"symbol": symbol, "spot": spot, "futures": sorted(futures.values(), key=lambda item: item["exchange"] != "Binance"), "funding": funding, "funding_total": sum(item["rate"] for item in funding), "funding_daily": funding_daily, "binance_index_components": json.loads(component.components_json) if component else [], "trends": trends})


def positive_binance_funding_streak(symbol, minimum=0.005, periods=3):
    rows = FundingRateRecord.query.filter_by(symbol=symbol.replace("/", "")).order_by(FundingRateRecord.funding_time.desc()).limit(periods).all()
    return len(rows) == periods and all(row.funding_rate > minimum for row in rows)


def spot_futures_simple_funding_threshold(interval_hours):
    try:
        interval = int(float(interval_hours or 0))
    except (TypeError, ValueError):
        interval = 0
    if interval == 8:
        return 0.01
    if interval == 4:
        return 0.005
    if interval == 1:
        return 0.005
    return 0.005


def is_low_insurance_funding(rate, minimum):
    if rate is None:
        return False
    return abs(float(rate) - minimum) < 0.000001


def spot_futures_history_is_all_low_insurance(symbol, interval_hours):
    """Filter only when previous settled funding and every settled period in the last 24H are exactly low-insurance funding."""
    minimum = spot_futures_simple_funding_threshold(interval_hours)
    try:
        interval = int(float(interval_hours or 0))
    except (TypeError, ValueError):
        interval = 0
    expected_periods = max(1, int(24 / interval)) if interval in {1, 2, 4, 8} else 3
    rows = FundingRateRecord.query.filter_by(symbol=symbol.replace("/", "")).order_by(FundingRateRecord.funding_time.desc()).limit(expected_periods).all()
    if len(rows) < expected_periods:
        return False
    return all(is_low_insurance_funding(row.funding_rate, minimum) for row in rows)


@app.get("/api/arbitrage-thinking/simple")
def simple_arbitrage_thinking():
    spot_snapshot = load_latest_market_snapshot()
    dual_snapshot = load_latest_dual_futures_snapshot()
    spot_simple = []
    if spot_snapshot:
        enrich_funding_statistics(spot_snapshot["symbols"])
        for group in spot_snapshot["symbols"]:
            if is_rwa_stock_pair(group["symbol"]):
                continue
            for row in group["rows"]:
                if row["open_spread"] > 0 and row["funding_rate"] > 0 and not spot_futures_history_is_all_low_insurance(group["symbol"], row.get("funding_interval_hours")):
                    spot_simple.append({"symbol": group["symbol"], "long_exchange": row["long_exchange"], "short_exchange": "Binance", "open_spread": row["open_spread"], "close_spread": row["close_spread"], "funding": row["funding_rate"], "funding_current": row["funding_rate"], "funding_previous": row.get("funding_previous"), "funding_24h": row.get("funding_24h"), "funding_3d": row.get("funding_3d"), "long_is_spot": True, "short_is_spot": False, "long_interval": None, "short_interval": row.get("funding_interval_hours"), "long_open_interest": None, "short_open_interest": row.get("futures_open_interest"), "long_volume": row.get("spot_volume"), "short_volume": row.get("futures_volume")})
    dual_simple = []
    if dual_snapshot:
        dual_stats = funding_statistics([group["symbol"].replace("/", "") for group in dual_snapshot["symbols"]])
        for group in dual_snapshot["symbols"]:
            stats = dual_stats.get(group["symbol"].replace("/", ""), {})
            for row in group["rows"]:
                # Current net funding must be positive and the Binance settlement side
                # must also have remained positive across recent periods.
                if row["open_spread"] > 0 and (row.get("funding_difference") or 0) > 0.005 and positive_binance_funding_streak(group["symbol"], minimum=0, periods=3):
                    dual_simple.append({"symbol": group["symbol"], "long_exchange": row["long_exchange"], "short_exchange": row["short_exchange"], "open_spread": row["open_spread"], "close_spread": row["close_spread"], "funding": row["funding_difference"], "funding_current": row.get("funding_difference"), "funding_previous": stats.get("previous"), "funding_24h": stats.get("day_1"), "funding_3d": stats.get("day_3"), "long_is_spot": False, "short_is_spot": False, "long_interval": row.get("long_funding_interval_hours"), "short_interval": row.get("short_funding_interval_hours"), "long_open_interest": row.get("long_open_interest"), "short_open_interest": row.get("short_open_interest"), "long_volume": row.get("long_volume"), "short_volume": row.get("short_volume")})
    return jsonify({"spot_simple": sorted(spot_simple, key=lambda item: item["open_spread"], reverse=True), "dual_simple": sorted(dual_simple, key=lambda item: item["open_spread"], reverse=True)})


@app.get("/api/alerts")
def alerts():
    all_events = AlertEvent.query.order_by(AlertEvent.created_at.desc()).limit(200).all()
    # 旧版拉升报警没有连续采样确认凭证，不能再作为可靠信号展示。
    all_events = [item for item in all_events if (item.alert_type == "basis_threshold" or item.alert_type.startswith("rapid_") or "确认后的" in item.message) and not is_rwa_stock_pair(item.symbol)]
    grouped_events = {}
    for item in all_events:
        grouped_events.setdefault(item.symbol, []).append(item)
    grouped_events = dict(list(grouped_events.items())[:30])
    latest_events = [items[0] for items in grouped_events.values()]
    active_events = [item for item in latest_events if (datetime.now() - item.created_at).total_seconds() <= 120]
    tracking = [item for item in BasisTracking.query.filter_by(resolved_at=None).order_by(BasisTracking.max_abs_basis.desc()).limit(50).all() if not is_rwa_stock_pair(item.symbol)]
    def alert_context(item):
        if item.strategy == "futures_futures":
            latest = LatestDualFuturesSnapshot.query.filter_by(symbol=item.symbol, long_exchange=item.long_exchange, short_exchange=item.short_exchange).first()
            return {
                "strategy": "futures_futures",
                "long_exchange": item.long_exchange or "Bybit",
                "long_market": "合约",
                "short_exchange": item.short_exchange or "Binance",
                "short_market": "合约",
                "long_interval": latest.long_funding_interval_hours if latest else None,
                "short_interval": latest.short_funding_interval_hours if latest else None,
            }
        long_exchange = next((name for name in ("Binance", "Gate", "Bitget") if item.message.startswith(name)), "Binance")
        latest = LatestMarketSnapshot.query.filter_by(symbol=item.symbol).first()
        return {
            "strategy": "spot_futures",
            "long_exchange": long_exchange,
            "long_market": "现货",
            "short_exchange": "Binance",
            "short_market": "合约",
            "long_interval": None,
            "short_interval": latest.funding_interval_hours if latest else None,
        }

    def event_payload(item):
        return {"id": item.id, "symbol": item.symbol, "type": item.alert_type, "message": item.message, "open_spread": item.open_spread, "close_spread": item.close_spread, "basis": item.basis, "funding_rate": item.funding_rate, "created_at": item.created_at.strftime("%m-%d %H:%M:%S"), "remaining_seconds": max(0, int(120 - (datetime.now() - item.created_at).total_seconds())), **alert_context(item)}

    def alert_group_payload(symbol, items):
        timeline = [event_payload(item) for item in items[:20]]
        latest = timeline[0]
        spread_items = [item for item in items if item.alert_type == "rapid_spread" and item.open_spread is not None]
        basis_items = [item for item in items if ("basis" in item.alert_type) and item.basis is not None]
        if not spread_items:
            spread_items = [item for item in items if item.open_spread is not None]
        if not basis_items:
            basis_items = [item for item in items if item.basis is not None]
        max_spread_item = max(spread_items, key=lambda item: abs(item.open_spread or 0), default=None)
        max_basis_item = max(basis_items, key=lambda item: abs(item.basis or 0), default=None)
        return {
            "symbol": symbol,
            "latest": latest,
            "first_at": timeline[-1]["created_at"],
            "alert_count": len(items),
            "move_peaks": {
                "open_spread": max_spread_item.open_spread if max_spread_item else None,
                "open_spread_at": max_spread_item.created_at.strftime("%m-%d %H:%M:%S") if max_spread_item else None,
                "basis": max_basis_item.basis if max_basis_item else None,
                "basis_at": max_basis_item.created_at.strftime("%m-%d %H:%M:%S") if max_basis_item else None,
            },
            "timeline": timeline,
        }

    return jsonify({
        "events": [alert_group_payload(symbol, items) for symbol, items in grouped_events.items()],
        "active_events": [event_payload(item) for item in active_events],
        "basis_tracking": [{"symbol": item.symbol, "strategy": item.strategy or "spot_futures", "direction": item.direction, "started_at": item.started_at.strftime("%m-%d %H:%M:%S"), "opening_basis": item.opening_basis, "last_level": item.last_recorded_level, "max_basis": item.max_basis, "max_at": item.max_at.strftime("%m-%d %H:%M:%S")} for item in tracking],
    })


@app.get("/api/strategies")
def list_strategies():
    return jsonify([{
        "id": item.id, "name": item.name, "mode": item.mode, "symbol": item.symbol,
        "enabled": item.enabled,
    } for item in Strategy.query.order_by(Strategy.id.desc()).all()])


@app.post("/api/strategies")
def create_strategy():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    mode = str(data.get("mode", "")).strip()
    symbol = str(data.get("symbol", "")).strip().upper()
    if not name or mode not in {"跨所合约套利", "现货多/合约空", "资金费率套利"} or symbol not in MARKETS:
        return jsonify({"error": "请填写有效的策略名称、类型和交易对。"}), 400
    item = Strategy(name=name, mode=mode, symbol=symbol)
    db.session.add(item)
    db.session.commit()
    return jsonify({"id": item.id, "message": "策略已创建"}), 201


@app.patch("/api/strategies/<int:strategy_id>")
def toggle_strategy(strategy_id):
    item = db.get_or_404(Strategy, strategy_id)
    item.enabled = not item.enabled
    db.session.commit()
    return jsonify({"id": item.id, "enabled": item.enabled})


with app.app_context():
    db.create_all()
    alert_columns = {column["name"] for column in inspect(db.engine).get_columns("alert_event")}
    for column_name, column_type in (("strategy", "VARCHAR(30)"), ("long_exchange", "VARCHAR(30)"), ("short_exchange", "VARCHAR(30)")):
        if column_name not in alert_columns:
            db.session.execute(text(f"ALTER TABLE alert_event ADD COLUMN {column_name} {column_type}"))
    db.session.execute(text("UPDATE alert_event SET strategy = 'spot_futures' WHERE strategy IS NULL"))
    tracking_columns = {column["name"] for column in inspect(db.engine).get_columns("basis_tracking")}
    for column_name, column_type in (("strategy", "VARCHAR(30)"), ("opening_basis", "FLOAT")):
        if column_name not in tracking_columns:
            db.session.execute(text(f"ALTER TABLE basis_tracking ADD COLUMN {column_name} {column_type}"))
    db.session.execute(text("UPDATE basis_tracking SET strategy = 'spot_futures' WHERE strategy IS NULL"))
    for tracking in BasisTracking.query.filter(BasisTracking.opening_basis.is_(None)).all():
        first_log = BasisExpansionLog.query.filter_by(tracking_id=tracking.id).order_by(BasisExpansionLog.created_at).first()
        tracking.opening_basis = first_log.observed_basis if first_log else tracking.max_basis
    dual_columns = {column["name"] for column in inspect(db.engine).get_columns("latest_dual_futures_snapshot")}
    for column_name in ("long_index", "short_index", "long_volume", "short_volume", "long_open_interest", "short_open_interest"):
        if column_name not in dual_columns:
            db.session.execute(text(f"ALTER TABLE latest_dual_futures_snapshot ADD COLUMN {column_name} FLOAT"))
    market_columns = {column["name"] for column in inspect(db.engine).get_columns("latest_market_snapshot")}
    for column_name in ("spot_volume", "futures_volume", "futures_open_interest"):
        if column_name not in market_columns:
            db.session.execute(text(f"ALTER TABLE latest_market_snapshot ADD COLUMN {column_name} FLOAT"))
    listing_event_columns = {column["name"] for column in inspect(db.engine).get_columns("listing_event")}
    for column_name, column_type in (("title", "VARCHAR(500)"), ("source_url", "VARCHAR(1000)"), ("announcement", "BOOLEAN DEFAULT 0"), ("effective_at", "DATETIME")):
        if column_name not in listing_event_columns:
            db.session.execute(text(f"ALTER TABLE listing_event ADD COLUMN {column_name} {column_type}"))
    daily_horn_columns = {column["name"] for column in inspect(db.engine).get_columns("daily_horn_signal")}
    for column_name in ("oi_value", "ratio_value"):
        if column_name not in daily_horn_columns:
            db.session.execute(text(f"ALTER TABLE daily_horn_signal ADD COLUMN {column_name} FLOAT"))
    inspect(db.engine).get_columns("lark_push_state")
    db.session.commit()
    if not Strategy.query.first():
        db.session.add(Strategy(name="BTC 跨所价差监控", mode="跨所合约套利", symbol="BTC/USDT"))
        db.session.add(Strategy(name="ETH 资金费率观察", mode="资金费率套利", symbol="ETH/USDT", enabled=False))
        db.session.commit()


def background_spot_market_refresh():
    while True:
        cycle_started_at = time.time()
        try:
            with app.app_context():
                spot_futures_snapshot()
        except Exception as exc:
            MARKET_REFRESH_METRICS["last_error"] = f"{type(exc).__name__}: {exc}"
            with app.app_context():
                db.session.rollback()
        time.sleep(max(0, MARKET_REFRESH_SECONDS - (time.time() - cycle_started_at)))


def background_dual_market_refresh():
    while True:
        cycle_started_at = time.time()
        try:
            with app.app_context():
                dual_futures_snapshot()
        except Exception:
            with app.app_context():
                db.session.rollback()
        time.sleep(max(0, MARKET_REFRESH_SECONDS - (time.time() - cycle_started_at)))


def background_funding_history_sync():
    global FUNDING_SYNC_CURSOR
    time.sleep(5)
    while True:
        try:
            with app.app_context():
                snapshot = load_latest_market_snapshot()
                if snapshot:
                    symbols = [group["symbol"].replace("/", "") for group in snapshot["symbols"]]
                    batch = symbols[FUNDING_SYNC_CURSOR:FUNDING_SYNC_CURSOR + 60]
                    if not batch:
                        FUNDING_SYNC_CURSOR = 0
                        batch = symbols[:60]
                    FUNDING_SYNC_CURSOR = (FUNDING_SYNC_CURSOR + len(batch)) % len(symbols)
                    sync_funding_history(batch)
        except Exception:
            db.session.rollback()
        time.sleep(FUNDING_HISTORY_SYNC_SECONDS)


def background_price_history_backfill():
    time.sleep(60)
    while True:
        try:
            with app.app_context():
                snapshot = load_latest_market_snapshot()
                if snapshot:
                    backfill_price_history(snapshot["symbols"])
        except Exception:
            db.session.rollback()
        time.sleep(PRICE_BACKFILL_SYNC_SECONDS)


def background_announcement_scan():
    global LAST_ANNOUNCEMENT_SCAN_DATE
    while True:
        now = datetime.now(SHANGHAI_TZ)
        if now.hour >= ANNOUNCEMENT_SCAN_HOUR and LAST_ANNOUNCEMENT_SCAN_DATE != now.date():
            try:
                with app.app_context():
                    mark_automation_status("announcement_scan", "started")
                    scan_exchange_announcements()
                    mark_automation_status("announcement_scan", "success")
                LAST_ANNOUNCEMENT_SCAN_DATE = now.date()
            except Exception as exc:
                with app.app_context():
                    db.session.rollback()
                    mark_automation_status("announcement_scan", "error", exc)
        time.sleep(60)


def background_daily_horn_scan():
    global LAST_HORN_SCAN_DATE, LAST_LARK_TREND_PUSH_DATE
    while True:
        now = datetime.now(SHANGHAI_TZ)
        report_date = now.strftime("%Y-%m-%d")
        if now.hour == HORN_SCAN_HOUR and LAST_HORN_SCAN_DATE != now.date():
            try:
                with app.app_context():
                    mark_automation_status("daily_horn_scan", "started")
                    if not lark_daily_trend_already_pushed(report_date):
                        scan_daily_horn_signals()
                        mark_automation_status("daily_horn_scan", "success")
                        mark_automation_status("daily_lark_trend_push", "started")
                        if send_daily_lark_trend_report():
                            mark_lark_daily_trend_pushed(report_date)
                            mark_automation_status("daily_lark_trend_push", "success")
                        else:
                            mark_automation_status("daily_lark_trend_push", "error", "未发送：无 webhook、无候选或 Lark 返回失败")
                    else:
                        mark_automation_status("daily_horn_scan", "success")
                    LAST_HORN_SCAN_DATE = now.date()
                    if lark_daily_trend_already_pushed(report_date):
                        LAST_LARK_TREND_PUSH_DATE = now.date()
            except Exception as exc:
                with app.app_context():
                    db.session.rollback()
                    mark_automation_status("daily_horn_scan", "error", exc)
        time.sleep(60)


def background_transfer_network_sync():
    time.sleep(10)
    while True:
        try:
            with app.app_context():
                mark_automation_status("transfer_network_sync", "started")
                refresh_public_transfer_networks()
                mark_automation_status("transfer_network_sync", "success")
        except Exception as exc:
            with app.app_context():
                db.session.rollback()
                mark_automation_status("transfer_network_sync", "error", exc)
        time.sleep(TRANSFER_NETWORK_SYNC_SECONDS)


def background_index_component_sync():
    time.sleep(2)
    while True:
        try:
            with app.app_context():
                mark_automation_status("index_component_sync", "started")
                refresh_index_components()
                mark_automation_status("index_component_sync", "success")
        except Exception as exc:
            with app.app_context():
                db.session.rollback()
                mark_automation_status("index_component_sync", "error", exc)
        time.sleep(INDEX_COMPONENT_REFRESH_SECONDS)


def background_thought_analysis_push():
    time.sleep(20)
    while True:
        try:
            with app.app_context():
                mark_automation_status("thought_analysis_push", "started")
                send_thought_analysis_push()
                mark_automation_status("thought_analysis_push", "success")
        except Exception as exc:
            with app.app_context():
                db.session.rollback()
                mark_automation_status("thought_analysis_push", "error", exc)
        time.sleep(300)


def start_background_workers():
    global BACKGROUND_WORKERS_STARTED
    if BACKGROUND_WORKERS_STARTED:
        return
    BACKGROUND_WORKERS_STARTED = True
    threading.Thread(target=background_spot_market_refresh, daemon=True, name="spot-market-refresh").start()
    threading.Thread(target=background_dual_market_refresh, daemon=True, name="dual-market-refresh").start()
    threading.Thread(target=background_funding_history_sync, daemon=True, name="funding-history-sync").start()
    threading.Thread(target=background_price_history_backfill, daemon=True, name="price-history-backfill").start()
    threading.Thread(target=background_index_component_sync, daemon=True, name="index-component-sync").start()
    threading.Thread(target=background_announcement_scan, daemon=True, name="announcement-scan").start()
    threading.Thread(target=background_daily_horn_scan, daemon=True, name="daily-horn-scan").start()
    threading.Thread(target=background_transfer_network_sync, daemon=True, name="transfer-network-sync").start()
    threading.Thread(target=background_thought_analysis_push, daemon=True, name="thought-analysis-push").start()


if __name__ == "__main__":
    start_background_workers()
    app.run(host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "5000")), debug=False)
