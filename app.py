import os
import random
import json
import time
import threading
import re
import html
import hashlib
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_, case, func, inspect, or_, text, tuple_

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "local-development-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///arbitrage_hub.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
db = SQLAlchemy(app)


@app.after_request
def disable_local_static_cache(response):
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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


class TradeValidation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)
    entry_price = db.Column(db.Float, nullable=False)
    stop_price = db.Column(db.Float, nullable=False)
    take_profit_1 = db.Column(db.Float, nullable=False)
    take_profit_2 = db.Column(db.Float, nullable=False)
    stake_usdt = db.Column(db.Float, nullable=False, default=100.0)
    leverage = db.Column(db.Float, nullable=False, default=1.0)
    status = db.Column(db.String(20), nullable=False, default="planned", index=True)
    thesis = db.Column(db.String(1000))
    opened_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    exit_price = db.Column(db.Float)
    exit_reason = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class TradeValidationCandle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    interval = db.Column(db.String(10), nullable=False, default="5m", index=True)
    bucket_at = db.Column(db.BigInteger, nullable=False, index=True)
    open = db.Column(db.Float, nullable=False)
    high = db.Column(db.Float, nullable=False)
    low = db.Column(db.Float, nullable=False)
    close = db.Column(db.Float, nullable=False)
    volume = db.Column(db.Float)
    quote_volume = db.Column(db.Float)
    captured_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    __table_args__ = (db.UniqueConstraint("symbol", "interval", "bucket_at", name="uq_trade_validation_candle_symbol_interval_bucket"),)


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


class EarlyTrendSignal(db.Model):
    """30 分钟早期启动与强启动信号；阶段判断单独存表，避免被结构分覆盖。"""
    id = db.Column(db.Integer, primary_key=True)
    report_date = db.Column(db.String(10), nullable=False, index=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    signal_type = db.Column(db.String(20), nullable=False, index=True)
    stage_key = db.Column(db.String(24), nullable=False)
    stage_label = db.Column(db.String(60), nullable=False)
    stage_number = db.Column(db.Integer, nullable=False, default=0)
    stage_reason = db.Column(db.String(500), nullable=False)
    price_change_5 = db.Column(db.Float, nullable=False)
    cvd_change_5 = db.Column(db.Float, nullable=False)
    oi_change_5 = db.Column(db.Float, nullable=False)
    ratio_change_5 = db.Column(db.Float, nullable=False)
    prior_price_change = db.Column(db.Float)
    prior_range = db.Column(db.Float)
    volume_ratio = db.Column(db.Float)
    last_price = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    __table_args__ = (db.UniqueConstraint("report_date", "symbol", name="uq_early_trend_signal"),)


class LarkPushState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel = db.Column(db.String(40), nullable=False)
    symbol = db.Column(db.String(30), nullable=False)
    signal_key = db.Column(db.String(120), nullable=False)
    pushed_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    __table_args__ = (db.UniqueConstraint("channel", "symbol", "signal_key", name="uq_lark_push_state"),)


class ThoughtPushSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, unique=True, index=True)
    direction = db.Column(db.String(60), nullable=False)
    signal_key = db.Column(db.String(120), nullable=False)
    last_price = db.Column(db.Float)
    basis = db.Column(db.Float)
    funding_rate = db.Column(db.Float)
    oi_value = db.Column(db.Float)
    futures_volume = db.Column(db.Float)
    spot_volume = db.Column(db.Float)
    cvd_30m = db.Column(db.Float)
    cvd_1h = db.Column(db.Float)
    cvd_2h = db.Column(db.Float)
    price_change_30m = db.Column(db.Float)
    price_change_1h = db.Column(db.Float)
    price_change_2h = db.Column(db.Float)
    oi_change_30m = db.Column(db.Float)
    oi_change_1h = db.Column(db.Float)
    oi_change_2h = db.Column(db.Float)
    ratio_change_30m = db.Column(db.Float)
    ratio_change_1h = db.Column(db.Float)
    ratio_change_2h = db.Column(db.Float)
    wall_qty = db.Column(db.Float)
    wall_notional = db.Column(db.Float)
    pushed_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class ThoughtPushEvent(db.Model):
    """不可覆盖的思路推送审计记录，同时用于重启/并发场景的发送预占位。"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, index=True)
    direction = db.Column(db.String(60), nullable=False)
    signal_key = db.Column(db.String(120), nullable=False)
    reservation_key = db.Column(db.String(64), nullable=False, unique=True, index=True)
    trigger_reason = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="reserved", index=True)
    snapshot_json = db.Column(db.Text)
    message_text = db.Column(db.Text)
    error_text = db.Column(db.String(500))
    reserved_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    sent_at = db.Column(db.DateTime)


class ThoughtWatch(db.Model):
    """可由网页管理的盯盘清单；思路历史保留，active只控制后续自动扫描和推送。"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False, unique=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    start_price = db.Column(db.Float)
    stopped_at = db.Column(db.DateTime)
    stop_price = db.Column(db.Float)
    note = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class SymbolAlias(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    canonical_symbol = db.Column(db.String(30), nullable=False, index=True)
    alias_symbol = db.Column(db.String(30), nullable=False, index=True)
    canonical_base = db.Column(db.String(30), nullable=False, index=True)
    alias_base = db.Column(db.String(30), nullable=False, index=True)
    exchange = db.Column(db.String(30), nullable=False, default="ANY")
    market_type = db.Column(db.String(30), nullable=False, default="contract")
    multiplier = db.Column(db.Float, nullable=False, default=1.0)
    verified = db.Column(db.Boolean, nullable=False, default=True)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    __table_args__ = (db.UniqueConstraint("alias_symbol", "exchange", "market_type", name="uq_symbol_alias_scope"),)


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
DUAL_FUTURES_CACHE = {"snapshot": None}
LAST_MARKET_DB_PERSIST_AT = {"spot": 0.0, "dual": 0.0}
MARKET_PAYLOAD_CACHE = {}
SPOT_VIEW_CACHE = {"key": None, "symbols": None}
DUAL_VIEW_CACHE = {"key": None, "symbols": None}
FUNDING_HISTORY_CACHE = {}
FUNDING_STATISTICS_CACHE = {"ts": 0.0, "symbols": frozenset(), "data": {}}
FUNDING_STATISTICS_LOCK = threading.Lock()
LAST_PRICE_HISTORY_BUCKET = None
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
STATIC_RWA_STOCK_SYMBOLS = {
    # Binance/partner TradFi perpetuals are sometimes unavailable in exchangeInfo during partial refreshes.
    # Keep a local guard so stock, ETF and metal RWA contracts do not leak into crypto arbitrage scanners.
    "AAOIUSDT", "AAPLUSDT", "ADBEUSDT", "ALABUSDT", "AMATUSDT", "AMDUSDT", "AMZNUSDT", "APPUSDT",
    "ARMUSDT", "ASMLUSDT", "ASTSUSDT", "AVGOUST", "AVGOUSDT", "BABAUSDT", "BMNRUSDT", "CIENUSDT",
    "COHRUSDT", "COINUSDT", "COSTUSDT", "CRCLUSDT", "CRDOUSDT", "CRMUSDT", "CRWDUSDT", "CRWVUSDT",
    "CSCOUSDT", "DELLUSDT", "DKNGUSDT", "FLNCUSDT", "GEVUSDT", "GLWUSDT", "GMEUSDT", "GOOGLUSDT",
    "HIMSUSDT", "HOODUSDT", "HPEUSDT", "HYUNDAIUSDT", "IBMUSDT", "INTCUSDT", "IRENUSDT", "IWMUSDT",
    "KLACUSDT", "KORUUSDT", "LLYUSDT", "LRCXUSDT", "METAUSDT", "MRVLUSDT", "MSFTUSDT", "MSTRUSDT",
    "MUUSDT", "NBISUSDT", "NFLXUSDT", "NOKUSDT", "NOWUSDT", "NVDAUSDT", "ONDSUSDT", "ORCLUSDT",
    "PANWUSDT", "PLTRUSDT", "QCOMUSDT", "QQQUSDT", "RIVNUSDT", "RKLBUSDT", "SAMSUNGUSDT",
    "SKHYNIXUSDT", "SMCIUSDT", "SNDKUSDT", "SNOWUSDT", "SOFIUSDT", "SONYUSDT", "SOXLUSDT",
    "SOXSUSDT", "SPYUSDT", "SQQQUSDT", "TERUSDT", "TQQQUSDT", "TSLAUSDT", "TSMUSDT", "TTWOUSDT",
    "TXNUSDT", "TZAUSDT", "URNMUSDT", "UVXYUSDT", "VRTUSDT", "WDCUSDT", "XAGUSDT", "XAUUSDT",
    "XBIUSDT", "XLEUSDT", "XPDUSDT", "XPTUSDT", "ZMUSDT",
}
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
MARKET_DB_PERSIST_SECONDS = 15
HORN_SCAN_HOUR = 8
LAST_HORN_SCAN_DATE = None
LAST_LARK_TREND_PUSH_DATE = None
INDEX_COMPONENT_REFRESH_SECONDS = 5 * 60
FUNDING_HISTORY_SYNC_SECONDS = 60
PRICE_BACKFILL_SYNC_SECONDS = 2 * 60
PRICE_HISTORY_BUCKET_SECONDS = 5 * 60
PRICE_HISTORY_RETENTION_SECONDS = 8 * 24 * 60 * 60
TRADE_VALIDATION_CANDLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
TRADE_VALIDATION_CHART_SECONDS = 3 * 24 * 60 * 60
TRADE_VALIDATION_INTERVAL = "5m"
TRADE_VALIDATION_AUTO_SYMBOLS = {"MIRA/USDT"}
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
CONTRACT_SPOT_ALIASES = {
    "1000XECUSDT": {"spot_symbol": "XECUSDT", "multiplier": 1000, "canonical": "XEC/USDT"},
}
SEEDED_SYMBOL_ALIASES = [
    {
        "canonical_symbol": "XEC/USDT",
        "alias_symbol": "1000XEC/USDT",
        "canonical_base": "XEC",
        "alias_base": "1000XEC",
        "exchange": "ANY",
        "market_type": "contract",
        "multiplier": 1000,
        "note": "倍率合约：1 张 1000XEC 合约对应 1000 枚 XEC 标的。",
    },
]
SPOT_CONTRACT_SYMBOL_MISMATCHES = {
    ("EDGEUSDT", "Gate"): {
        "spot_project": "Definitive",
        "spot_chain": "BASEEVM",
        "spot_contract": "0xed6e000def95780fb89734c07ee2ce9f6dcaf110",
        "spot_website": "https://www.definitive.fi",
        "contract_project": "edgeX",
        "note": "Gate EDGE 现货是 Definitive；Binance EDGEUSDT 合约是 edgeX，同名不同币，禁止现多期空匹配。",
    },
}


def contract_spot_alias(symbol):
    return CONTRACT_SPOT_ALIASES.get(symbol, {"spot_symbol": symbol, "multiplier": 1, "canonical": f"{symbol[:-4]}/USDT"})


def canonical_market_symbol(symbol):
    compact = symbol.upper().replace("/", "")
    if compact in CONTRACT_SPOT_ALIASES:
        return CONTRACT_SPOT_ALIASES[compact]["canonical"]
    return f"{compact[:-4]}/USDT" if compact.endswith("USDT") else symbol.upper()


def compact_pair(symbol):
    return str(symbol or "").upper().replace("/", "").replace("-", "").replace("_", "")


def pair_base(symbol):
    compact = compact_pair(symbol)
    return compact[:-4] if compact.endswith("USDT") else compact.split("/", 1)[0]


def pair_slash(symbol):
    compact = compact_pair(symbol)
    return f"{compact[:-4]}/USDT" if compact.endswith("USDT") else str(symbol or "").upper()


def is_spot_contract_symbol_mismatch(symbol, exchange):
    return (compact_pair(symbol), exchange) in SPOT_CONTRACT_SYMBOL_MISMATCHES


def symbol_alias_rows():
    rows = []
    try:
        rows = SymbolAlias.query.filter_by(verified=True).all()
    except Exception:
        rows = []
    if rows:
        return rows

    class SeedAlias:
        def __init__(self, data):
            self.__dict__.update(data)
            self.verified = True
    return [SeedAlias(item) for item in SEEDED_SYMBOL_ALIASES]


def symbol_alias_candidates(symbol):
    canonical = canonical_market_symbol(symbol)
    symbol_pair = pair_slash(symbol)
    candidates = {compact_pair(symbol_pair), pair_base(symbol_pair), compact_pair(canonical), pair_base(canonical)}
    for row in symbol_alias_rows():
        if row.canonical_symbol == canonical or row.alias_symbol == symbol_pair or row.alias_symbol == canonical:
            candidates.update({
                compact_pair(row.canonical_symbol),
                compact_pair(row.alias_symbol),
                row.canonical_base.upper(),
                row.alias_base.upper(),
            })
    return {item for item in candidates if item}


def symbol_matches_query(symbol, raw_query):
    query = compact_pair(raw_query)
    if not query:
        return True
    full_pair_search = "/" in raw_query or query.endswith("USDT")
    candidates = symbol_alias_candidates(symbol)
    if full_pair_search:
        return query in candidates
    return any(candidate == query or candidate.startswith(query) for candidate in candidates)


def seed_symbol_aliases():
    for data in SEEDED_SYMBOL_ALIASES:
        existing = SymbolAlias.query.filter_by(
            alias_symbol=data["alias_symbol"],
            exchange=data["exchange"],
            market_type=data["market_type"],
        ).first()
        if existing:
            for key, value in data.items():
                setattr(existing, key, value)
            existing.verified = True
            existing.updated_at = datetime.now()
        else:
            db.session.add(SymbolAlias(verified=True, **data))


def get_json(url, timeout=4):
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ArbiScope/1.0",
        "Accept": "application/json",
    })
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
    "turnover_basis_watch": "SOON/ZAMA基差换手监控",
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
    compact = symbol.replace("/", "").replace("-", "")
    return compact in RWA_STOCK_SYMBOLS or compact in STATIC_RWA_STOCK_SYMBOLS


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
    requested_symbols = frozenset(symbols)
    def cached_result(now_ts):
        if (
            FUNDING_STATISTICS_CACHE["data"]
            and now_ts - FUNDING_STATISTICS_CACHE["ts"] < 60
            and requested_symbols.issubset(FUNDING_STATISTICS_CACHE["symbols"])
        ):
            return {symbol: FUNDING_STATISTICS_CACHE["data"].get(symbol, {}) for symbol in symbols}
        return None

    result = cached_result(time.time())
    if result is not None:
        return result
    # 防止首轮多个页面同时命中空缓存，重复读取数万条固定历史资费记录。
    with FUNDING_STATISTICS_LOCK:
        now_ts = time.time()
        result = cached_result(now_ts)
        if result is not None:
            return result
        now = datetime.now(SHANGHAI_TZ)
        start_day = now.date() - timedelta(days=29)
        start_time = int(datetime.combine(start_day, datetime.min.time(), tzinfo=SHANGHAI_TZ).timestamp() * 1000)
        end_time = int(now.timestamp() * 1000)
        starts = {
            days: int(datetime.combine(now.date() - timedelta(days=days - 1), datetime.min.time(), tzinfo=SHANGHAI_TZ).timestamp() * 1000)
            for days in (1, 3, 7, 30)
        }
        aggregates = db.session.query(
            FundingRateRecord.symbol,
            func.sum(case((FundingRateRecord.funding_time >= starts[1], FundingRateRecord.funding_rate), else_=0.0)),
            func.sum(case((FundingRateRecord.funding_time >= starts[3], FundingRateRecord.funding_rate), else_=0.0)),
            func.sum(case((FundingRateRecord.funding_time >= starts[7], FundingRateRecord.funding_rate), else_=0.0)),
            func.sum(case((FundingRateRecord.funding_time >= starts[30], FundingRateRecord.funding_rate), else_=0.0)),
        ).filter(
            FundingRateRecord.symbol.in_(symbols),
            FundingRateRecord.funding_time >= start_time,
            FundingRateRecord.funding_time <= end_time,
        ).group_by(FundingRateRecord.symbol).all()
        aggregate_by_symbol = {row[0]: row[1:] for row in aggregates}
        latest_times = db.session.query(
            FundingRateRecord.symbol.label("symbol"),
            func.max(FundingRateRecord.funding_time).label("funding_time"),
        ).filter(
            FundingRateRecord.symbol.in_(symbols),
            FundingRateRecord.funding_time <= end_time,
        ).group_by(FundingRateRecord.symbol).subquery()
        previous_by_symbol = dict(db.session.query(
            FundingRateRecord.symbol,
            FundingRateRecord.funding_rate,
        ).join(
            latest_times,
            (FundingRateRecord.symbol == latest_times.c.symbol)
            & (FundingRateRecord.funding_time == latest_times.c.funding_time),
        ).all())
        output = {}
        for symbol in symbols:
            totals = aggregate_by_symbol.get(symbol)
            output[symbol] = {
                "previous": previous_by_symbol.get(symbol),
                "day_1": totals[0] if totals else None,
                "day_3": totals[1] if totals else None,
                "day_7": totals[2] if totals else None,
                "day_30": totals[3] if totals else None,
            }
        FUNDING_STATISTICS_CACHE.update({"ts": now_ts, "symbols": requested_symbols, "data": output})
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
    global LAST_PRICE_HISTORY_BUCKET
    now = int(time.time())
    bucket_at = now // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    # 价格趋势使用 5 分钟桶；同一桶内无需每 5 秒重复扫描、清理并提交百万行历史表。
    if LAST_PRICE_HISTORY_BUCKET == bucket_at:
        return
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
    LAST_PRICE_HISTORY_BUCKET = bucket_at


def trend_candidate_buckets(target_buckets, lag_buckets=2):
    buckets = set()
    for bucket_at in target_buckets:
        for offset in range(lag_buckets + 1):
            buckets.add(bucket_at - offset * PRICE_HISTORY_BUCKET_SECONDS)
    return buckets


def nearest_trend_points(history, key_builder, target_buckets, lag_buckets=2):
    max_lag = lag_buckets * PRICE_HISTORY_BUCKET_SECONDS
    points = {}
    for item in history:
        base_key = key_builder(item)
        for target_bucket in target_buckets:
            lag = target_bucket - item.bucket_at
            if 0 <= lag <= max_lag:
                key = (*base_key, target_bucket)
                previous = points.get(key)
                if previous is None or item.bucket_at > previous[0]:
                    points[key] = (item.bucket_at, item.price)
    return {key: value for key, (_bucket_at, value) in points.items()}


def enrich_price_changes(groups):
    if not groups:
        return
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    target_buckets = [now_bucket - seconds for seconds in TREND_WINDOWS.values()]
    symbols = [group["symbol"] for group in groups]
    history = FuturesPriceHistory.query.filter(
        FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at.in_(trend_candidate_buckets(target_buckets))
    ).all()
    points = nearest_trend_points(history, lambda item: (item.symbol,), target_buckets)
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


def okx_turnover_usd(item, mark_price=None):
    """OKX swap ticker 的 volCcy24h 多数是标的币数量；页面统一展示 USDT 成交额。"""
    for key in ("volUsd24h", "volUsd"):
        try:
            value = float(item.get(key, 0) or 0)
            if value:
                return value
        except (TypeError, ValueError):
            continue
    try:
        base_volume = float(item.get("volCcy24h", 0) or item.get("vol24h", 0) or 0)
        price = float(mark_price or item.get("last", 0) or item.get("bidPx", 0) or item.get("askPx", 0) or 0)
        return base_volume * price if base_volume and price else None
    except (TypeError, ValueError):
        return None


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
                    current_funding_time = item.get("fundingTime")
                    next_funding_time = item.get("nextFundingTime")
                    OKX_FUNDING_CACHE[inst_id] = {
                        "funding_rate": float(item.get("fundingRate", 0)) * 100,
                        "funding_interval_hours": max(1, round((int(next_funding_time or 0) - int(current_funding_time or 0)) / 3_600_000)) if next_funding_time and current_funding_time else 8,
                        # OKX fundingTime 是下一次实际结算，nextFundingTime 是再下一期；页面倒计时必须看 fundingTime。
                        "next_funding_time": format_funding_time(current_funding_time or next_funding_time),
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
        DualFuturesPriceHistory.symbol.in_(symbols), DualFuturesPriceHistory.bucket_at.in_(trend_candidate_buckets(target_buckets))
    ).all()
    points = nearest_trend_points(history, lambda item: (item.symbol, item.exchange), target_buckets)
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
        FuturesPriceHistory.symbol.in_(symbols), FuturesPriceHistory.bucket_at.in_(trend_candidate_buckets(target_buckets))
    ).all()
    points = nearest_trend_points(history, lambda item: (item.symbol,), target_buckets)
    for group in groups:
        reference = references.get(group["symbol"], {})
        binance_row = next((row for row in group["rows"] if row["short_exchange"] == "Binance"), None)
        current_price = reference.get("price") or ((binance_row["short_ask"] + binance_row["short_bid"]) / 2 if binance_row else None)
        group["binance_basis"] = reference.get("basis") if reference else (binance_row.get("short_basis") if binance_row else None)
        for key, seconds in TREND_WINDOWS.items():
            previous = points.get((group["symbol"], now_bucket - seconds))
            group[f"binance_{key}"] = (current_price - previous) / previous * 100 if current_price and previous else None


def load_latest_dual_futures_snapshot():
    if DUAL_FUTURES_CACHE["snapshot"]:
        return DUAL_FUTURES_CACHE["snapshot"]
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
    recent_window = datetime.now() - timedelta(minutes=30)
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
    if recent and recent.created_at >= recent_window and current_abs < recent_abs + 0.2:
        return
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
        # Rapid moves are useful only when the abnormal value survives multiple
        # refreshes. Two samples can still be a bad quote that disappears before
        # the user can trade it, so rapid alerts require three consecutive active
        # observations after the 30s baseline check.
        if active and RAPID_MOVE_CANDIDATES[candidate_key] >= 3:
            window_key = (key, metric)
            window = RAPID_MOVE_ALERT_WINDOWS.get(window_key)
            current_abs = abs(value)
            expired = not window or now - window["started_at"] >= 30 * 60
            widened = bool(window and current_abs >= window["max_abs"] + 0.2)
            if expired or widened:
                RAPID_MOVE_ALERT_WINDOWS[window_key] = {"started_at": now, "max_abs": current_abs}
                triggered.append((seconds, threshold, expanded))
    return triggered


def dual_spread_quote_supported(row):
    open_spread = float(row.get("open_spread") or 0)
    if abs(open_spread) < 0.5:
        return True
    long_basis = row.get("long_basis")
    short_basis = row.get("short_basis")
    if long_basis is None or short_basis is None:
        return abs(open_spread) >= 1.5
    long_basis = float(long_basis or 0)
    short_basis = float(short_basis or 0)
    basis_gap = short_basis - long_basis
    basis_support = abs(long_basis) >= 0.35 or abs(short_basis) >= 0.35 or abs(basis_gap) >= 0.25
    long_funding = float(row.get("long_funding_rate") or 0)
    short_funding = float(row.get("short_funding_rate") or 0)
    funding_support = abs(long_funding - short_funding) >= 0.02 or any(abs(abs(value) - 0.005) > 0.003 for value in (long_funding, short_funding))
    if abs(open_spread - basis_gap) <= 0.35:
        return True
    if basis_support and abs(open_spread - basis_gap) <= 0.8:
        return True
    # If both contract bases are flat and funding is just low-insurance level,
    # a sudden large top-of-book spread is more likely a bad quote than a tradable
    # futures/futures arbitrage signal.
    return basis_support or funding_support


def validate_dual_alert_quote(row, alert_type):
    if "spread" in alert_type and not dual_spread_quote_supported(row):
        return False
    if "basis" in alert_type:
        bn_basis = row.get("basis")
        if bn_basis is None:
            return False
        if abs(float(bn_basis or 0)) < 0.5:
            return False
    return True


def create_basis_open_alert(symbol, row, strategy, message):
    if strategy == "spot_futures":
        create_alert(symbol, "basis_threshold", message, row, strategy, row.get("long_exchange"), "Binance")
    else:
        create_alert(symbol, "dual_basis_threshold", message, row, strategy, row.get("long_exchange"), row.get("short_exchange"))


def track_basis(symbol, row, active_by_symbol, active_by_key, strategy="spot_futures", emit_alert=True):
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
        if emit_alert:
            create_basis_open_alert(symbol, row, strategy, "basis reopened above 1 percent")
        if False and strategy == "spot_futures":
            create_alert(symbol, "basis_threshold", "基差连续两次越过 ±1% 阈值", row)
    if absolute > active.max_abs_basis:
        active.max_abs_basis, active.max_basis, active.max_at = absolute, basis, now
    expanded = False
    while absolute >= round(active.last_recorded_level + 0.2, 1):
        active.last_recorded_level = round(active.last_recorded_level + 0.2, 1)
        db.session.add(BasisExpansionLog(tracking_id=active.id, level=active.last_recorded_level, observed_basis=basis))
        expanded = True
    if expanded and emit_alert:
        create_basis_open_alert(symbol, row, strategy, f"basis expanded to {active.last_recorded_level:.1f} percent level")


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
            for metric_key, metric_label, value in (("spread", "开差", float(row.get("open_spread") or 0)), ("basis", "BN 基差", float(bn_basis or 0))):
                if metric_key == "basis" and bn_basis is None:
                    continue
                alert_type = f"rapid_{metric_key}"
                for seconds, threshold, expanded in rapid_move_alerts(("futures_futures", *path), metric_key, value):
                    if validate_dual_alert_quote(alert_row, alert_type):
                        create_alert(symbol, alert_type, f"{row['long_exchange']} 合约与 {row['short_exchange']} 合约：{seconds} 秒内{metric_label}绝对值扩大 {expanded:.3f}%（阈值 {threshold:.1f}%）", alert_row, "futures_futures", row["long_exchange"], row["short_exchange"])
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
                # The same path and threshold need to survive three consecutive snapshots.
                stable = previous is not None and previous * observed > 0 and abs(previous - observed) <= 0.6
                if DUAL_ALERT_CANDIDATES[key] < 3 or not stable:
                    continue
                message = f"{row['long_exchange']} 合约与 {row['short_exchange']} 合约出现确认后的{label}异动"
                if validate_dual_alert_quote(alert_row, alert_type):
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
    for compact_symbol, exchange in SPOT_CONTRACT_SYMBOL_MISMATCHES:
        LatestMarketSnapshot.query.filter_by(symbol=pair_slash(compact_symbol), long_exchange=exchange).delete(synchronize_session=False)
    db.session.commit()


def load_latest_market_snapshot():
    if SPOT_FUTURES_CACHE["snapshot"]:
        return SPOT_FUTURES_CACHE["snapshot"]
    rows = LatestMarketSnapshot.query.order_by(LatestMarketSnapshot.symbol, LatestMarketSnapshot.long_exchange).all()
    if not rows:
        return None
    groups = {}
    captured_at = max(item.captured_at for item in rows)
    for item in rows:
        if is_spot_contract_symbol_mismatch(item.symbol, item.long_exchange):
            continue
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
        alias = contract_spot_alias(symbol)
        spot_symbol = alias["spot_symbol"]
        multiplier = alias["multiplier"]
        rows = []
        for exchange in ("Binance", "Gate", "Bitget"):
            if is_spot_contract_symbol_mismatch(symbol, exchange):
                continue
            raw_book = spot_books[exchange].get(spot_symbol)
            book = {"ask": raw_book["ask"] * multiplier, "bid": raw_book["bid"] * multiplier} if raw_book and multiplier != 1 else raw_book
            if not book:
                continue
            open_spread = (futures_book["bid"] - book["ask"]) / book["ask"] * 100
            close_spread = (futures_book["ask"] - book["bid"]) / book["bid"] * 100
            contract_basis = (float(funding_item["markPrice"]) - float(funding_item["indexPrice"])) / float(funding_item["indexPrice"]) * 100
            oi_contracts = BINANCE_OPEN_INTEREST_CACHE.get(symbol, {}).get("contracts")
            rows.append({"long_exchange": exchange, "long_ask": book["ask"], "long_bid": book["bid"], "short_exchange": "Binance 合约", "short_bid": futures_book["bid"], "short_ask": futures_book["ask"], "basis": contract_basis, "funding_rate": float(funding_item["lastFundingRate"]) * 100, "funding_interval_hours": intervals.get(symbol, 8), "next_funding_time": datetime.fromtimestamp(int(funding_item["nextFundingTime"]) / 1000, tz=timezone.utc).astimezone().strftime("%m-%d %H:%M"), "spot_volume": spot_volumes[exchange].get(spot_symbol), "futures_volume": futures_volumes.get(symbol), "futures_open_interest": oi_contracts * float(funding_item.get("markPrice", 0) or 0) if oi_contracts is not None else None, "open_spread": open_spread, "close_spread": close_spread})
        if rows:
            rows_by_symbol.append({"symbol": f"{symbol[:-4]}/USDT", "rows": rows})
    rows_finished_at = time.perf_counter()
    evaluate_alerts(rows_by_symbol)
    alerts_finished_at = time.perf_counter()
    captured_at = datetime.now()
    live_snapshot = {
        "symbols": rows_by_symbol,
        "errors": errors,
        "updated_at": captured_at.strftime("%H:%M:%S"),
        "next_refresh_in_seconds": MARKET_REFRESH_SECONDS,
        "stored": True,
    }
    SPOT_FUTURES_CACHE["snapshot"] = live_snapshot
    persist_due = time.time() - LAST_MARKET_DB_PERSIST_AT["spot"] >= MARKET_DB_PERSIST_SECONDS
    if persist_due:
        save_latest_market_snapshot(rows_by_symbol)
        LAST_MARKET_DB_PERSIST_AT["spot"] = time.time()
    snapshot_finished_at = time.perf_counter()
    if persist_due:
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
    return live_snapshot


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
        mark_price = okx_marks.get(inst_id)
        okx[symbol] = {**book, "mark": mark_price, "index": okx_indexes.get(inst_id.replace("-SWAP", "").replace("-", "")), "volume": okx_turnover_usd(item, mark_price), "open_interest": okx_open_interest.get(inst_id), "funding_rate": cached_funding.get("funding_rate"), "funding_interval_hours": cached_funding.get("funding_interval_hours"), "next_funding_time": cached_funding.get("next_funding_time"), "okx_inst_id": inst_id}
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
    captured_at = datetime.now()
    live_snapshot = {
        "symbols": groups,
        "errors": errors,
        "updated_at": captured_at.strftime("%H:%M:%S"),
        "next_refresh_in_seconds": MARKET_REFRESH_SECONDS,
        "stored": True,
    }
    DUAL_FUTURES_CACHE["snapshot"] = live_snapshot
    if time.time() - LAST_MARKET_DB_PERSIST_AT["dual"] >= MARKET_DB_PERSIST_SECONDS:
        save_latest_dual_futures_snapshot(groups)
        LAST_MARKET_DB_PERSIST_AT["dual"] = time.time()
    return live_snapshot


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


@app.get("/daily-trends")
def daily_trends_page():
    return redirect("/#daily-trends")


MAJOR_MARKET_CACHE = {"ts": 0, "items": []}
MAJOR_MARKET_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DASHBOARD_OPPORTUNITY_CACHE = {"ts": 0, "items": []}


def major_market_window_metrics(klines, oi_rows, ratio_rows, candle_count):
    closed = klines[:-1] if len(klines) > candle_count else klines
    rows = closed[-candle_count:]
    oi = oi_rows[-candle_count:] if oi_rows else []
    ratios = ratio_rows[-candle_count:] if ratio_rows else []
    if len(rows) < candle_count:
        return {"price_change": None, "oi_change": None, "ratio_change": None, "cvd": None}
    price_change = percent_delta(float(rows[-1][4]), float(rows[0][1]))
    cvd_value = sum((2 * float(row[10]) - float(row[7])) for row in rows)
    oi_change = None
    ratio_change = None
    if len(oi) >= candle_count:
        oi_change = percent_delta(float(oi[-1].get("sumOpenInterestValue", 0) or 0), float(oi[0].get("sumOpenInterestValue", 0) or 0))
    if len(ratios) >= candle_count:
        ratio_change = percent_delta(float(ratios[-1].get("longShortRatio", 0) or 0), float(ratios[0].get("longShortRatio", 0) or 0))
    return {"price_change": price_change, "oi_change": oi_change, "ratio_change": ratio_change, "cvd": cvd_value}


def major_market_forecast(metrics):
    windows = metrics.get("windows", {})
    w30 = windows.get("30m", {})
    w4h = windows.get("4h", {})
    funding = metrics.get("funding_rate")
    basis = metrics.get("basis")
    score = 0
    reasons = []
    if (w30.get("price_change") or 0) > 0 and (w30.get("cvd") or 0) > 0:
        score += 1
        reasons.append("短线价格与主动买入同步")
    if (w4h.get("price_change") or 0) > 0 and (w4h.get("cvd") or 0) > 0:
        score += 1
        reasons.append("4H 结构仍偏多")
    if (w30.get("oi_change") or 0) > 0 and (w30.get("price_change") or 0) > 0:
        score += 1
        reasons.append("上涨时持仓增加")
    if funding is not None and funding >= 0 and basis is not None and basis >= 0:
        score += 1
        reasons.append("资费与基差未明显转弱")
    if (w30.get("price_change") or 0) < 0 and (w30.get("cvd") or 0) < 0:
        score -= 1
        reasons.append("短线主动卖出压制价格")
    if (w4h.get("price_change") or 0) < 0 and (w4h.get("cvd") or 0) < 0:
        score -= 1
        reasons.append("4H 结构偏弱")
    if (w30.get("oi_change") or 0) < 0 and (w30.get("price_change") or 0) < 0:
        score -= 1
        reasons.append("下跌中持仓回落")
    if funding is not None and funding < 0 and basis is not None and basis < 0:
        score -= 1
        reasons.append("资费与基差同时偏负")

    if score >= 2:
        stance = "偏强"
        forecast = "短线仍偏多，回调只要不放量跌破近端支撑，大盘风险偏好还能维持。"
    elif score <= -2:
        stance = "偏弱"
        forecast = "短线偏弱，若 BTC 继续压制，山寨追多风险会明显变高。"
    else:
        stance = "震荡"
        forecast = "多空证据不够一致，先按震荡处理，等待 BTC 选择方向。"
    return {"stance": stance, "score": score, "forecast": forecast, "reason": "；".join(reasons[:3]) or "等待更多共振"}


def fetch_major_market_symbol(raw_symbol):
    ticker = get_json("https://fapi.binance.com/fapi/v1/ticker/24hr?" + urlencode({"symbol": raw_symbol}), timeout=4)
    premium = get_json("https://fapi.binance.com/fapi/v1/premiumIndex?" + urlencode({"symbol": raw_symbol}), timeout=4)
    klines = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "30m", "limit": 60}), timeout=4)
    oi_rows = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": 50}), timeout=4)
    ratio_rows = get_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": 50}), timeout=4)
    index_price = float(premium.get("indexPrice", 0) or 0)
    mark_price = float(premium.get("markPrice", 0) or 0)
    metrics = {
        "symbol": raw_symbol[:-4] + "/USDT",
        "price": float(ticker.get("lastPrice", 0) or 0),
        "change_24h": float(ticker.get("priceChangePercent", 0) or 0),
        "volume_24h": float(ticker.get("quoteVolume", 0) or 0),
        "funding_rate": float(premium.get("lastFundingRate", 0) or 0) * 100,
        "basis": percent_delta(mark_price, index_price) if index_price else None,
        "open_interest": float(oi_rows[-1].get("sumOpenInterestValue", 0) or 0) if oi_rows else None,
        "long_short_ratio": float(ratio_rows[-1].get("longShortRatio", 0) or 0) if ratio_rows else None,
        "windows": {
            "30m": major_market_window_metrics(klines, oi_rows, ratio_rows, 1),
            "1h": major_market_window_metrics(klines, oi_rows, ratio_rows, 2),
            "4h": major_market_window_metrics(klines, oi_rows, ratio_rows, 8),
        },
        "updated_at": datetime.now(SHANGHAI_TZ).strftime("%H:%M:%S"),
    }
    metrics["forecast"] = major_market_forecast(metrics)
    return metrics


def fetch_major_market_overview():
    now_ts = time.time()
    if MAJOR_MARKET_CACHE["items"] and now_ts - MAJOR_MARKET_CACHE["ts"] < 15:
        return MAJOR_MARKET_CACHE["items"]
    items = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_major_market_symbol, raw_symbol): raw_symbol for raw_symbol in MAJOR_MARKET_SYMBOLS}
        for future in as_completed(futures):
            try:
                items.append(future.result())
            except Exception:
                continue
    order = {symbol[:-4] + "/USDT": idx for idx, symbol in enumerate(MAJOR_MARKET_SYMBOLS)}
    items.sort(key=lambda item: order.get(item["symbol"], 99))
    if items:
        btc = next((item for item in items if item["symbol"] == "BTC/USDT"), None)
        for item in items:
            if btc and item["symbol"] != "BTC/USDT":
                item["market_note"] = "强于 BTC" if (item.get("change_24h") or 0) > (btc.get("change_24h") or 0) else "弱于 BTC"
            else:
                item["market_note"] = "大盘锚点"
        MAJOR_MARKET_CACHE.update({"ts": now_ts, "items": items})
    return MAJOR_MARKET_CACHE["items"]


def fetch_dashboard_symbol_quotes(raw_symbol):
    base = raw_symbol[:-4]
    urls = {
        "Binance": "https://fapi.binance.com/fapi/v1/ticker/24hr?" + urlencode({"symbol": raw_symbol}),
        "OKX": "https://www.okx.com/api/v5/market/ticker?" + urlencode({"instId": f"{base}-USDT-SWAP"}),
        "Bybit": "https://api.bybit.com/v5/market/tickers?" + urlencode({"category": "linear", "symbol": raw_symbol}),
    }
    quotes = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(get_json, url, 4): exchange for exchange, url in urls.items()}
        for future in as_completed(futures):
            exchange = futures[future]
            try:
                payload = future.result()
                if exchange == "Binance":
                    price = float(payload.get("lastPrice", 0) or 0)
                elif exchange == "OKX":
                    rows = payload.get("data") or []
                    price = float((rows[0] if rows else {}).get("last", 0) or 0)
                else:
                    rows = ((payload.get("result") or {}).get("list") or [])
                    price = float((rows[0] if rows else {}).get("lastPrice", 0) or 0)
                if price > 0:
                    quotes[exchange] = price
            except Exception:
                continue
    return raw_symbol[:-4] + "/USDT", quotes


def live_dashboard_opportunities():
    now_ts = time.time()
    if DASHBOARD_OPPORTUNITY_CACHE["items"] and now_ts - DASHBOARD_OPPORTUNITY_CACHE["ts"] < 15:
        return DASHBOARD_OPPORTUNITY_CACHE["items"]
    rows = []
    major_by_symbol = {item["symbol"]: item for item in fetch_major_market_overview()}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(fetch_dashboard_symbol_quotes, symbol) for symbol in MAJOR_MARKET_SYMBOLS]
        for future in as_completed(futures):
            try:
                symbol, quotes = future.result()
            except Exception:
                continue
            if not quotes:
                major = major_by_symbol.get(symbol)
                if major and major.get("price"):
                    quotes = {"Binance": major["price"]}
            if not quotes:
                continue
            buy_exchange = min(quotes, key=quotes.get)
            sell_exchange = max(quotes, key=quotes.get)
            buy, sell = quotes[buy_exchange], quotes[sell_exchange]
            spread = (sell / buy - 1) * 100 if buy else 0
            major = major_by_symbol.get(symbol) or {}
            rows.append({
                "symbol": symbol,
                "buy_exchange": buy_exchange,
                "sell_exchange": sell_exchange,
                "buy_price": round(buy, 8),
                "sell_price": round(sell, 8),
                "exchange_prices": {
                    "Binance": round(quotes["Binance"], 8) if "Binance" in quotes else None,
                    "OKX": round(quotes["OKX"], 8) if "OKX" in quotes else None,
                    "Bybit": round(quotes["Bybit"], 8) if "Bybit" in quotes else None,
                },
                "spread": round(spread, 4),
                "estimated_profit": round(spread - 0.12, 4),
                "funding": major.get("funding_rate") if major.get("funding_rate") is not None else 0,
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            })
    rows.sort(key=lambda item: item["estimated_profit"], reverse=True)
    if rows:
        DASHBOARD_OPPORTUNITY_CACHE.update({"ts": now_ts, "items": rows})
    return DASHBOARD_OPPORTUNITY_CACHE["items"]


@app.get("/api/dashboard")
def dashboard():
    items = live_dashboard_opportunities()
    active = Strategy.query.filter_by(enabled=True).count()
    return jsonify({
        "majors": fetch_major_market_overview(),
        "opportunities": items,
        "summary": {
            "active_strategies": active,
            "best_spread": items[0]["spread"] if items else 0,
            "markets_scanned": len(items) * len(EXCHANGES),
            "mode": "机会看板 · 三所合约公开 API",
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
        symbols = [
            group for group in symbols
            if symbol_matches_query(group["symbol"], raw_symbol_query)
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
    include_bybit_okx = request.args.get("include_bybit_okx", "0") in {"1", "true", "TRUE", "yes", "on"}
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
    if not include_bybit_okx:
        filtered_symbols = []
        for group in symbols:
            rows = [
                row for row in group["rows"]
                if not (row.get("long_exchange") == "Bybit" and row.get("short_exchange") == "OKX")
            ]
            if rows:
                filtered_symbols.append({**group, "rows": rows})
        symbols = filtered_symbols
    if symbol_query:
        symbols = [
            group for group in symbols
            if symbol_matches_query(group["symbol"], raw_symbol_query)
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
    return jsonify({**snapshot, "page": page, "pages": pages, "page_size": page_size, "total_symbols": total, "symbol_query": symbol_query, "sort_by": sort_by, "sort_direction": sort_direction, "include_bybit_okx": include_bybit_okx, "symbols": symbols[start:start + page_size]})


@app.get("/api/symbol-suggestions")
def symbol_suggestions():
    query = "".join(request.args.get("q", "").strip().upper().split())
    if not query:
        return jsonify({"items": []})
    compact_query = query.replace("/", "").replace("-", "")
    live_pairs = sorted({
        item.symbol.upper()
        for item in LatestMarketSnapshot.query.with_entities(LatestMarketSnapshot.symbol).all()
        if not is_rwa_stock_pair(item.symbol)
    } | {
        item.symbol.upper()
        for item in LatestDualFuturesSnapshot.query.with_entities(LatestDualFuturesSnapshot.symbol).all()
        if not is_rwa_stock_pair(item.symbol)
    })
    live_compacts = {compact_pair(item) for item in live_pairs}
    pairs = set(live_pairs)
    for base in COIN_ALIASES:
        pairs.add(f"{base}/USDT")
    for row in symbol_alias_rows():
        pairs.add(row.canonical_symbol)
        pairs.add(row.alias_symbol)

    matches = []
    for pair in pairs:
        base = pair.split("/", 1)[0]
        chinese_name = COIN_ALIASES.get(base, "")
        alias_candidates = symbol_alias_candidates(pair)
        searchable = f"{base}{pair.replace('/', '')}{chinese_name}{''.join(sorted(alias_candidates))}".upper()
        if compact_query not in searchable:
            continue
        live = pair in live_pairs or bool(alias_candidates & live_compacts)
        label = f"{chinese_name} · {pair}" if chinese_name else pair
        starts_with = any(candidate.startswith(compact_query) for candidate in alias_candidates) or chinese_name.startswith(query)
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


def percentile(values, q):
    cleaned = sorted(float(value) for value in values if value is not None)
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return cleaned[0]
    position = (len(cleaned) - 1) * q / 100
    lower = int(position)
    upper = min(lower + 1, len(cleaned) - 1)
    weight = position - lower
    return cleaned[lower] * (1 - weight) + cleaned[upper] * weight


def fetch_t_micro_metrics(raw_symbol):
    """T/USDT 专用 5 分钟级盯盘：捕捉横盘后的短线向上异动与反抽停滞。"""
    try:
        live_timeout = 5 if raw_symbol == "TLMUSDT" else 2
        k5 = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "5m", "limit": 48}), timeout=live_timeout)
        oi5 = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": "5m", "limit": 48}), timeout=live_timeout)
        ratios5 = get_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?" + urlencode({"symbol": raw_symbol, "period": "5m", "limit": 48}), timeout=live_timeout)
    except Exception:
        return {}

    def micro_window(candles):
        rows = k5[-candles:]
        oi_rows = oi5[-candles:] if isinstance(oi5, list) else []
        ratio_rows = ratios5[-candles:] if isinstance(ratios5, list) else []
        if len(rows) < candles:
            return {}
        price_change = percent_delta(float(rows[-1][4]), float(rows[0][1]))
        quote_volume = sum(float(row[7]) for row in rows)
        prior_rows = k5[-(candles + 12):-candles] if len(k5) >= candles + 12 else []
        prior_volume = sum(float(row[7]) for row in prior_rows) / len(prior_rows) * candles if prior_rows else None
        volume_ratio = quote_volume / prior_volume if prior_volume else None
        cvd = sum((2 * float(row[10]) - float(row[7])) for row in rows)
        oi_change = None
        oi_value = None
        if len(oi_rows) >= candles:
            oi_start = float(oi_rows[0].get("sumOpenInterest", 0) or 0)
            oi_end = float(oi_rows[-1].get("sumOpenInterest", 0) or 0)
            oi_change = percent_delta(oi_end, oi_start)
            oi_value = float(oi_rows[-1].get("sumOpenInterestValue", 0) or 0)
        ratio_change = None
        ratio_value = None
        if len(ratio_rows) >= candles:
            ratio_start = float(ratio_rows[0].get("longShortRatio", 0) or 0)
            ratio_end = float(ratio_rows[-1].get("longShortRatio", 0) or 0)
            ratio_change = percent_delta(ratio_end, ratio_start)
            ratio_value = ratio_end
        high = max(float(row[2]) for row in rows)
        low = min(float(row[3]) for row in rows)
        return {
            "price_change": price_change,
            "volume": quote_volume,
            "volume_ratio": volume_ratio,
            "cvd": cvd,
            "oi_change": oi_change,
            "oi_value": oi_value,
            "ratio_change": ratio_change,
            "ratio_value": ratio_value,
            "high": high,
            "low": low,
        }

    recent_12 = k5[-12:] if len(k5) >= 12 else k5
    recent_high = max(float(row[2]) for row in recent_12) if recent_12 else None
    recent_low = min(float(row[3]) for row in recent_12) if recent_12 else None
    current = float(k5[-1][4]) if k5 else None
    return {
        "5m": micro_window(1),
        "15m": micro_window(3),
        "30m": micro_window(6),
        "current": current,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "range_mid": ((recent_high + recent_low) / 2) if recent_high and recent_low else None,
    }


BINANCE_FUTURES_DEPTH_MAX_LIMIT = 1000
BINANCE_SPOT_DEPTH_MAX_LIMIT = 5000


def fetch_ake_orderbook_wall(raw_symbol):
    """AKE 上方 0.0020-0.0023 卖墙监控。挂单只作为盘口意图线索，不直接等同真实空单。"""
    reference_buckets = [
        {"level": 0.0020, "upper": 0.0021, "qty": 1_100_000, "notional": 0.0020 * 1_100_000},
        {"level": 0.0021, "upper": 0.0022, "qty": 1_200_000, "notional": 0.0021 * 1_200_000},
        {"level": 0.0022, "upper": 0.0023, "qty": 1_000_000, "notional": 0.0022 * 1_000_000},
    ]
    try:
        live_timeout = 2
        depth = get_json("https://fapi.binance.com/fapi/v1/depth?" + urlencode({"symbol": raw_symbol, "limit": BINANCE_FUTURES_DEPTH_MAX_LIMIT}), timeout=live_timeout)
        ticker = get_json("https://fapi.binance.com/fapi/v1/ticker/24hr?" + urlencode({"symbol": raw_symbol}), timeout=live_timeout)
    except Exception:
        return {}
    asks = [(float(price), float(qty)) for price, qty in depth.get("asks", [])]
    bids = [(float(price), float(qty)) for price, qty in depth.get("bids", [])]
    levels = [0.0020, 0.0021, 0.0022, 0.0023]
    buckets = []
    for level in levels:
        upper = level + 0.0001
        qty = sum(qty for price, qty in asks if level <= price < upper)
        notional = sum(price * qty for price, qty in asks if level <= price < upper)
        buckets.append({"level": level, "upper": upper, "qty": qty, "notional": notional})
    wall_qty = sum(item["qty"] for item in buckets)
    wall_notional = sum(item["notional"] for item in buckets)
    near_bid_qty = sum(qty for price, qty in bids if 0.0018 <= price < 0.0020)
    near_bid_notional = sum(price * qty for price, qty in bids if 0.0018 <= price < 0.0020)
    last = float(ticker.get("lastPrice", 0) or 0)
    visible_high = max([price for price, _qty in asks] or [0])
    visible_depth_covers_wall = visible_high >= 0.0023
    spot_depth = {}
    try:
        spot_depth = get_json("https://api.binance.com/api/v3/depth?" + urlencode({"symbol": raw_symbol, "limit": BINANCE_SPOT_DEPTH_MAX_LIMIT}), timeout=2)
    except Exception:
        spot_depth = {}
    spot_asks = [(float(price), float(qty)) for price, qty in spot_depth.get("asks", [])]
    spot_wall_qty = sum(qty for price, qty in spot_asks if 0.0020 <= price < 0.0023)
    spot_wall_notional = sum(price * qty for price, qty in spot_asks if 0.0020 <= price < 0.0023)
    spot_visible_high = max([price for price, _qty in spot_asks] or [0])
    return {
        "last": last,
        "buckets": buckets,
        "reference_buckets": reference_buckets,
        "futures_depth_limit": BINANCE_FUTURES_DEPTH_MAX_LIMIT,
        "spot_depth_limit": BINANCE_SPOT_DEPTH_MAX_LIMIT,
        "wall_qty": wall_qty,
        "wall_notional": wall_notional,
        "near_bid_qty": near_bid_qty,
        "near_bid_notional": near_bid_notional,
        "visible_high": visible_high,
        "visible_depth_covers_wall": visible_depth_covers_wall,
        "spot_wall_qty": spot_wall_qty,
        "spot_wall_notional": spot_wall_notional,
        "spot_visible_high": spot_visible_high,
        "wall_low": 0.0020,
        "wall_mid": 0.00215,
        "wall_high": 0.0023,
    }


def fetch_liquidation_summary(symbol, lookback_minutes=240):
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - lookback_minutes * 60 * 1000
    try:
        payload = get_json("https://fapi.binance.com/fapi/v1/allForceOrders?" + urlencode({
            "symbol": symbol.replace("/", ""),
            "startTime": start_ms,
            "endTime": now_ms,
            "limit": 1000,
        }), timeout=6)
    except Exception:
        return {"total_quote": 0.0, "long_quote": 0.0, "short_quote": 0.0, "count": 0, "available": False}
    long_quote = 0.0
    short_quote = 0.0
    for item in payload or []:
        qty = float(item.get("executedQty") or item.get("origQty") or 0)
        price = float(item.get("avgPrice") or item.get("price") or 0)
        quote = float(item.get("cumQuote") or 0) or qty * price
        if item.get("side") == "SELL":
            long_quote += quote
        elif item.get("side") == "BUY":
            short_quote += quote
    return {"total_quote": long_quote + short_quote, "long_quote": long_quote, "short_quote": short_quote, "count": len(payload or []), "available": True}


def classify_early_trend_stage(closed, oi_rows, ratio_rows):
    """识别启动前蓄势与已启动阶段；强信号按最近 5 根 30M 整体首尾涨幅判断。"""
    if len(closed) < 25 or len(oi_rows) < 5 or len(ratio_rows) < 5:
        return None
    recent = closed[-5:]
    prior = closed[-25:-5]
    recent_oi = oi_rows[-5:]
    recent_ratio = ratio_rows[-5:]
    first_open = float(recent[0][1])
    last_close = float(recent[-1][4])
    price_change = percent_delta(last_close, first_open)
    oi_change = percent_delta(
        float(recent_oi[-1].get("sumOpenInterest", 0) or 0),
        float(recent_oi[0].get("sumOpenInterest", 0) or 0),
    )
    ratio_change = percent_delta(
        float(recent_ratio[-1].get("longShortRatio", 0) or 0),
        float(recent_ratio[0].get("longShortRatio", 0) or 0),
    )
    if None in (price_change, oi_change, ratio_change):
        return None
    cvd_change = sum(2 * float(row[10]) - float(row[7]) for row in recent)
    recent_volume = sum(float(row[7]) for row in recent)
    prior_average_5 = (sum(float(row[7]) for row in prior) / len(prior) * 5) if prior else None
    volume_ratio = recent_volume / prior_average_5 if prior_average_5 else None
    prior_change = percent_delta(float(prior[-1][4]), float(prior[-10][1])) if len(prior) >= 10 else None
    prior_low = min(float(row[3]) for row in prior)
    prior_high = max(float(row[2]) for row in prior)
    prior_range = percent_delta(prior_high, prior_low)
    horn = oi_change > 0 and ratio_change < 0
    cvd_up = cvd_change > 0

    # 用户指定的强看多入口：不是逐根相加，而是第一根开盘到第五根收盘的整体涨幅。
    if price_change >= 50 and horn and cvd_up:
        latest = recent[-1]
        body_high = max(float(latest[1]), float(latest[4]))
        upper_wick = percent_delta(float(latest[2]), body_high) or 0
        oi_values = [float(row.get("sumOpenInterest", 0) or 0) for row in recent_oi]
        oi_drawdown = percent_delta(oi_values[-1], max(oi_values)) or 0
        if (volume_ratio or 0) >= 4 and (upper_wick >= 12 or oi_drawdown <= -8):
            stage_key, stage_label, stage_number = "overheated", "过热换手期 · 第4阶段", 4
            reason = "5根已强涨且末端巨量/上影或持仓回落，趋势仍强，但已进入换手与派发风险区。"
        elif (prior_change or 0) <= 15 and (prior_range or 999) <= 28:
            stage_key, stage_label, stage_number = "ignition", "启动初期 · 第1阶段点火", 1
            reason = "前20根以压缩整理为主，本轮5根是首次明显突破，属于刚从蓄势切入主升。"
        elif (prior_change or 0) <= 70:
            stage_key, stage_label, stage_number = "acceleration", "主升加速 · 第2阶段", 2
            reason = "前置K线已有一段抬升，本轮5根再次放大，属于第二段加速而非最早起涨点。"
        else:
            stage_key, stage_label, stage_number = "late_main", "主升后段 · 第3阶段或更后", 3
            reason = "本轮之前价格已经明显上涨，当前是趋势延续确认，不应再标成启动初期。"
        return {
            "signal_type": "strong_focus", "stage_key": stage_key, "stage_label": stage_label,
            "stage_number": stage_number, "stage_reason": reason, "price_change_5": price_change,
            "cvd_change_5": cvd_change, "oi_change_5": oi_change, "ratio_change_5": ratio_change,
            "prior_price_change": prior_change, "prior_range": prior_range,
            "volume_ratio": volume_ratio, "last_price": last_close,
        }

    # 真正的启动前观察：价格尚未大涨，但合约仓位与人数结构已经先形成犄角，CVD 开始积累。
    if (
        -5 <= price_change <= 15 and oi_change >= 5 and ratio_change <= -5 and cvd_up
        and (prior_range or 999) <= 25 and (volume_ratio or 0) >= 1.15
    ):
        return {
            "signal_type": "prelaunch", "stage_key": "prelaunch",
            "stage_label": "启动前蓄势 · 第0阶段", "stage_number": 0,
            "stage_reason": "价格仍在压缩区，持仓先增、人数比先降、CVD先累积；这是前置观察，不等于已经确认拉升。",
            "price_change_5": price_change, "cvd_change_5": cvd_change,
            "oi_change_5": oi_change, "ratio_change_5": ratio_change,
            "prior_price_change": prior_change, "prior_range": prior_range,
            "volume_ratio": volume_ratio, "last_price": last_close,
        }
    return None


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
        oi_amounts = [float(row.get("sumOpenInterest", 0) or 0) for row in oi]
        oi_value = float(oi[-1].get("sumOpenInterestValue", 0) or 0)
        ratio_value = float(ratios[-1].get("longShortRatio", 0) or 0)
        oi_change = percent_delta(oi_amounts[-1], oi_amounts[0])
        ratio_change = percent_delta(ratio_value, float(ratios[0].get("longShortRatio", 0) or 0))
        price_change = percent_delta(float(closed[-1][4]), float(closed[0][4]))
        cvd_change = sum((2 * float(row[10]) - float(row[7])) for row in closed)
        if None in (oi_change, ratio_change, price_change):
            return None
        price_score = max(0, min(price_change / 30, 1)) * 8
        price_structure_score = directional_consistency([row[4] for row in closed], "up") * 6
        oi_score = max(0, min(oi_change / 25, 1)) * 28
        oi_structure_score = directional_consistency(oi_amounts, "up") * 18
        ratio_score = max(0, min(abs(ratio_change) / 22, 1)) * 24 if ratio_change < 0 else 0
        ratio_structure_score = directional_consistency([row.get("longShortRatio", 0) for row in ratios], "down") * 10
        cvd_score = 6 if cvd_change > 0 else 0
        score = price_score + price_structure_score + oi_score + oi_structure_score + ratio_score + ratio_structure_score + cvd_score
        result = {"symbol": symbol, "timeframe": timeframe, "price_change": price_change, "oi_change": oi_change, "oi_value": oi_value, "ratio_change": ratio_change, "ratio_value": ratio_value, "cvd_change": cvd_change, "cvd_confirmed": cvd_change > 0, "score": round(score, 1)}
        if timeframe == "30m":
            result["early_trend"] = classify_early_trend_stage(closed, oi, ratios)
        return result
    except Exception:
        return None


def fetch_horn_continuation_metrics(symbol):
    raw_symbol = symbol.replace("/", "")
    try:
        window = 150
        oi = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": window}), timeout=6)
        ratios = get_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": window}), timeout=6)
        klines = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "30m", "limit": window + 1}), timeout=6)
        closed = klines[:-1]
        if len(oi) < 24 or len(ratios) < 24 or len(closed) < 24:
            return None
        oi_values = [float(row.get("sumOpenInterest", 0) or 0) for row in oi]
        current_oi_value = float(oi[-1].get("sumOpenInterestValue", 0) or 0)
        ratio_values = [float(row.get("longShortRatio", 0) or 0) for row in ratios]
        current_oi = oi_values[-1]
        start_oi = max(oi_values[0], 1e-9)
        peak_index = max(range(len(oi_values)), key=lambda index: oi_values[index])
        peak_oi = oi_values[peak_index] or current_oi
        baseline = percentile(oi_values[:30], 50) or start_oi
        impulse_start = 0
        for index, value in enumerate(oi_values[:peak_index + 1]):
            if value >= baseline * 1.25:
                impulse_start = index
                break
        structure_start = max(impulse_start, peak_index - 60)
        structure_values = oi_values[structure_start:-3] if len(oi_values[structure_start:-3]) >= 12 else oi_values[impulse_start:-3]
        if len(structure_values) < 12:
            return None
        structure_bottom = percentile(structure_values, 20)
        structure_hard_floor = percentile(structure_values, 10)
        structure_mid = (structure_bottom + peak_oi) / 2 if structure_bottom else None
        if not structure_bottom:
            return None
        ratio_structure_values = ratio_values[structure_start:-3] if len(ratio_values[structure_start:-3]) >= 12 else ratio_values[impulse_start:-3]
        if len(ratio_structure_values) < 12:
            return None
        ratio_bottom = percentile(ratio_structure_values, 20)
        ratio_top = percentile(ratio_structure_values, 80)
        ratio_mid = (ratio_bottom + ratio_top) / 2 if ratio_bottom is not None and ratio_top is not None else None
        ratio_value = ratio_values[-1]
        if ratio_top is None or ratio_mid is None:
            return None
        retention = current_oi / peak_oi if peak_oi else 0
        oi_multiple = current_oi / start_oi if start_oi else 0
        oi_change = percent_delta(current_oi, start_oi)
        ratio_change = percent_delta(ratio_value, ratio_values[0])
        price_change = percent_delta(float(closed[-1][4]), float(closed[0][4]))
        cvd_change = sum((2 * float(row[10]) - float(row[7])) for row in closed)
        if None in (oi_change, ratio_change, price_change):
            return None
        oi_floor_broken = (
            structure_hard_floor is not None
            and current_oi < structure_hard_floor
            and current_oi < start_oi
        )
        ratio_low_zone = ratio_value <= ratio_mid or ratio_value <= 0.75
        ratio_compressed = ratio_low_zone and (ratio_change < 0 or cvd_change > 0)
        oi_floor_supported = (
            oi_floor_broken
            and retention >= 0.68
            and price_change > 20
            and cvd_change > 0
            and ratio_compressed
        )
        mature_oi_compression_supported = (
            oi_floor_broken
            and retention >= 0.85
            and cvd_change > 0
            and ratio_compressed
            and (price_change > -8 or directional_consistency([row[4] for row in closed[-50:]], "up") >= 0.38)
        )
        if oi_floor_broken and not (oi_floor_supported or mature_oi_compression_supported):
            return None
        price_score = max(0, min(price_change / 80, 1)) * 8
        oi_multiple_score = max(0, min((oi_multiple - 1) / 0.7, 1)) * 26
        retention_score = max(0, min((retention - 0.45) / 0.35, 1)) * 22
        ratio_level_score = max(0, min((1.15 - ratio_value) / 0.75, 1)) * 12
        ratio_change_score = max(0, min(abs(ratio_change) / 45, 1)) * 18 if ratio_change < 0 else 0
        price_structure_score = directional_consistency([row[4] for row in closed[-50:]], "up") * 5
        cvd_score = 9 if cvd_change > 0 else 0
        score = price_score + oi_multiple_score + retention_score + ratio_level_score + ratio_change_score + price_structure_score + cvd_score
        if current_oi < structure_bottom:
            score = min(score, 72)
        if structure_mid and current_oi < structure_mid:
            score = min(score, 68)
        if ratio_value > ratio_mid:
            score = min(score, 68)
        if oi_floor_broken:
            score = min(score, 58)
        if mature_oi_compression_supported:
            score = min(score, 52)
        oi_structure_alive = oi_multiple >= 1.08 or oi_change >= 8 or retention >= 0.62
        ratio_structure_alive = (
            (ratio_change < -10 and (ratio_value <= ratio_top or ratio_change < -25))
            or ratio_compressed
        )
        price_not_broken = price_change > -18 or directional_consistency([row[4] for row in closed[-50:]], "up") >= 0.42
        if not (price_not_broken and oi_structure_alive and retention >= 0.5 and ratio_structure_alive and score >= 42):
            return None
        return {
            "symbol": symbol,
            "timeframe": "continue",
            "price_change": price_change,
            "oi_change": oi_change,
            "oi_value": current_oi_value,
            "ratio_change": ratio_change,
            "ratio_value": ratio_value,
            "cvd_change": cvd_change,
            "cvd_confirmed": cvd_change > 0,
            "score": round(score, 1),
        }
    except Exception:
        return None


def scan_daily_horn_signals():
    """Run once each morning: startup horn + continuation horn; CVD is a confirmation label."""
    snapshot = load_latest_market_snapshot()
    if not snapshot:
        return 0
    enrich_price_changes(snapshot["symbols"])
    candidates = []
    for group in snapshot["symbols"]:
        if is_rwa_stock_pair(group["symbol"]):
            continue
        row = group["rows"][0]
        priority = max(abs(row.get("change_24h") or 0), abs(row.get("change_7d") or 0), row.get("futures_volume") or 0)
        for timeframe in ("30m", "4h"):
            candidates.append((priority, group["symbol"], timeframe))
    candidates = sorted(candidates, reverse=True)[:360]
    signals = []
    early_signals = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_horn_metrics, symbol, timeframe) for _, symbol, timeframe in candidates]
        for future in as_completed(futures):
            item = future.result()
            if item and item.get("early_trend"):
                early_signals.append({"symbol": item["symbol"], **item["early_trend"]})
            if item and item["oi_change"] > 0 and item["ratio_change"] < 0 and item["score"] >= 42:
                signals.append({key: value for key, value in item.items() if key != "early_trend"})
    continuation_symbols = []
    seen_symbols = set()
    for _, symbol, _ in candidates:
        if symbol not in seen_symbols:
            seen_symbols.add(symbol)
            continuation_symbols.append(symbol)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_horn_continuation_metrics, symbol) for symbol in continuation_symbols[:80]]
        for future in as_completed(futures):
            item = future.result()
            if item:
                signals.append(item)
    report_date = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    DailyHornSignal.query.filter_by(report_date=report_date).delete()
    EarlyTrendSignal.query.filter_by(report_date=report_date).delete()
    selected = [item for timeframe in ("30m", "4h", "continue") for item in sorted((value for value in signals if value["timeframe"] == timeframe), key=lambda value: value["score"], reverse=True)[:20]]
    for early in early_signals:
        selected.append({
            "symbol": early["symbol"], "timeframe": "focus",
            "price_change": early["price_change_5"], "oi_change": early["oi_change_5"],
            "oi_value": None, "ratio_change": early["ratio_change_5"], "ratio_value": None,
            "cvd_change": early["cvd_change_5"], "cvd_confirmed": early["cvd_change_5"] > 0,
            "score": 100.0 if early["signal_type"] == "strong_focus" else 68.0,
        })
    for item in selected:
        db.session.add(DailyHornSignal(report_date=report_date, **item))
    for item in sorted(early_signals, key=lambda value: (value["signal_type"] == "strong_focus", value["price_change_5"]), reverse=True)[:30]:
        db.session.add(EarlyTrendSignal(report_date=report_date, **item))
    db.session.commit()
    return len(selected) + len(early_signals)


def daily_lark_trend_candidates(report_date):
    early_map = {item.symbol: item for item in EarlyTrendSignal.query.filter_by(report_date=report_date).all()}
    grouped = {}
    for item in DailyHornSignal.query.filter_by(report_date=report_date).order_by(DailyHornSignal.score.desc()).all():
        grouped.setdefault(item.symbol, []).append(item)
    candidates = []
    for symbol, items in grouped.items():
        primary = max(items, key=lambda item: item.score)
        early = early_map.get(symbol)
        early_bonus = 120 if early and early.signal_type == "strong_focus" else (25 if early else 0)
        resonance = primary.score + (12 if len(items) > 1 else 0) + early_bonus
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
    primary = frames.get("focus") or frames.get("4h") or frames.get("30m") or frames.get("continue")
    short = frames.get("30m")
    long = frames.get("4h")
    continuation = frames.get("continue")
    focus = frames.get("focus")
    resonance = len(items) > 1
    cvd_ok = all(item.cvd_confirmed for item in items)
    strong = primary.score >= 85
    overheated = (primary.price_change or 0) > 70 and (primary.oi_change or 0) > 90
    ratio_drop = primary.ratio_change or 0
    oi_text = f"持仓增加 {primary.oi_change:+.1f}%"
    ratio_text = f"多空人数比下降 {abs(ratio_drop):.1f}%"
    if focus:
        core = "最近5根30M整体涨幅、CVD与犄角结构同时满足重点条件；方向偏多，但必须结合前置K线判断是首次点火还是后段加速。"
    elif continuation and not (short or long):
        core = "犄角延续型：前期持仓爆发后仍保留大部分仓位，多空人数比继续压低，价格结构仍在延续。"
    elif resonance and cvd_ok and strong:
        core = f"30M 与 4H 同时共振，{oi_text}、{ratio_text}，主动买入也在跟，属于比较标准的犄型主升结构。"
    elif resonance and not cvd_ok:
        core = f"30M 与 4H 结构同向，但 CVD 没有完全确认，说明价格和持仓在推，主动买入并不够干净，要防冲高后的背离。"
    elif long and not short:
        core = f"4H 结构成立但 30M 没跟上，像大级别趋势里的短线休整；如果 30M 重新放量转强，再看共振加强。"
    elif short and not long:
        core = f"30M 先走强但 4H 还没确认，更像短线点火；要等 4H 持仓继续上、人数比继续下，才能升级成趋势机会。"
    else:
        core = f"{primary.timeframe.upper()} 结构成立，但暂时不是最完整的共振，只能按候选观察。"

    if continuation and cvd_ok:
        flow = "CVD 仍然正向累积，不能只因 OI 从峰值小幅回落就剔除；继续盯放量、资费和基差是否转坏。"
    elif overheated:
        flow = "涨幅和持仓扩张都很猛，优势是主力推动明显，缺点是短线拥挤，接近压力位时要看是否放量承接。"
    elif cvd_ok and primary.ratio_change < -20:
        flow = "CVD 上涨且人数比明显下跌，说明主动买入在推，散户侧仍有做空/不追多的对手盘，延续性比普通拉升更好。"
    elif cvd_ok:
        flow = "CVD 是正向确认，但人数比下跌幅度不算极端，属于偏强而非无脑追涨。"
    else:
        flow = "CVD 没给足确认，若后续价格新高但 CVD 不新高，要把它从主升候选降级成冲高派发观察。"

    if support and resistance:
        width = (resistance - support) / support * 100 if support else 0
        if width < 6:
            levels = f"关键位：{support:.6g} 是近端防守，{resistance:.6g} 是上沿；区间不宽，突破要看量，不放量容易假突破。"
        else:
            levels = f"关键位：守住 {support:.6g} 结构还在，向上看 {resistance:.6g}；若接近上沿放量不涨，优先防派发。"
    else:
        levels = "关键位暂未同步，先看最近平台低点是否守住，以及新高时成交量能不能跟。"
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
            if item.timeframe != "focus" and (item.oi_value is None or item.ratio_value is None):
                fresh = fetch_horn_continuation_metrics(symbol) if item.timeframe == "continue" else fetch_horn_metrics(symbol, item.timeframe)
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
            label = "延续" if timeframe == "continue" else ("5根重点" if timeframe == "focus" else timeframe.upper())
            if not item:
                return f"近{label}：暂无完整结构"
            price_color = lark_signed_color(item.price_change)
            oi_color = lark_signed_color(item.oi_change)
            ratio_color = "cus-bear" if item.ratio_change and item.ratio_change < 0 else lark_signed_color(item.ratio_change)
            return (
                f"近{label}：价格 <font color='{price_color}'>{item.price_change:+.2f}%</font>"
                f"｜持仓 <font color='{oi_color}'>{item.oi_change:+.2f}%</font>（{lark_large_value(item.oi_value)}）"
                f"｜多空人数比 <font color='{ratio_color}'>{item.ratio_change:+.2f}%</font>（{lark_ratio_value(item.ratio_value)}）"
                f"｜CVD {lark_cvd_label(item.cvd_change)}"
            )

        short_item = rows.get("30m")
        long_item = rows.get("4h")
        continue_item = rows.get("continue")
        early_stage = EarlyTrendSignal.query.filter_by(report_date=report_date, symbol=symbol).first()
        if early_stage:
            setup_title = early_stage.stage_label
        elif short_item and long_item:
            setup_title = "双周期犄型共振"
        elif long_item:
            setup_title = "4H 主结构成立，30M 等回踩确认"
        elif continue_item:
            setup_title = "犄角延续型，结构分参与前三排序"
        else:
            setup_title = "30M 短线点火，等待 4H 跟随"
        levels_text = f"向下看 {support:.6g}｜向上看 {resistance:.6g}" if support and resistance else "关键位暂未同步"
        sections.append("\n".join([
            f"{lark_dot_label('↑ 看涨 / ' + ('较强' if resonance >= 85 else '观察'), 'cus-bull')}",
            f"**{index}. {symbol}**　{lark_dot_label('重点启动信号' if early_stage and early_stage.signal_type == 'strong_focus' else f'结构分 {resonance:.1f}', 'cus-bull' if early_stage and early_stage.signal_type == 'strong_focus' else lark_score_color(resonance))}",
            f"结构：{setup_title}",
            f"时间：{report_date} 08:00",
            metric_line("focus") if "focus" in rows else "",
            metric_line("30m"),
            metric_line("4h"),
            metric_line("continue") if "continue" in rows else "",
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
        return jsonify({"updated_at": None, "rising": [], "falling": [], "horn_30m": [], "horn_4h": [], "horn_continue": [], "early_focus": []})
    enrich_price_changes(snapshot["symbols"])
    rows = [
        {"symbol": group["symbol"], "change_24h": group["rows"][0].get("change_24h"), "change_7d": group["rows"][0].get("change_7d")}
        for group in snapshot["symbols"] if not is_rwa_stock_pair(group["symbol"])
    ]
    valid = [item for item in rows if item["change_24h"] is not None]
    report_date = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    horn_rows = DailyHornSignal.query.filter_by(report_date=report_date).order_by(DailyHornSignal.score.desc()).all()
    early_rows = EarlyTrendSignal.query.filter_by(report_date=report_date).order_by(
        EarlyTrendSignal.stage_number.asc(), EarlyTrendSignal.price_change_5.desc()
    ).all()
    signal_payload = lambda item: {"symbol": item.symbol, "timeframe": item.timeframe, "price_change": item.price_change, "oi_change": item.oi_change, "oi_value": item.oi_value, "ratio_change": item.ratio_change, "ratio_value": item.ratio_value, "cvd_confirmed": item.cvd_confirmed, "score": item.score}
    early_payload = lambda item: {
        "symbol": item.symbol, "signal_type": item.signal_type, "stage_key": item.stage_key,
        "stage_label": item.stage_label, "stage_number": item.stage_number,
        "stage_reason": item.stage_reason, "price_change_5": item.price_change_5,
        "cvd_change_5": item.cvd_change_5, "oi_change_5": item.oi_change_5,
        "ratio_change_5": item.ratio_change_5, "prior_price_change": item.prior_price_change,
        "prior_range": item.prior_range, "volume_ratio": item.volume_ratio,
        "last_price": item.last_price,
    }
    return jsonify({"updated_at": snapshot["updated_at"], "rising": sorted(valid, key=lambda item: item["change_24h"], reverse=True)[:20], "falling": sorted(valid, key=lambda item: item["change_24h"])[:20], "horn_30m": [signal_payload(item) for item in horn_rows if item.timeframe == "30m"], "horn_4h": [signal_payload(item) for item in horn_rows if item.timeframe == "4h"], "horn_continue": [signal_payload(item) for item in horn_rows if item.timeframe == "continue"], "early_focus": [early_payload(item) for item in early_rows], "automation_status": automation_statuses("daily_horn_scan", "daily_lark_trend_push")})


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
    "SOON/USDT": {
        "entry": None,
        "entry_time": "2026-07-28 等待基差转负后的换手",
        "side": "short_watch",
        "fallback": {},
    },
    "ZAMA/USDT": {
        "entry": None,
        "entry_time": "2026-07-28 基差先行转负观察",
        "side": "short_watch",
        "fallback": {},
    },
    "ERA/USDT": {
        "entry": None,
        "entry_time": "2026-07-24 看空观察",
        "side": "short_watch",
        "fallback": {
            "support": 0.0937,
            "resistance": 0.10086,
        },
    },
}

THOUGHT_WATCH_SEED = {
    "AKE/USDT": ("2026-07-16 19:07", True, "AKE新机会与主力出货结构"),
    "US/USDT": ("2026-07-18 00:00", False, "已按用户要求停止盯盘"),
    "T/USDT": ("2026-07-17 11:00", True, "底部反抽后先多再空"),
    "SOON/USDT": ("2026-07-28 00:00", True, "主升后基差换手"),
    "ZAMA/USDT": ("2026-07-28 00:00", True, "深负基差与换手结构"),
    "ERA/USDT": ("2026-07-24 00:00", True, "弱支撑与反转结构"),
}


def first_thought_watch_price(symbol, fallback=None):
    if fallback:
        return fallback
    event = ThoughtPushEvent.query.filter_by(symbol=symbol).order_by(ThoughtPushEvent.reserved_at.asc()).first()
    if event and event.snapshot_json:
        try:
            return float((json.loads(event.snapshot_json) or {}).get("last_price") or 0) or None
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    history = FuturesPriceHistory.query.filter_by(symbol=symbol).order_by(FuturesPriceHistory.bucket_at.asc()).first()
    return history.price if history else None


def seed_thought_watches():
    for symbol, (started_text, active, note) in THOUGHT_WATCH_SEED.items():
        if ThoughtWatch.query.filter_by(symbol=symbol).first():
            continue
        started_at = datetime.strptime(started_text, "%Y-%m-%d %H:%M")
        start_price = first_thought_watch_price(symbol, (THOUGHT_WATCHLIST.get(symbol) or {}).get("entry"))
        row = ThoughtWatch(symbol=symbol, active=active, started_at=started_at, start_price=start_price, note=note)
        if not active:
            row.stopped_at = datetime.now()
            row.stop_price = start_price
        db.session.add(row)
    db.session.commit()


def active_thought_symbols():
    return {row.symbol for row in ThoughtWatch.query.filter_by(active=True).all()}


def thought_watch_config(symbol):
    """Return a persistent generic configuration for symbols added from the web UI."""
    config = THOUGHT_WATCHLIST.get(symbol)
    if config is not None:
        return config
    row = ThoughtWatch.query.filter_by(symbol=symbol).first()
    return {
        "entry": None,
        "entry_time": row.started_at.strftime("%Y-%m-%d %H:%M") if row else "网页新增盯盘",
        "side": "watch",
        "fallback": {},
    }

# 30秒基差监控连续确认状态。完整盯盘只接受已连续两次为负的基差，防止单点插针。
TURNOVER_BASIS_STATE = {}
# 换手方向切换候选：滚动K线在未收盘时会快速抖动，方向改变必须跨两次独立完整扫描。
TURNOVER_DIRECTION_CANDIDATES = {}


def thought_snapshot(symbol):
    config = thought_watch_config(symbol)
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
        "micro_validation": {},
        "orderbook_wall": {},
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "fallback",
    }
    fallback.update(fallback_overrides)
    try:
        live_timeout = 2
        ticker = get_json("https://fapi.binance.com/fapi/v1/ticker/24hr?" + urlencode({"symbol": raw_symbol}), timeout=live_timeout)
        k30 = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "30m", "limit": 60}), timeout=live_timeout)
        k4h = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "4h", "limit": 30}), timeout=live_timeout)
        premium = get_json("https://fapi.binance.com/fapi/v1/premiumIndex?" + urlencode({"symbol": raw_symbol}), timeout=live_timeout)
        oi = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": 50}), timeout=live_timeout)
        ratios = get_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": 50}), timeout=live_timeout)
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
        micro_validation = fetch_t_micro_metrics(raw_symbol) if symbol in {"T/USDT", "AKE/USDT", "ERA/USDT", "SOON/USDT", "ZAMA/USDT"} else {}
        orderbook_wall = fetch_ake_orderbook_wall(raw_symbol) if symbol == "AKE/USDT" else {}
        market_context = thought_market_context(symbol) or {}
        validation = {"30m": window_metrics(1), "1h": window_metrics(2), "2h": window_metrics(4), "4h": window_metrics(8)}
        for key in ("5m", "15m"):
            if micro_validation.get(key):
                validation[key] = micro_validation[key]
        return {
            **fallback,
            **{key: value for key, value in market_context.items() if value is not None},
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
            "validation": validation,
            "micro_validation": micro_validation,
            "orderbook_wall": orderbook_wall,
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
        "support": fallback.get("support"),
        "resistance": fallback.get("resistance"),
        "oi_change_pct": None,
        "ratio_value": None,
        "ratio_change_pct": None,
        "cvd": None,
        "change_30m": None,
        "change_4h": None,
        "validation": {},
        "micro_validation": {},
        "orderbook_wall": {},
        "source": "db_fallback",
    }


def thought_fast_snapshot(symbol):
    config = thought_watch_config(symbol)
    now = datetime.now(SHANGHAI_TZ)
    fallback = {
        "symbol": symbol,
        "entry": config.get("entry"),
        "entry_time": config.get("entry_time") or "重点观察",
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
        "micro_validation": {},
        "orderbook_wall": {},
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "db_fallback",
    }
    fallback.update(config.get("fallback") or {})
    return thought_snapshot_from_db(symbol, fallback)


def thought_market_context(symbol):
    market_rows = LatestMarketSnapshot.query.filter_by(symbol=symbol).order_by(LatestMarketSnapshot.captured_at.desc()).all()
    if not market_rows:
        dual = LatestDualFuturesSnapshot.query.filter(
            LatestDualFuturesSnapshot.symbol == symbol,
            or_(LatestDualFuturesSnapshot.long_exchange == "Binance", LatestDualFuturesSnapshot.short_exchange == "Binance"),
        ).order_by(LatestDualFuturesSnapshot.captured_at.desc()).first()
        if not dual:
            return None
        binance_is_long = dual.long_exchange == "Binance"
        bid = dual.long_bid if binance_is_long else dual.short_bid
        ask = dual.long_ask if binance_is_long else dual.short_ask
        futures_mid = ((bid or 0) + (ask or 0)) / 2 if bid and ask else None
        return {
            "last": futures_mid,
            "oi_value": dual.long_open_interest if binance_is_long else dual.short_open_interest,
            "funding_rate": dual.long_funding_rate if binance_is_long else dual.short_funding_rate,
            "basis": dual.long_basis if binance_is_long else dual.short_basis,
            "open_spread": dual.open_spread,
            "close_spread": dual.close_spread,
            "spot_volume": None,
            "futures_volume": dual.long_volume if binance_is_long else dual.short_volume,
            "futures_spot_volume_ratio": None,
            "updated_at": dual.captured_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
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


def thought_watch_snapshots(only_symbols=None):
    active_symbols = active_thought_symbols()
    symbols = sorted(symbol for symbol in active_symbols if not only_symbols or symbol in only_symbols)

    def load_symbol(symbol):
        with app.app_context():
            return symbol, thought_snapshot(symbol)

    results = {}
    with ThreadPoolExecutor(max_workers=min(4, len(symbols) or 1)) as executor:
        futures = [executor.submit(load_symbol, symbol) for symbol in symbols]
        for future in as_completed(futures):
            symbol, snapshot = future.result()
            results[symbol] = snapshot
    return [results[symbol] for symbol in symbols if symbol in results]


def t_micro_direction(analysis):
    if analysis.get("symbol") != "T/USDT":
        return None
    micro = analysis.get("micro_validation") or {}
    m5 = micro.get("5m") or {}
    m15 = micro.get("15m") or {}
    m30 = micro.get("30m") or {}
    last = analysis.get("last") or micro.get("current")
    if not last:
        return None

    def value(row, key, default=0):
        item = row.get(key)
        return default if item is None else item

    near_floor = 0.00392 <= last <= 0.00418
    breaks_floor_box = last >= 0.00412 and value(m5, "price_change") >= 0.45
    short_cvd_turns_up = value(m5, "cvd") > 0 and value(m15, "cvd") > 0
    volume_wakes = max(value(m5, "volume_ratio"), value(m15, "volume_ratio"), value(m30, "volume_ratio")) >= 1.35
    oi_not_dumping = min(value(m5, "oi_change"), value(m15, "oi_change")) > -1.2
    ratio_not_overheated = value(m15, "ratio_change") < 8.0
    if near_floor and breaks_floor_box and short_cvd_turns_up and volume_wakes and oi_not_dumping and ratio_not_overheated:
        return "t_bounce_long"

    bounce_extended = last >= 0.00428
    price_stalls = value(m5, "price_change") <= 0.15 and value(m15, "price_change") <= 0.55
    cvd_weakens = value(m5, "cvd") < 0 or value(m15, "cvd") < 0
    volume_hot = max(value(m5, "volume_ratio"), value(m15, "volume_ratio")) >= 1.5
    funding_bad = analysis.get("funding_rate") is not None and analysis.get("funding_rate") < 0
    basis_bad = analysis.get("basis") is not None and analysis.get("basis") < -0.35
    ratio_chases = value(m15, "ratio_change") > 1.0 or value(m30, "ratio_change") > 1.5
    if bounce_extended and price_stalls and (cvd_weakens or ratio_chases) and (volume_hot or funding_bad or basis_bad):
        return "t_bounce_stall_short"
    return None


def tlm_trap_short_direction(analysis):
    if analysis.get("symbol") != "TLM/USDT":
        return None
    funding = analysis.get("funding_rate")
    basis = analysis.get("basis")
    validation = analysis.get("validation") or {}
    checks = [validation.get(key) or {} for key in ("30m", "1h", "2h")]
    valid = [item for item in checks if item.get("price_change") is not None]

    def value(row, key, default=0):
        item = row.get(key)
        return default if item is None else item

    price_reclaim = sum(value(item, "price_change") > 0.6 for item in valid) >= 2
    cvd_reversal = sum(value(item, "cvd") > 0 for item in valid) >= 2
    ratio_not_chasing = sum(value(item, "ratio_change") <= 0.2 for item in valid) >= 2
    funding_repaired = funding is not None and funding > -0.03
    basis_repaired = basis is not None and basis > -0.20
    if valid and price_reclaim and cvd_reversal and (ratio_not_chasing or funding_repaired or basis_repaired):
        return "tlm_reversal"

    funding_deep_negative = funding is not None and funding <= -0.08
    basis_negative = basis is not None and basis <= -0.35
    if not valid:
        open_spread = analysis.get("open_spread")
        return "tlm_trap_short" if funding_deep_negative and basis_negative and (open_spread is None or open_spread <= 0) else None
    recent_bounce = any(value(item, "price_change") >= 0.25 for item in valid)
    cvd_selling = sum(value(item, "cvd") < 0 for item in valid) >= 1
    ratio_chasing_long = sum(value(item, "ratio_change") > 0.25 for item in valid) >= 1
    oi_not_collapsed = sum(value(item, "oi_change") > -2.5 for item in valid) >= 2
    if funding_deep_negative and basis_negative and (cvd_selling or ratio_chasing_long) and oi_not_collapsed:
        return "tlm_trap_short"
    if recent_bounce and cvd_selling and ratio_chasing_long and (funding_deep_negative or basis_negative):
        return "tlm_trap_short"
    return None


def turnover_structure_features(analysis):
    """Shared measurements only; SOON and ZAMA interpret them with separate hypotheses."""
    funding = analysis.get("funding_rate")
    basis = analysis.get("basis")
    validation = analysis.get("validation") or {}
    micro = analysis.get("micro_validation") or {}

    def value(row, key, default=0):
        item = (row or {}).get(key)
        return default if item is None else item

    m5 = micro.get("5m") or {}
    m15 = micro.get("15m") or {}
    m30 = validation.get("30m") or micro.get("30m") or {}
    short_rows = [row for row in (m5, m15, m30) if row.get("price_change") is not None]
    price_stalls = sum(value(row, "price_change") <= 0.15 for row in short_rows) >= 2
    cvd_weakens = sum(value(row, "cvd") < 0 for row in short_rows) >= 2
    oi_unwinds = sum(value(row, "oi_change") < -0.25 for row in short_rows) >= 1
    ratio_chases_long = sum(value(row, "ratio_change") > 0.35 for row in short_rows) >= 1
    volume_hot = max([value(row, "volume_ratio") for row in short_rows] or [0]) >= 1.35
    medium_rows = [validation.get(key) or {} for key in ("30m", "1h", "2h")]
    horn_price = sum(value(row, "price_change") > 0 for row in medium_rows) >= 2
    horn_oi = sum(value(row, "oi_change") > 0 for row in medium_rows) >= 2
    horn_ratio = sum(value(row, "ratio_change") < 0 for row in medium_rows) >= 2
    horn_cvd = sum(value(row, "cvd") > 0 for row in medium_rows) >= 2
    return {
        "funding": funding,
        "basis": basis,
        "price_stalls": price_stalls,
        "cvd_weakens": cvd_weakens,
        "oi_unwinds": oi_unwinds,
        "ratio_chases_long": ratio_chases_long,
        "volume_hot": volume_hot,
        "bullish_horn_core": horn_price and horn_oi and horn_ratio,
        "bullish_horn": horn_price and horn_oi and horn_ratio and horn_cvd,
    }


def soon_turnover_short_direction(analysis):
    """SOON: monitor the destruction of its positive-premium main trend."""
    if analysis.get("symbol") != "SOON/USDT" or analysis.get("source") != "live":
        return None
    features = turnover_structure_features(analysis)
    funding, basis = features["funding"], features["basis"]
    observed_basis = (TURNOVER_BASIS_STATE.get("SOON/USDT") or {}).get("basis")
    if observed_basis is not None and basis is not None:
        basis = min(basis, observed_basis)
        analysis["turnover_basis_observed"] = observed_basis
    if funding is None or basis is None or basis >= 0:
        return None
    if not (TURNOVER_BASIS_STATE.get(analysis.get("symbol")) or {}).get("stable"):
        return None
    # 负基差只能说明合约短暂贴水。若1H/2H仍是价格涨、持仓涨、人数比跌的犄角核心，
    # 先按主升延续的反证保护，不能被5MIN/15MIN回调误升级为做空确认。
    if features["bullish_horn_core"]:
        return "soon_basis_negative_watch"
    turnover_confirmed = features["price_stalls"] and features["cvd_weakens"] and (
        features["oi_unwinds"] or features["ratio_chases_long"] or features["volume_hot"]
    )
    if turnover_confirmed:
        return "soon_turnover_short_ready"
    if funding < 0:
        return "soon_funding_follow_watch"
    return "soon_basis_negative_watch"


def zama_turnover_short_direction(analysis):
    """ZAMA: negative basis can coexist with a bullish horn, so protect against squeezing first."""
    if analysis.get("symbol") != "ZAMA/USDT" or analysis.get("source") != "live":
        return None
    features = turnover_structure_features(analysis)
    funding, basis = features["funding"], features["basis"]
    observed_basis = (TURNOVER_BASIS_STATE.get("ZAMA/USDT") or {}).get("basis")
    if observed_basis is not None and basis is not None:
        basis = min(basis, observed_basis)
        analysis["turnover_basis_observed"] = observed_basis
    if funding is None or basis is None or basis >= 0:
        return None
    if not (TURNOVER_BASIS_STATE.get(analysis.get("symbol")) or {}).get("stable"):
        return None
    turnover_confirmed = features["price_stalls"] and features["cvd_weakens"] and (
        features["oi_unwinds"] or features["ratio_chases_long"] or features["volume_hot"]
    )
    # ZAMA的-2%瞬时深贴水是独立换手阶段；先提醒深基差，再由结构决定能否做空。
    if basis <= -2.0:
        if turnover_confirmed:
            return "zama_turnover_short_ready"
        if funding < 0:
            return "zama_deep_basis_funding_follow"
        return "zama_deep_basis_watch"
    # ZAMA当前最重要的反证：负基差存在，但中周期仍是持仓增、人数比降、CVD涨的偏多犄角。
    if features["bullish_horn"]:
        return "zama_negative_basis_bullish_horn"
    if turnover_confirmed:
        return "zama_turnover_short_ready"
    if funding < 0:
        return "zama_funding_follow_watch"
    return "zama_basis_negative_watch"


def era_squeeze_direction(analysis):
    """ERA 两阶段盯盘：先抓拥挤空头的逼空反弹，再抓诱多停滞后的重新转弱。"""
    if analysis.get("symbol") != "ERA/USDT":
        return None
    micro = analysis.get("micro_validation") or {}
    validation = analysis.get("validation") or {}
    last = analysis.get("last") or micro.get("current")
    if not last:
        return None

    def value(row, key, default=0):
        item = row.get(key)
        return default if item is None else item

    m5, m15, m30 = (micro.get(key) or {} for key in ("5m", "15m", "30m"))
    h1 = validation.get("1h") or {}
    short_rows = [row for row in (m5, m15, m30) if row.get("price_change") is not None]
    price_up = sum(value(row, "price_change") >= 0.25 for row in short_rows)
    cvd_up = sum(value(row, "cvd") > 0 for row in short_rows)
    oi_supported = sum(value(row, "oi_change") >= -0.15 for row in short_rows)
    ratio_compressed = sum(value(row, "ratio_change") <= 0 for row in short_rows)
    volume_ratio = max([value(row, "volume_ratio") for row in short_rows] or [0])

    # 第二阶段优先：反弹进入目标带后，短周期主动买入衰竭，提示诱多可能结束。
    bounce_reached = last >= 0.0732
    price_stalls = value(m5, "price_change") <= 0.10 and value(m15, "price_change") <= 0.35
    cvd_weakens = value(m5, "cvd") < 0 or value(m15, "cvd") < 0
    structure_unwinds = value(m15, "oi_change") <= -0.35 or value(m15, "ratio_change") >= 0.35
    if bounce_reached and price_stalls and cvd_weakens and (structure_unwinds or volume_ratio >= 1.35):
        return "era_bounce_stall_short"

    # 第一阶段确认：站上0.0726，至少两个短周期价格/CVD转强，并有仓位和人数结构配合。
    if last >= 0.0726 and price_up >= 2 and cvd_up >= 2 and oi_supported >= 2 and ratio_compressed >= 2 and volume_ratio >= 1.15:
        return "era_squeeze_confirmed"

    # 萌芽提示：离开0.0714低点区，30M/1H已改善，但仍明确标成反抽/逼空观察，不叫趋势反转。
    h1_improves = value(h1, "price_change") > 0 and value(h1, "oi_change") > 0 and value(h1, "ratio_change") < 0 and value(h1, "cvd") > 0
    if last >= 0.07205 and ((price_up >= 2 and cvd_up >= 2) or h1_improves) and oi_supported >= 2:
        return "era_squeeze_probe"
    return None


def ake_orderbook_wall_direction(analysis):
    if analysis.get("symbol") != "AKE/USDT":
        return None
    wall = analysis.get("orderbook_wall") or {}
    last = analysis.get("last") or wall.get("last")
    if not last:
        return None

    wall_qty = wall.get("wall_qty") or 0
    wall_notional = wall.get("wall_notional") or 0
    reference_qty = sum((item.get("qty") or 0) for item in (wall.get("reference_buckets") or []))
    wall_exists = wall_qty >= 2_000_000 or wall_notional >= 3_000 or reference_qty >= 2_000_000
    if not wall_exists:
        return None

    validation = analysis.get("validation") or {}
    short_windows = [validation.get(key) or {} for key in ("30m", "1h", "2h")]

    def value(row, key, default=0):
        item = row.get(key)
        return default if item is None else item

    cvd_up = sum(value(item, "cvd") > 0 for item in short_windows)
    oi_up = sum(value(item, "oi_change") > 0 for item in short_windows)
    price_up = sum(value(item, "price_change") > 0.25 for item in short_windows)
    price_weak = sum(value(item, "price_change") < -0.25 for item in short_windows)
    cvd_weak = sum(value(item, "cvd") < 0 for item in short_windows)
    volume_active = any(value(item, "volume_ratio") >= 1.35 for item in short_windows)
    resistance = analysis.get("resistance") or 0
    wall_was_tested = last >= 0.00195 or resistance >= 0.00198
    wall_spiked_above_entry = resistance >= 0.0020
    in_wall_zone = 0.0020 <= last < 0.0022
    near_wall_lower = 0.00195 <= last < 0.0020

    if last >= 0.0022:
        return "ake_wall_breakout"
    if in_wall_zone:
        return "ake_wall_zone_strength"
    if wall_spiked_above_entry and 0.00192 <= last < 0.0020 and (oi_up >= 1 or cvd_up >= 1 or not volume_active):
        return "ake_wall_spike_retest"
    if near_wall_lower and volume_active and cvd_up >= 1:
        return "ake_wall_test"
    if wall_was_tested and last < 0.00190 and volume_active and price_weak >= 2 and cvd_weak >= 2 and oi_up == 0:
        return "ake_wall_rejection"
    return None


def ake_structure_direction(analysis):
    """AKE 离开旧卖墙区后，不再只盯 0.0020-0.0022，而是按结构强弱重新分层。"""
    if analysis.get("symbol") != "AKE/USDT":
        return None
    last = analysis.get("last")
    if not last:
        return None
    funding = analysis.get("funding_rate")
    basis = analysis.get("basis")
    validation = analysis.get("validation") or {}
    windows = [validation.get(key) or {} for key in ("30m", "1h", "2h")]

    def value(row, key, default=0):
        item = row.get(key)
        return default if item is None else item

    oi_down_votes = sum(value(item, "oi_change") <= -1.0 for item in windows)
    oi_up_votes = sum(value(item, "oi_change") >= 1.0 for item in windows)
    ratio_up_votes = sum(value(item, "ratio_change") >= 0.3 for item in windows)
    ratio_down_votes = sum(value(item, "ratio_change") <= -0.3 for item in windows)
    cvd_up_votes = sum(value(item, "cvd") > 0 for item in windows)
    cvd_down_votes = sum(value(item, "cvd") < 0 for item in windows)
    price_up_votes = sum(value(item, "price_change") > 0.4 for item in windows)
    price_down_votes = sum(value(item, "price_change") < -0.4 for item in windows)
    broad_oi_change = analysis.get("oi_change_pct")
    broad_ratio_change = analysis.get("ratio_change_pct")
    funding_negative = funding is not None and funding < 0
    basis_negative = basis is not None and basis < 0
    funding_positive = funding is not None and funding > 0
    basis_positive = basis is not None and basis > 0
    main_long_unwind = (
        oi_down_votes >= 1 and ratio_up_votes >= 1
    ) or (
        broad_oi_change is not None and broad_ratio_change is not None
        and broad_oi_change <= -5 and broad_ratio_change >= 3
    )

    if last >= 0.0022:
        # 高位币最容易误判的一种结构：CVD 仍可能上涨，但持仓下降 + 多空人数比上升，
        # 对资金面解释是散户平空、主力平多，不能继续按单纯逼空看涨处理。
        if main_long_unwind:
            return "ake_main_long_unwind_watch"
        if funding_negative and basis_negative:
            return "ake_above_wall_distribution_watch"
        if oi_down_votes >= 1 and (cvd_down_votes >= 1 or basis_negative or funding_negative):
            return "ake_above_wall_bull_weakening"
        if funding_positive and basis_positive and (oi_up_votes >= 1 or ratio_down_votes >= 1 or cvd_up_votes >= 1 or not windows):
            return "ake_above_wall_bull_continue"
        return "ake_above_wall_new_range"
    if 0.0020 <= last < 0.0022:
        if funding_negative or basis_negative or oi_down_votes >= 1:
            return "ake_wall_zone_weakening"
        return "ake_wall_zone_strength"
    if last < 0.0020 and (funding_negative or basis_negative) and (price_down_votes >= 1 or cvd_down_votes >= 1 or oi_down_votes >= 1):
        return "ake_wall_failed_watch"
    return None


def thought_push_direction(analysis):
    symbol = analysis.get("symbol")
    # AKE/ERA are high-frequency narrative watches. A temporary exchange timeout
    # must not let a stale DB fallback flip their direction and generate a false
    # "new idea" push. Wait for the next complete live snapshot instead.
    if symbol in {"AKE/USDT", "ERA/USDT", "SOON/USDT", "ZAMA/USDT"} and analysis.get("source") != "live":
        return None
    t_direction = t_micro_direction(analysis)
    if t_direction:
        return t_direction
    era_direction = era_squeeze_direction(analysis)
    if symbol == "ERA/USDT":
        return era_direction
    if symbol == "SOON/USDT":
        return soon_turnover_short_direction(analysis)
    if symbol == "ZAMA/USDT":
        return zama_turnover_short_direction(analysis)
    tlm_direction = tlm_trap_short_direction(analysis)
    if tlm_direction:
        return tlm_direction
    if symbol == "T/USDT":
        return None
    if symbol == "AKE/USDT":
        ake_structure = ake_structure_direction(analysis)
        if ake_structure in {
            "ake_main_long_unwind_watch",
            "ake_above_wall_distribution_watch",
            "ake_above_wall_bull_weakening",
            "ake_wall_zone_weakening",
            "ake_wall_failed_watch",
        }:
            return ake_structure
    ake_direction = ake_orderbook_wall_direction(analysis)
    if ake_direction:
        return ake_direction
    ake_structure = ake_structure_direction(analysis)
    if ake_structure:
        return ake_structure
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
    if symbol == "AKE/USDT":
        return None
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
    if symbol == "T/USDT" and funding is not None and basis is not None:
        if funding < 0 and basis <= -1.0 and (open_spread is None or open_spread <= -1.0):
            return "bearish_db_watch"
    return None


def thought_signal_key(analysis, direction):
    last = analysis.get("last") or 0
    resistance = analysis.get("resistance") or 0
    support = analysis.get("support") or 0
    if direction in {
        "soon_basis_negative_watch", "soon_funding_follow_watch", "soon_turnover_short_ready",
        "zama_basis_negative_watch", "zama_funding_follow_watch", "zama_turnover_short_ready",
        "zama_negative_basis_bullish_horn", "zama_deep_basis_watch", "zama_deep_basis_funding_follow",
    }:
        basis = analysis.get("turnover_basis_observed")
        if basis is None:
            basis = analysis.get("basis") or 0
        if basis <= -2.0:
            basis_zone = "below-minus-200"
        elif basis <= -1.0:
            basis_zone = "minus-100-200"
        elif basis <= -0.5:
            basis_zone = "minus-050-100"
        elif basis <= -0.2:
            basis_zone = "minus-020-050"
        elif basis < 0:
            basis_zone = "negative-watch"
        else:
            basis_zone = "positive"
        return f"{direction}-{basis_zone}"
    if direction in {"era_squeeze_probe", "era_squeeze_confirmed", "era_bounce_stall_short"}:
        if last >= 0.0757:
            price_zone = "above-757"
        elif last >= 0.0738:
            price_zone = "bounce-738-757"
        elif last >= 0.0732:
            price_zone = "bounce-732-738"
        elif last >= 0.0726:
            price_zone = "confirm-726-732"
        elif last >= 0.0721:
            price_zone = "probe-721-726"
        else:
            price_zone = "floor-watch"
        return f"{direction}-{price_zone}"
    if direction in {"t_bounce_long", "t_bounce_stall_short"}:
        if last >= 0.0047:
            price_zone = "above-470"
        elif last >= 0.00445:
            price_zone = "stall-445-470"
        elif last >= 0.00428:
            price_zone = "bounce-428-445"
        elif last >= 0.00412:
            price_zone = "floor-break-412-428"
        else:
            price_zone = "floor-watch"
        return f"{direction}-{price_zone}"
    if direction in {"tlm_trap_short", "tlm_reversal"}:
        if last >= 0.0024:
            price_zone = "above-2400"
        elif last >= 0.0022:
            price_zone = "trap-2200-2400"
        elif last >= 0.0020:
            price_zone = "trap-2000-2200"
        elif last >= 0.00185:
            price_zone = "entry-1850-2000"
        else:
            price_zone = "below-1850"
        return f"{direction}-{price_zone}"
    if direction in {"ake_wall_test", "ake_wall_spike_retest", "ake_wall_zone_strength", "ake_wall_breakout", "ake_wall_rejection", "ake_main_long_unwind_watch", "ake_above_wall_distribution_watch", "ake_above_wall_bull_weakening", "ake_above_wall_bull_continue", "ake_above_wall_new_range", "ake_wall_zone_weakening", "ake_wall_failed_watch"}:
        if last >= 0.0028:
            price_zone = "above-2800"
        elif last >= 0.0024:
            price_zone = "above-2400"
        elif last >= 0.0022:
            price_zone = "break-2200"
        elif last >= 0.0021:
            price_zone = "wall-2100-2200"
        elif last >= 0.0020:
            price_zone = "wall-2000-2100"
        elif last >= 0.00195:
            price_zone = "test-1950-2000"
        elif last >= 0.00190:
            price_zone = "retest-1900-1950"
        else:
            price_zone = "below-1900"
        return f"{direction}-{price_zone}"
    if direction == "bullish" and resistance and last >= resistance * 0.995:
        return "bullish-near-breakout"
    if direction in {"bearish", "reversal", "distribution"} and support and last <= support * 1.005:
        return "bearish-near-breakdown"
    return f"{direction}-resonance"


def metric_changed(current, previous, abs_threshold=None, pct_threshold=None):
    if current is None or previous is None:
        return current is not None and previous is None
    diff = abs(current - previous)
    if abs_threshold is not None and diff >= abs_threshold:
        return True
    if pct_threshold is not None and abs(previous) > 1e-12 and diff / abs(previous) >= pct_threshold:
        return True
    return False


def meaningful_sign_flip(current, previous, min_abs_each, min_span):
    """Ignore zero-axis jitter; a sign change is meaningful only after moving through a real deadband."""
    if current is None or previous is None or current * previous >= 0:
        return False
    return min(abs(current), abs(previous)) >= min_abs_each and abs(current - previous) >= min_span


def cvd_direction(value):
    if value is None:
        return "none"
    return "up" if value > 0 else ("down" if value < 0 else "flat")


def thought_push_metrics(analysis, direction, signal_key):
    validation = analysis.get("validation") or {}
    wall = analysis.get("orderbook_wall") or {}
    data = {
        "symbol": analysis.get("symbol"),
        "direction": direction,
        "signal_key": signal_key,
        "last_price": analysis.get("last"),
        "basis": analysis.get("turnover_basis_observed") if direction in TURNOVER_THOUGHT_DIRECTIONS and analysis.get("turnover_basis_observed") is not None else analysis.get("basis"),
        "funding_rate": analysis.get("funding_rate"),
        "oi_value": analysis.get("oi_value"),
        "futures_volume": analysis.get("futures_volume"),
        "spot_volume": analysis.get("spot_volume"),
        "wall_qty": wall.get("wall_qty"),
        "wall_notional": wall.get("wall_notional"),
    }
    for key, suffix in (("30m", "30m"), ("1h", "1h"), ("2h", "2h")):
        item = validation.get(key) or {}
        data[f"cvd_{suffix}"] = item.get("cvd")
        data[f"price_change_{suffix}"] = item.get("price_change")
        data[f"oi_change_{suffix}"] = item.get("oi_change")
        data[f"ratio_change_{suffix}"] = item.get("ratio_change")
    return data


def thought_cvd_profile(metrics):
    return tuple(cvd_direction(metrics.get(f"cvd_{suffix}")) for suffix in ("30m", "1h", "2h"))


THOUGHT_REPEAT_COOLDOWN_HOURS = 4

TURNOVER_THOUGHT_DIRECTIONS = {
    "soon_basis_negative_watch", "soon_funding_follow_watch", "soon_turnover_short_ready",
    "zama_basis_negative_watch", "zama_funding_follow_watch", "zama_turnover_short_ready",
    "zama_negative_basis_bullish_horn", "zama_deep_basis_watch", "zama_deep_basis_funding_follow",
}


def thought_hours_since_push(previous):
    if previous is None or not previous.pushed_at:
        return None
    delta = datetime.now() - previous.pushed_at
    return max(delta.total_seconds() / 3600, 0)


def thought_major_narrative_shift(previous, metrics):
    """Only return True for changes worth waking the user inside the 4H window."""
    if previous is None:
        return True
    if previous.direction != metrics["direction"]:
        return True
    if previous.signal_key != metrics["signal_key"]:
        return True

    previous_funding = previous.funding_rate
    current_funding = metrics.get("funding_rate")
    if meaningful_sign_flip(current_funding, previous_funding, min_abs_each=0.005, min_span=0.02):
        return True

    previous_basis = previous.basis
    current_basis = metrics.get("basis")
    if meaningful_sign_flip(current_basis, previous_basis, min_abs_each=0.10, min_span=0.30):
        return True

    if metric_changed(current_funding, previous_funding, abs_threshold=0.20):
        return True
    if metric_changed(current_basis, previous_basis, abs_threshold=0.50):
        return True
    if metric_changed(metrics.get("oi_value"), previous.oi_value, pct_threshold=0.25):
        return True
    if metric_changed(metrics.get("wall_qty"), previous.wall_qty, pct_threshold=1.00):
        return True

    old_cvd = tuple(cvd_direction(getattr(previous, f"cvd_{suffix}", None)) for suffix in ("30m", "1h", "2h"))
    new_cvd = thought_cvd_profile(metrics)
    if old_cvd != new_cvd:
        old_up = sum(direction == "up" for direction in old_cvd)
        new_up = sum(direction == "up" for direction in new_cvd)
        if abs(new_up - old_up) >= 2:
            return True
    return False


def thought_turnover_has_new_information(previous, metrics):
    """换手盯盘只在叙事改变或风险绝对值继续扩大时推送，基差修复不算同方向新消息。"""
    if previous is None:
        return True
    if previous.direction != metrics["direction"]:
        # -2%深基差已经由5秒同步快照连续确认，属于需要立即提醒的价格风险；
        # 其余结构方向会受未收盘5/15/30分钟线影响，必须持续至少20秒并连续两次一致。
        if metrics["direction"] in {"zama_deep_basis_watch", "zama_deep_basis_funding_follow"}:
            TURNOVER_DIRECTION_CANDIDATES.pop(metrics.get("symbol"), None)
            return True
        symbol = metrics.get("symbol")
        now = time.monotonic()
        candidate = TURNOVER_DIRECTION_CANDIDATES.get(symbol)
        if not candidate or candidate.get("direction") != metrics["direction"]:
            TURNOVER_DIRECTION_CANDIDATES[symbol] = {
                "direction": metrics["direction"],
                "first_seen": now,
                "count": 1,
            }
            return False
        candidate["count"] += 1
        if candidate["count"] < 2 or now - candidate["first_seen"] < 20:
            return False
        TURNOVER_DIRECTION_CANDIDATES.pop(symbol, None)
        return True

    TURNOVER_DIRECTION_CANDIDATES.pop(metrics.get("symbol"), None)

    previous_basis = previous.basis
    current_basis = metrics.get("basis")
    # 同方向下只承认风险继续扩大。跨区间但绝对值缩小（如 -0.20% → -0.02%）是修复，
    # 不得用 signal_key 变化绕过重复抑制。
    if previous_basis is not None and current_basis is not None:
        if abs(current_basis) >= abs(previous_basis) + 0.20:
            return True
    elif current_basis is not None and abs(current_basis) >= 0.50:
        return True

    previous_funding = previous.funding_rate
    current_funding = metrics.get("funding_rate")
    if meaningful_sign_flip(current_funding, previous_funding, min_abs_each=0.005, min_span=0.02):
        return True
    if metric_changed(metrics.get("last_price"), previous.last_price, pct_threshold=0.06):
        return True
    if metric_changed(metrics.get("oi_value"), previous.oi_value, pct_threshold=0.25):
        return True

    old_cvd = tuple(cvd_direction(getattr(previous, f"cvd_{suffix}", None)) for suffix in ("30m", "1h", "2h"))
    new_cvd = thought_cvd_profile(metrics)
    if abs(sum(item == "up" for item in new_cvd) - sum(item == "up" for item in old_cvd)) >= 2:
        return True

    hours_since_push = thought_hours_since_push(previous)
    return hours_since_push is not None and hours_since_push >= THOUGHT_REPEAT_COOLDOWN_HOURS


def thought_structural_has_new_information(previous, metrics):
    if previous is None:
        return True
    if previous.direction != metrics["direction"]:
        return True
    if previous.signal_key != metrics["signal_key"]:
        return True
    # Watched symbols are narrative/structure trades. If a coin stays in the
    # same zone with the same judgement, normal 5-minute indicator jitter is not
    # a new idea and should not wake the user.
    if metric_changed(metrics.get("last_price"), previous.last_price, pct_threshold=0.06):
        return True
    if metric_changed(metrics.get("basis"), previous.basis, abs_threshold=0.50):
        return True
    previous_funding = previous.funding_rate
    current_funding = metrics.get("funding_rate")
    if current_funding is not None and previous_funding is not None:
        if meaningful_sign_flip(current_funding, previous_funding, min_abs_each=0.005, min_span=0.02):
            return True
    if metric_changed(current_funding, previous_funding, abs_threshold=0.15):
        return True
    if metric_changed(metrics.get("oi_value"), previous.oi_value, pct_threshold=0.25):
        return True
    if metric_changed(metrics.get("wall_qty"), previous.wall_qty, pct_threshold=0.80):
        return True
    if thought_cvd_profile(metrics) != tuple(cvd_direction(getattr(previous, f"cvd_{suffix}", None)) for suffix in ("30m", "1h", "2h")):
        old_up = sum(direction == "up" for direction in tuple(cvd_direction(getattr(previous, f"cvd_{suffix}", None)) for suffix in ("30m", "1h", "2h")))
        new_up = sum(direction == "up" for direction in thought_cvd_profile(metrics))
        if abs(new_up - old_up) >= 2:
            return True
    return False


def thought_ake_has_new_information(previous, metrics):
    if previous is None:
        return True
    if previous.direction != metrics["direction"]:
        return True
    if previous.signal_key != metrics["signal_key"]:
        return True
    previous_funding = previous.funding_rate
    current_funding = metrics.get("funding_rate")
    if meaningful_sign_flip(current_funding, previous_funding, min_abs_each=0.005, min_span=0.02):
        return True
    previous_basis = previous.basis
    current_basis = metrics.get("basis")
    if meaningful_sign_flip(current_basis, previous_basis, min_abs_each=0.10, min_span=0.30):
        return True
    if metric_changed(current_funding, previous_funding, abs_threshold=0.08):
        return True
    if metric_changed(current_basis, previous_basis, abs_threshold=0.25):
        return True
    if metric_changed(metrics.get("oi_value"), previous.oi_value, pct_threshold=0.12):
        return True
    if thought_cvd_profile(metrics) != tuple(cvd_direction(getattr(previous, f"cvd_{suffix}", None)) for suffix in ("30m", "1h", "2h")):
        return True
    return False


def thought_push_has_new_information(previous, metrics):
    if previous is None:
        return True
    if metrics.get("direction") in TURNOVER_THOUGHT_DIRECTIONS:
        return thought_turnover_has_new_information(previous, metrics)
    if previous.direction != metrics["direction"]:
        return True
    if previous.signal_key != metrics["signal_key"]:
        return True

    hours_since_push = thought_hours_since_push(previous)
    if hours_since_push is not None and hours_since_push < THOUGHT_REPEAT_COOLDOWN_HOURS:
        return thought_major_narrative_shift(previous, metrics)

    if metrics.get("symbol") == "AKE/USDT":
        return thought_ake_has_new_information(previous, metrics)
    return thought_structural_has_new_information(previous, metrics)


def thought_push_trigger_reason(previous, metrics):
    if previous is None:
        return "首次形成可推送结构"
    reasons = []
    if previous.direction != metrics["direction"]:
        reasons.append(f"方向改变：{previous.direction} → {metrics['direction']}")
    if previous.signal_key != metrics["signal_key"]:
        if previous.direction == metrics["direction"] and metrics.get("direction") in TURNOVER_THOUGHT_DIRECTIONS:
            previous_basis = previous.basis
            current_basis = metrics.get("basis")
            if previous_basis is not None and current_basis is not None and abs(current_basis) >= abs(previous_basis) + 0.20:
                reasons.append(f"基差绝对值继续扩大：{previous_basis:+.4f}% → {current_basis:+.4f}%")
        else:
            reasons.append(f"信号区间改变：{previous.signal_key} → {metrics['signal_key']}")
    previous_funding = previous.funding_rate
    current_funding = metrics.get("funding_rate")
    if meaningful_sign_flip(current_funding, previous_funding, min_abs_each=0.005, min_span=0.02):
        reasons.append("资金费率正负翻转")
    previous_basis = previous.basis
    current_basis = metrics.get("basis")
    if meaningful_sign_flip(current_basis, previous_basis, min_abs_each=0.10, min_span=0.30):
        reasons.append("基差正负翻转")
    if metric_changed(metrics.get("oi_value"), previous.oi_value, pct_threshold=0.25):
        reasons.append("持仓较上次变化达到25%")
    old_cvd = tuple(cvd_direction(getattr(previous, f"cvd_{suffix}", None)) for suffix in ("30m", "1h", "2h"))
    new_cvd = thought_cvd_profile(metrics)
    if abs(sum(item == "up" for item in new_cvd) - sum(item == "up" for item in old_cvd)) >= 2:
        reasons.append("至少两个观察窗口的CVD方向改变")
    if not reasons:
        reasons.append("超过重复抑制周期后结构仍成立")
    return "；".join(reasons)


def thought_push_reservation_key(previous, metrics):
    previous_version = previous.pushed_at.isoformat(timespec="microseconds") if previous and previous.pushed_at else "initial"
    source = "|".join([
        str(metrics.get("symbol") or ""),
        str(metrics.get("direction") or ""),
        str(metrics.get("signal_key") or ""),
        previous_version,
    ])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def acquire_thought_push_lock():
    """跨进程互斥；网站重启交叠时也只允许一个推送任务进入。"""
    if db.engine.dialect.name not in {"mysql", "mariadb"}:
        return None, True
    connection = db.engine.connect()
    acquired = connection.execute(text("SELECT GET_LOCK('arbitrage_thought_push', 0)")).scalar()
    return connection, acquired == 1


def release_thought_push_lock(connection):
    if connection is None:
        return
    try:
        connection.execute(text("SELECT RELEASE_LOCK('arbitrage_thought_push')"))
    finally:
        connection.close()


def upsert_thought_push_snapshot(symbol, metrics):
    item = ThoughtPushSnapshot.query.filter_by(symbol=symbol).first()
    if item is None:
        item = ThoughtPushSnapshot(symbol=symbol, direction=metrics["direction"], signal_key=metrics["signal_key"])
        db.session.add(item)
    for key, value in metrics.items():
        setattr(item, key, value)
    item.pushed_at = datetime.now()
    item.updated_at = datetime.now()


def t_micro_line(micro, label, key):
    item = (micro or {}).get(key) or {}
    return (
        f"近{label}：价格 {lark_plain_value(item.get('price_change'), 2, '%')}｜"
        f"持仓 {lark_plain_value(item.get('oi_change'), 2, '%')}｜"
        f"多空人数比 {lark_plain_value(item.get('ratio_change'), 2, '%')}｜"
        f"CVD {lark_compact_number(item.get('cvd'))}｜"
        f"放量 {lark_plain_value(item.get('volume_ratio'), 2, 'x')}"
    )


def thought_lark_t_message(analysis, direction):
    symbol = analysis["symbol"]
    micro = analysis.get("micro_validation") or {}
    last = analysis.get("last") or micro.get("current")
    if direction == "t_bounce_long":
        header = "方向：<font color='cus-bull'>●●● 🟦⬆️ 看涨 / 底部反抽启动</font>"
        title = "T思路盯盘：0.0040-0.0041 横盘后的向上异动"
        judgement = (
            "判断：T 长时间在 0.0040-0.0041 附近横住，现在如果 5MIN/15MIN 出现价格上拐、CVD 转正、成交量放大，"
            "同时持仓没有快速塌掉，我会先按你说的“主力先拉一段再继续出货”处理。这里的重点不是追长期多，"
            "而是抓底部反抽的短线多单。"
        )
        key_zone = "关键位：站上 0.00412 后才算离开横盘箱体；上方先看 0.00428-0.00438，若放量冲到这里但价格不继续推进，就准备平多观察反手空。"
    else:
        header = "方向：<font color='cus-bear'>●●● 🟦⬇️ 平多 / 反手空观察</font>"
        title = "T思路盯盘：反抽停滞，重新观察诱多后下跌"
        judgement = (
            "判断：如果价格已经离开 0.0040-0.0041 底部箱体，但 5MIN/15MIN 开始涨不动，或者放量却不再创新高，"
            "同时 CVD 转弱、多空人数比回升、负资费/负基差仍没有修复，我会按你说的“反复上涨出货并诱多”处理。"
            "这时不再恋多，优先平多，随后看是否能转为空单。"
        )
        key_zone = "关键位：反抽停滞区先看 0.00428-0.00445；若跌回 0.00412 下方，说明反抽失败概率升高，下方重新看 0.0040，甚至更低。"
    return "\n".join([
        header,
        title,
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(last)}｜BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}｜BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        t_micro_line(micro, "5MIN", "5m"),
        t_micro_line(micro, "15MIN", "15m"),
        t_micro_line(micro, "30MIN", "30m"),
        judgement,
        key_zone,
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def thought_lark_era_message(analysis, direction):
    symbol = analysis["symbol"]
    micro = analysis.get("micro_validation") or {}
    validation = analysis.get("validation") or {}
    if direction == "era_squeeze_probe":
        header = "方向：<font color='cus-bull-soft'>●● 🟦⬆️ 反抽萌芽 / 逼空燃料观察</font>"
        title = "ERA思路盯盘：短周期开始转强，长周期仍未反转"
        judgement = "判断：空头拥挤开始转化为反抽燃料，30MIN/1H结构改善，但这只是小拉或逼空萌芽，尚不能叫大级别反转。重点验证主动买入能否持续，以及价格能否离开0.0714低点区。"
        key_zone = "关键位：先收回0.0721-0.0723；放量站上0.0726才升级为逼空确认。跌回0.0714下方且CVD重新转负，萌芽信号失效。"
    elif direction == "era_squeeze_confirmed":
        header = "方向：<font color='cus-bull'>●●● 🟦⬆️ 看涨 / 逼空反弹确认</font>"
        title = "ERA思路盯盘：短中周期共振，开始执行小拉剧本"
        judgement = "判断：价格、CVD、持仓与人数比开始形成短周期犄角，且已站上第一确认位。这里先按逼空/诱多上涨段处理，不提前假设它会反转成长期主升。"
        key_zone = "关键位：站稳0.0726后先看0.0732-0.0738；只有继续放量并保持新仓增长，才观察0.0757。上涨时若持仓快速下降，更像空头回补，持续性要降级。"
    else:
        header = "方向：<font color='cus-bear'>●●● 🟦⬇️ 平多 / 诱多结束观察</font>"
        title = "ERA思路盯盘：反弹停滞，准备验证重新下跌"
        judgement = "判断：ERA完成一段逼空/诱多反弹后，短周期价格开始滞涨、CVD转弱，仓位或人数结构也不再支持上涨。此时才把你的‘拉高诱多再跌’剧本升级为可执行的重新看空观察。"
        key_zone = "关键位：0.0732-0.0738是第一停滞观察带；若冲高失败后跌回0.0726下方，转弱确认度提高，下方重新看0.0721和0.0714。"
    return "\n".join([
        header, title,
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}｜BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}｜BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        t_micro_line(micro, "5MIN", "5m"),
        t_micro_line(micro, "15MIN", "15m"),
        t_micro_line(micro, "30MIN", "30m"),
        thought_window_line(validation, "1H", "1h"),
        thought_window_line(validation, "2H", "2h"),
        judgement, key_zone,
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def thought_lark_turnover_body(analysis, header, title, judgement, key_note):
    symbol = analysis["symbol"]
    micro = analysis.get("micro_validation") or {}
    validation = analysis.get("validation") or {}
    funding = analysis.get("funding_rate")
    basis = analysis.get("basis")
    observed_basis = analysis.get("turnover_basis_observed")
    recent_high = micro.get("recent_high")
    recent_low = micro.get("recent_low")
    return "\n".join([
        header,
        title,
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}｜BN当前基差：{lark_plain_value(basis, 4, '%')}｜本轮捕捉基差：{lark_plain_value(observed_basis, 4, '%')}｜BN资费：{lark_plain_value(funding, 4, '%')}",
        t_micro_line(micro, "5MIN", "5m"),
        t_micro_line(micro, "15MIN", "15m"),
        thought_window_line(validation, "30MIN", "30m"),
        thought_window_line(validation, "1H", "1h"),
        thought_window_line(validation, "2H", "2h"),
        f"判断：{judgement.removeprefix('判断：')}",
        f"关键位：近1H高低区约 {lark_price_value(recent_low)} - {lark_price_value(recent_high)}。{key_note}",
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def thought_lark_soon_message(analysis, direction):
    if direction == "soon_basis_negative_watch":
        return thought_lark_turnover_body(
            analysis,
            "方向：<font color='orange'>●● 🟠⬇️ 正溢价破坏 / 基差转负观察</font>",
            "SOON思路盯盘：持续正基差首次稳定转负",
            "判断：SOON此前持续正基差、正资费，基差稳定转负代表原有合约溢价主升结构开始受损。这里只是第一道破坏信号，继续检查资费是否跟随，以及价格、CVD与持仓是否同步转弱。",
            "跌破近端低点且反抽收不回，换手做空确认增强；若迅速修复基差并放量突破近端高点，按主升延续处理。",
        )
    if direction == "soon_funding_follow_watch":
        return thought_lark_turnover_body(
            analysis,
            "方向：<font color='cus-bear'>●● 🔴⬇️ SOON资费跟随转负</font>",
            "SOON思路盯盘：正溢价破坏开始传导到资金费",
            "判断：SOON的基差先转负后，资费也跟随转负，原来持续正资费的主升结构进一步受损。仍需等待价格滞涨、主动卖出和仓位退出确认，不能只凭负资费追空。",
            "反抽无法收回近端高点、CVD继续走弱时偏空增强；重新恢复正基差、正资费并创新高则否定换手假设。",
        )
    return thought_lark_turnover_body(
        analysis,
        "方向：<font color='cus-bear'>●●● 🔴⬇️ SOON换手转弱 / 做空确认观察</font>",
        "SOON思路盯盘：正溢价主升结构破坏后出现滞涨卖出",
        "判断：SOON在基差转负后，短周期价格滞涨、CVD转弱，并出现持仓退出、人数比追多或异常放量之一，已经比单纯贴水更接近主升后的换手做空窗口。",
        "跌破近端低点且反抽无法收回才提高执行权重；重新放量创新高则立即降级空头假设。",
    )


def thought_lark_zama_message(analysis, direction):
    if direction == "zama_deep_basis_watch":
        return thought_lark_turnover_body(
            analysis,
            "方向：<font color='orange'>●●● 🟠⬇️ ZAMA深负基差 / 换手加速观察</font>",
            "ZAMA思路盯盘：盘中基差进入 -2% 深贴水区",
            "判断：ZAMA本轮捕捉基差已到 -2% 以下，符合你提出的放量换手路径。深贴水说明合约端卖压或空头需求骤增，但在资费尚未明显跟负、价格与偏多犄角尚未同步破坏前，不能只凭基差直接追空；先判断这是短时插针、逼空前压盘，还是持续换手。",
            "连续样本维持 -2% 以下、成交量放大且价格/CVD/持仓转弱，换手做空权重上升；若基差快速修复且犄角延续，则保留继续上拉的反证。",
        )
    if direction == "zama_deep_basis_funding_follow":
        return thought_lark_turnover_body(
            analysis,
            "方向：<font color='cus-bear'>●●● 🔴⬇️ ZAMA深负基差 / 资费开始跟负</font>",
            "ZAMA思路盯盘：深贴水正在向资金费传导",
            "判断：ZAMA基差已进入 -2% 以下，资费也开始转负，说明合约端的换手压力不再只是瞬时价格偏离。若同时出现放量、价格滞涨、CVD转弱或持仓退出，才更接近主升后的换手阶段；偏多犄角仍完整时仍需防反向逼空。",
            "继续比较基差扩大速度与资费跟随速度；价格跌破近端低点且反抽收不回时提高做空确认，结构修复则立即降级。",
        )
    if direction == "zama_negative_basis_bullish_horn":
        return thought_lark_turnover_body(
            analysis,
            "方向：<font color='cus-bull'>●●● 🔵⬆️ 负基差但犄角偏多 / 防继续拉升</font>",
            "ZAMA思路盯盘：合约贴水与偏多犄角形成背离",
            "判断：ZAMA虽已稳定负基差，但30MIN/1H/4H仍以持仓增加、人数比下降、CVD上涨为主，说明新多与主动买入结构尚未破坏。负基差当前更像合约滞后或空头压力，不能直接解释成主力已经换手出货，先防继续上拉。",
            "若价格、持仓和CVD继续创新高，维持偏多反证；只有CVD转弱、持仓退出并跌破近端低点，才把负基差升级为换手做空信号。",
        )
    if direction == "zama_funding_follow_watch":
        return thought_lark_turnover_body(
            analysis,
            "方向：<font color='orange'>●● 🟠⬇️ ZAMA资费跟随转负 / 背离收敛</font>",
            "ZAMA思路盯盘：基差先负后资费开始跟随",
            "判断：ZAMA的资费已经开始跟随负基差，但是否完成换手仍取决于偏多犄角是否破坏。若价格、持仓和CVD依然强，继续防逼空；若它们同步转弱，做空权重才上升。",
            "重点看偏多犄角是否失效；跌破近端低点且持仓/CVD转弱才算结构确认。",
        )
    if direction == "zama_turnover_short_ready":
        return thought_lark_turnover_body(
            analysis,
            "方向：<font color='cus-bear'>●●● 🔴⬇️ ZAMA犄角破坏 / 换手做空观察</font>",
            "ZAMA思路盯盘：负基差开始与价格和资金面转弱共振",
            "判断：ZAMA不再只是负基差，短周期已出现滞涨、CVD转弱，并伴随持仓退出、人数比追多或异常放量，说明原偏多犄角正在破坏，才进入真正的换手做空观察。",
            "跌破近端低点且反抽无力提高确认；若持仓和CVD快速修复，撤销做空升级。",
        )
    return thought_lark_turnover_body(
        analysis,
        "方向：<font color='orange'>●● 🟠⬇️ ZAMA负基差独立观察</font>",
        "ZAMA思路盯盘：基差为负，但尚无完整换手确认",
        "判断：ZAMA的负基差已经稳定存在，但它不能套用SOON的正溢价破坏逻辑。继续独立检查自身的价格、持仓、人数比和CVD，等待偏多结构真正失效。",
        "若偏多犄角继续，防继续拉升；若主动卖出增强并跌破近端低点，再升级做空。",
    )


def ake_wall_bucket_line(wall):
    buckets = wall.get("buckets") or []
    live_qty = sum((item.get("qty") or 0) for item in buckets)
    if not buckets:
        return "盘口墙：暂无深度快照"
    if live_qty <= 0 and wall.get("reference_buckets"):
        buckets = wall.get("reference_buckets") or []
        parts = []
        for item in buckets:
            parts.append(f"{item.get('level', 0):.4f}参考≈{lark_compact_number(item.get('qty'))}")
        visible_high = wall.get("visible_high")
        suffix = f"；当前公开深度最高卖档仅到 {lark_price_value(visible_high)}，暂未覆盖墙区" if visible_high else ""
        return "盘口墙：" + " ｜ ".join(parts) + suffix
    parts = []
    for item in buckets:
        parts.append(f"{item.get('level', 0):.4f}≈{lark_compact_number(item.get('qty'))}")
    return "盘口墙：" + " ｜ ".join(parts)


def thought_lark_ake_wall_message(analysis, direction):
    symbol = analysis["symbol"]
    wall = analysis.get("orderbook_wall") or {}
    validation = analysis.get("validation") or {}
    breakout = direction == "ake_wall_breakout"
    if direction == "ake_wall_test":
        header = "方向：<font color='cus-bull'>● 🔵⬆️ 观察转强 / 试探 0.0020</font>"
        title = "AKE思路盯盘：价格开始试探卖墙下沿"
        judgement = "判断：AKE 已经靠近 0.0020 卖墙下沿，这里不是等 0.0022 才看，而是进入主力选择方向的区域。如果 CVD 转正、持仓不掉、量能放大，说明可能开始吃墙；如果反复碰 0.0020 不上去，后面就要转为墙下失败观察。"
        key_zone = "关键位：0.0020 是进入压力区的第一道门；站上后看 0.0021/0.0022，站不上且跌回 0.00195 下方，说明试探失败概率升高。"
    elif direction == "ake_wall_spike_retest":
        header = "方向：<font color='cus-bull'>● 🔵⬆️ 假设观察 / 0.002 插针回踩</font>"
        title = "AKE思路盯盘：插上 0.002 后快速回踩"
        judgement = "判断：这是你的当前 AKE 剧本假设，不作为固定规则。你的核心想法是：主力已经握有大量多单，需要吸引空单作为对手盘，可能通过 0.002 附近来回波动吸空，也可能直接插上 0.0022 吃掉空单后再大量换手。我先按这个剧本盯验证，不直接判死。"
        key_zone = "验证：重新站上 0.0020，假设增强；进入 0.0020-0.0022，观察是否小波动吸空；站上 0.0022，升级为强势逼空。证伪：跌破 0.00190 且价格/CVD/持仓同步转弱，降级为墙下失败。"
    elif direction == "ake_wall_zone_strength":
        header = "方向：<font color='cus-bull'>● 🔵⬆️ 看涨转强 / 进入卖墙区</font>"
        title = "AKE思路盯盘：已进入 0.0020-0.0022 卖墙区"
        judgement = "判断：价格进入 0.0020-0.0022 区间后，如果价格、CVD 或持仓开始配合转强，就说明主力可能在吃墙或逼退挂空/卖压。这里的重点是看能不能持续推进，而不是机械等 0.0022。"
        key_zone = "关键位：0.0020 上方是转强观察；0.0022 是卖墙上沿确认；如果冲进墙区后迅速跌回 0.0020 下方，说明吃墙失败，需要降级。"
    elif breakout:
        header = "方向：<font color='cus-bull'>● 🔵⬆️ 看涨 / 卖墙上沿突破</font>"
        title = "AKE思路盯盘：0.0022 上沿被突破"
        judgement = "判断：0.0022 上沿被突破，说明 0.0020-0.0022 这段压力已经被明显消化；在 CVD 与持仓没有同步转弱前，更像是继续逼退空头/逼空。后续重点看是否继续放量、基差是否继续正向打开、爆仓是否跟随出现。"
        key_zone = "关键位：0.0022 上方站稳才算卖墙上沿突破；0.0023 上方若还能稳住，说明墙区被真正消化。跌回 0.0020 下方，突破信号降级。"
    else:
        header = "方向：<font color='cus-bear'>● 🔵⬇️ 看跌 / 墙下反复失败</font>"
        title = "AKE思路盯盘：0.0020 试探失败后转弱"
        judgement = "判断：如果 AKE 反复试探 0.0020 却上不去，随后跌回 0.00195 下方，同时价格和 CVD 连续转弱，就更像是在墙下派发或诱多失败。这里要防止把压力墙误读成必然逼空，主力不吃墙时，墙本身反而会成为出货天花板。"
        key_zone = "关键位：重新站上 0.0020，才恢复卖墙区观察；站不上且继续走弱，下方先看 0.0019/0.00185。"
    effective_wall_qty = wall.get("wall_qty") or sum((item.get("qty") or 0) for item in (wall.get("reference_buckets") or []))
    effective_wall_notional = wall.get("wall_notional") or sum((item.get("notional") or 0) for item in (wall.get("reference_buckets") or []))
    return "\n".join([
        header,
        title,
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}，BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}，BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        ake_wall_bucket_line(wall),
        f"卖墙合计：{lark_compact_number(effective_wall_qty)} AKE，约 {lark_compact_number(effective_wall_notional)} USDT；0.0018-0.0020 买盘约 {lark_compact_number(wall.get('near_bid_qty'))} AKE",
        thought_window_line(validation, "30MIN", "30m"),
        thought_window_line(validation, "1H", "1h"),
        thought_window_line(validation, "2H", "2h"),
        judgement,
        key_zone,
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def thought_lark_message(analysis, direction):
    if direction in {"t_bounce_long", "t_bounce_stall_short"}:
        return thought_lark_t_message(analysis, direction)
    if direction in {"ake_wall_test", "ake_wall_spike_retest", "ake_wall_zone_strength", "ake_wall_breakout", "ake_wall_rejection"}:
        return thought_lark_ake_wall_message(analysis, direction)
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


def thought_direction_badge(direction):
    bullish = direction in {"bullish", "bullish_db_watch", "era_squeeze_probe", "era_squeeze_confirmed"}
    distribution = direction in {"distribution", "era_bounce_stall_short"}
    if bullish:
        return "方向：<font color='cus-bull'>●●●</font> 🟦⬆️ 看涨/转强"
    if distribution:
        return "方向：<font color='cus-bear'>●●●</font> 🟦⬇️ 派发预警"
    return "方向：<font color='cus-bear'>●●●</font> 🟦⬇️ 看跌/转弱"


def thought_window_line(validation, label, key):
    item = validation.get(key) or {}
    price = item.get("price_change")
    oi = item.get("oi_change")
    ratio = item.get("ratio_change")
    cvd = item.get("cvd")
    volume_ratio = item.get("volume_ratio")
    return (
        f"近{label}：价格 {lark_plain_value(price, 2, '%')}，"
        f"持仓 {lark_plain_value(oi, 2, '%')}，"
        f"多空人数比 {lark_plain_value(ratio, 2, '%')}，"
        f"CVD {lark_compact_number(cvd)}，"
        f"放量 {lark_plain_value(volume_ratio, 2, 'x')}"
    )


def thought_structure_summary(analysis, direction):
    if direction == "tlm_trap_short":
        validation = analysis.get("validation") or {}
        windows = [validation.get(key) or {} for key in ("30m", "1h", "2h")]
        valid_windows = [item for item in windows if item.get("price_change") is not None]
        if not valid_windows:
            return (
                "TLM 当前按诱多转弱剧本盯盘：价格在 0.0018 附近震荡，BN 资费仍深度为负、BN 基差仍负向打开，"
                "与用户观察到的“持仓增加、人数比小幅上升、CVD 小幅走低”组合方向一致，偏空假设暂时保留。"
                "本轮 30MIN/1H/2H 细分窗口未完整同步，所以不做过度确认；后续若价格跌回震荡下沿且 CVD/人数比继续配合，按偏空确认推送；若放量站稳上沿并修复资费/基差，按反转信号推送。"
            )
        cvd_down = sum((item.get("cvd") or 0) < 0 for item in windows)
        ratio_up = sum((item.get("ratio_change") or 0) > 0 for item in windows)
        price_up = sum((item.get("price_change") or 0) > 0 for item in windows)
        return (
            "TLM 当前按诱多转弱剧本盯盘：前面 7月19日-7月20日放量拉升后持续下跌，说明高位多单可能没完全出完，同时主力也可能在高点埋伏空单。"
            f"现在短线有反抽迹象（{price_up} 个窗口价格为正），但多空人数比有 {ratio_up} 个窗口上升、CVD 有 {cvd_down} 个窗口偏卖出，"
            "再叠加 BN 资费被打到负值、基差偏负，这更像给多头制造入场动力后的诱多，而不是健康主升。若反抽放量不涨或跌回入场下方，空头逻辑增强；若重新放量站稳压力带并修复负资费/负基差，做空逻辑降级。"
        )
    if direction == "tlm_reversal":
        validation = analysis.get("validation") or {}
        windows = [validation.get(key) or {} for key in ("30m", "1h", "2h")]
        cvd_up = sum((item.get("cvd") or 0) > 0 for item in windows)
        ratio_ok = sum((item.get("ratio_change") or 0) <= 0.2 for item in windows)
        price_up = sum((item.get("price_change") or 0) > 0.6 for item in windows)
        return (
            "TLM 出现做空假设的反证信号："
            f"{price_up} 个窗口价格重新转强，{cvd_up} 个窗口 CVD 转为主动买入，{ratio_ok} 个窗口多空人数比没有继续明显追多。"
            "这说明 0.0018 附近的震荡可能不再只是诱多派发，若负资费和负基差继续修复，做空逻辑需要降级；如果转强后放量不涨、CVD 再次转负，则反转信号作废，重新回到诱多转弱观察。"
        )
    validation = analysis.get("validation") or {}
    windows = [validation.get(key) or {} for key in ("30m", "1h", "2h")]
    valid = [item for item in windows if item]
    if not valid:
        if direction == "bullish_db_watch":
            return "BN 资费和 BN 基差仍为正，合约端仍保持溢价；只要回调不放量砸穿，先按多头结构未破观察。"
        if direction == "bearish_db_watch":
            return "BN 资费与基差继续偏负，反弹如果无法修复负基差，更像下跌途中的诱多修复。"
        return "当前结构以盘口快照为主，重点看资费、基差、持仓和成交量是否继续同向恶化。"
    price_up = sum((item.get("price_change") or 0) > 0 for item in valid)
    oi_up = sum((item.get("oi_change") or 0) > 0 for item in valid)
    ratio_down = sum((item.get("ratio_change") or 0) < 0 for item in valid)
    cvd_up = sum((item.get("cvd") or 0) > 0 for item in valid)
    vol_hot = any((item.get("volume_ratio") or 0) >= 2.0 for item in valid)
    if direction in {"bullish", "bullish_db_watch"}:
        if price_up >= 2 and oi_up >= 2 and ratio_down >= 2 and cvd_up >= 2:
            base = "30MIN/1H/2H 至少两档同时满足价格走强、持仓增加、人数比下跌、CVD 为正，属于更完整的犄型偏多结构。"
        elif price_up >= 2 and oi_up >= 2:
            base = "价格和持仓已经偏强，但人数比或 CVD 没有完全跟上，属于多头结构观察，不适合无脑追，只能看回踩后是否继续放量承接。"
        else:
            base = "短线有转强迹象，但多周期共振还不完整，需要等 30MIN、1H、2H 继续同步。"
        if vol_hot:
            base += " 量能已经放大，优势是主力推动更明显，风险是接近压力带时如果放量不涨，就要防高位换手。"
        return base
    if direction == "distribution":
        return "出现放量、资费恶化、基差打开的组合，更接近主力高位换手/派发预警；这里重点不是立刻追空，而是看放量后是否跌破关键防守区。"
    if price_up >= 2 and oi_up >= 2 and ratio_down <= 1:
        return "价格反弹但人数比没有形成犄型反向下跌，若同时资费/基差偏弱，更像反弹诱多，不是健康主升。"
    return "多周期结构开始转弱，若反抽无法重新站回关键区间，空头结构会继续增强。"


def thought_key_zone(analysis):
    support = analysis.get("support")
    resistance = analysis.get("resistance")
    last = analysis.get("last")
    if support and resistance:
        defend_low = support
        defend_high = support * 1.018
        attack_low = resistance * 0.985
        attack_high = resistance * 1.018
        return (
            f"关键区间：防守带 {lark_price_value(defend_low)}-{lark_price_value(defend_high)}；"
            f"突破观察带 {lark_price_value(attack_low)}-{lark_price_value(attack_high)}。"
            "防守带放量跌破，结构降级；突破带放量站稳，才算继续转强。"
        )
    if last:
        return (
            f"关键区间：现价 {lark_price_value(last)} 附近先看承接；"
            f"短线防守带 {lark_price_value(last * 0.97)}-{lark_price_value(last * 0.985)}，"
            f"突破观察带 {lark_price_value(last * 1.018)}-{lark_price_value(last * 1.04)}。"
        )
    return "关键区间：等待下一轮价格快照更新后确认。"


def thought_lark_message(analysis, direction):
    if direction in {"t_bounce_long", "t_bounce_stall_short"}:
        return thought_lark_t_message(analysis, direction)
    if direction in {"ake_wall_test", "ake_wall_spike_retest", "ake_wall_zone_strength", "ake_wall_breakout", "ake_wall_rejection"}:
        return thought_lark_ake_wall_message(analysis, direction)
    if analysis.get("source") == "db_fallback" or direction in {"bullish_db_watch", "bearish_db_watch"}:
        return thought_lark_db_fallback_message(analysis, direction)
    symbol = analysis["symbol"]
    validation = analysis.get("validation") or {}
    title_map = {
        "bullish": "结构转强确认",
        "bearish": "转弱观察",
        "reversal": "涨势反转预警",
        "distribution": "高位派发预警",
        "tlm_trap_short": "诱多转弱做空盯盘",
        "tlm_reversal": "做空假设反证",
    }
    return "\n".join([
        thought_direction_badge(direction),
        f"{symbol.split('/')[0]}思路盯盘：{title_map.get(direction, '结构观察')}",
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}，BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}，BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        thought_window_line(validation, "30MIN", "30m"),
        thought_window_line(validation, "1H", "1h"),
        thought_window_line(validation, "2H", "2h"),
        f"判断：{thought_structure_summary(analysis, direction)}",
        thought_key_zone(analysis),
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def thought_lark_db_fallback_message(analysis, direction):
    context = thought_market_context(analysis.get("symbol"))
    if context:
        analysis = {**analysis, **{key: value for key, value in context.items() if value is not None}}
    symbol = analysis["symbol"]
    bullish = direction == "bullish_db_watch"
    judgement = (
        "BN 资费仍为正、BN 基差仍为正，且合约成交量明显大于现货成交量，说明合约端多头结构还没被破坏。这里不追涨，只盯回调是否缩量、基差是否继续为正、资费是否突然转负。"
        if bullish
        else "BN 资费偏负、BN 基差负向打开，若反弹仍无法修复负基差和负资费，更像诱多后的下行延续。这里重点看反抽能否站回压力带。"
    )
    return "\n".join([
        thought_direction_badge(direction),
        f"{symbol.split('/')[0]}思路盯盘：结构观察",
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}，BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}，BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        f"合约持仓：{lark_compact_number(analysis.get('oi_value'))}，合约成交额：{lark_compact_number(analysis.get('futures_volume'))}，现货成交额：{lark_compact_number(analysis.get('spot_volume'))}",
        f"合约/现货量比：{lark_plain_value(analysis.get('futures_spot_volume_ratio'), 2, 'x')}，开差：{lark_plain_value(analysis.get('open_spread'), 4, '%')}，平差：{lark_plain_value(analysis.get('close_spread'), 4, '%')}",
        f"判断：{judgement}",
        thought_key_zone(analysis),
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


AKE_STRUCTURE_DIRECTIONS = {
    "ake_main_long_unwind_watch",
    "ake_above_wall_distribution_watch",
    "ake_above_wall_bull_weakening",
    "ake_above_wall_bull_continue",
    "ake_above_wall_new_range",
    "ake_wall_zone_weakening",
    "ake_wall_failed_watch",
}


def thought_lark_ake_structure_message(analysis, direction):
    symbol = analysis["symbol"]
    validation = analysis.get("validation") or {}
    last = analysis.get("last")
    previous = ThoughtPushSnapshot.query.filter_by(symbol=symbol).first()
    previous_oi = previous.oi_value if previous else None
    previous_price = previous.last_price if previous else None

    if direction == "ake_main_long_unwind_watch":
        header = "方向：<font color='cus-bear'>● 🔵↘️ 看涨明显减弱 / 主力平多观察</font>"
        title = "AKE思路盯盘：CVD上涨不能单独看多，持仓和人数比已经给出反证"
        judgement = (
            "判断：这次不再只看价格区间。AKE 当前更关键的是资金面结构：持仓下降，同时多空人数比上升。"
            "按照你的资金面框架，这更像“散户平空、主力平多”，即使 CVD 还在上涨，也可能只是高位主动买入承接或换手，不能把它简单解释成主力继续扫货。"
            "如果负资费、负基差继续存在，说明多头剧本正在从“拉高逼空”转向“高位换手/出货风险”。"
        )
        key_zone = "新区间：旧墙区 0.0020-0.0022 已经变成回踩区；现在重点看 0.00225-0.00245。若持仓继续降、人数比继续升，即使价格横住，也按看涨减弱处理；只有持仓重新增加、人数比回落、资费/基差修复，才恢复看涨。"
    elif direction == "ake_above_wall_distribution_watch":
        header = "方向：<font color='cus-bear'>● 🔵↘️ 看涨减弱 / 高位换手观察</font>"
        title = "AKE思路盯盘：已经脱离旧卖墙区，不能继续只看 0.002-0.0022"
        judgement = "判断：AKE 已经站到旧墙区上方，但现在 BN资费转负、BN基差也转负，说明之前“正资费+正基差拉合约”的多头支撑在减弱。这不等于立刻看空，但更像进入高位换手/派发观察阶段；如果持仓继续下降，就不能再按单纯逼空剧本处理。"
        key_zone = "新区间：旧墙区 0.0020-0.0022 变成下方回踩区；当前重点看 0.00225-0.00245 能否守住。若跌回 0.0022 下方且资费/基差继续为负，看涨剧本降级。"
    elif direction == "ake_above_wall_bull_weakening":
        header = "方向：<font color='cus-bear'>● 🔵↘️ 看涨减弱 / 持仓结构走弱</font>"
        title = "AKE思路盯盘：价格还在高位，但持仓或主动性开始减弱"
        judgement = "判断：价格虽然还在 0.0022 上方，但持仓下降、CVD走弱、负资费或负基差中出现至少一项。这里不能继续用同一句“吸空后继续拉”解释所有波动，应该观察是否进入换手区。"
        key_zone = "新区间：0.0022 是多头结构的第一道防线；0.0024 附近是新高位压力/换手区。若重新放量站稳 0.0024 且持仓不再下降，才恢复偏强。"
    elif direction == "ake_above_wall_bull_continue":
        header = "方向：<font color='cus-bull'>● 🔵⬆️ 看涨增强 / 墙上延续</font>"
        title = "AKE思路盯盘：旧卖墙被消化后，多头结构仍在"
        judgement = "判断：价格在 0.0022 上方，且资费/基差仍支持合约多头，持仓或CVD没有明显破坏。这种才属于真正的墙上延续，后续重点看是否放量继续抬高底部，而不是只看一根插针。"
        key_zone = "新区间：0.0022-0.0024 是新的回踩确认区；站稳 0.0024 后，上方再看 0.0026/0.0028。跌回 0.0022 下方则降级观察。"
    elif direction == "ake_wall_zone_weakening":
        header = "方向：<font color='cus-bear'>● 🔵↘️ 墙区减弱 / 防止假突破</font>"
        title = "AKE思路盯盘：还在旧墙区，但资金结构已经变差"
        judgement = "判断：价格还在 0.0020-0.0022，但资费、基差或持仓开始走弱。这时墙区不再只代表逼空，也可能代表主力在墙区附近换手。要看能不能重新站上 0.0022。"
        key_zone = "关键区间：0.0020 是墙区下沿，0.0022 是墙区上沿；上不去且资金结构继续变差，就按失败处理。"
    elif direction == "ake_wall_failed_watch":
        header = "方向：<font color='cus-bear'>● 🔵↘️ 墙下失败 / 多头剧本破坏</font>"
        title = "AKE思路盯盘：跌回旧墙区下方，先降级看涨剧本"
        judgement = "判断：如果价格跌回 0.0020 下方，同时资费/基差转弱或持仓下降，就说明旧卖墙没有被持续消化。这里不能继续把所有回调都解释成诱空，必须承认多头剧本被削弱。"
        key_zone = "关键区间：重新站上 0.0020 才恢复墙区观察；站不上则看 0.0019/0.00185 的承接。"
    else:
        header = "方向：<font color='cus-watch'>● 🔵↔️ 新区间观察 / 等待确认</font>"
        title = "AKE思路盯盘：离开旧卖墙区后，进入新价格区间"
        judgement = "判断：价格已经离开最初的 0.0020-0.0022 剧本区间，但资金结构没有给出单边确认。现在要重新定义区间，而不是继续重复旧判断。"
        key_zone = "新区间：下方看 0.0022 是否从压力变支撑；上方看 0.0024-0.0026 是否形成新压力。"

    oi_line = f"持仓：{lark_compact_number(analysis.get('oi_value'))}"
    if previous_oi and analysis.get("oi_value"):
        oi_change = (analysis.get("oi_value") - previous_oi) / previous_oi * 100
        oi_line += f"（较上次推送 {lark_plain_value(oi_change, 2, '%')}）"
    price_line = f"价格：{lark_price_value(last)}"
    if previous_price and last:
        price_change = (last - previous_price) / previous_price * 100
        price_line += f"（较上次推送 {lark_plain_value(price_change, 2, '%')}）"

    return "\n".join([
        header,
        title,
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"{price_line}，BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}，BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        f"{oi_line}，合约成交额：{lark_compact_number(analysis.get('futures_volume'))}，现货成交额：{lark_compact_number(analysis.get('spot_volume'))}",
        f"开差：{lark_plain_value(analysis.get('open_spread'), 4, '%')}，平差：{lark_plain_value(analysis.get('close_spread'), 4, '%')}",
        thought_window_line(validation, "30MIN", "30m"),
        thought_window_line(validation, "1H", "1h"),
        thought_window_line(validation, "2H", "2h"),
        judgement,
        key_zone,
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def thought_lark_message(analysis, direction):
    if direction in {"t_bounce_long", "t_bounce_stall_short"}:
        return thought_lark_t_message(analysis, direction)
    if direction in {"soon_basis_negative_watch", "soon_funding_follow_watch", "soon_turnover_short_ready"}:
        return thought_lark_soon_message(analysis, direction)
    if direction in {"zama_basis_negative_watch", "zama_funding_follow_watch", "zama_turnover_short_ready", "zama_negative_basis_bullish_horn", "zama_deep_basis_watch", "zama_deep_basis_funding_follow"}:
        return thought_lark_zama_message(analysis, direction)
    if direction in {"era_squeeze_probe", "era_squeeze_confirmed", "era_bounce_stall_short"}:
        return thought_lark_era_message(analysis, direction)
    if direction in AKE_STRUCTURE_DIRECTIONS:
        return thought_lark_ake_structure_message(analysis, direction)
    if direction in {"ake_wall_test", "ake_wall_spike_retest", "ake_wall_zone_strength", "ake_wall_breakout", "ake_wall_rejection"}:
        return thought_lark_ake_wall_message(analysis, direction)
    if analysis.get("source") == "db_fallback" or direction in {"bullish_db_watch", "bearish_db_watch"}:
        return thought_lark_db_fallback_message(analysis, direction)
    symbol = analysis["symbol"]
    validation = analysis.get("validation") or {}
    title_map = {
        "bullish": "结构转强确认",
        "bearish": "转弱观察",
        "reversal": "涨势反转预警",
        "distribution": "高位派发预警",
        "tlm_trap_short": "诱多转弱做空盯盘",
        "tlm_reversal": "做空假设反证",
    }
    return "\n".join([
        thought_direction_badge(direction),
        f"{symbol.split('/')[0]}思路盯盘：{title_map.get(direction, '结构观察')}",
        f"时间：{datetime.now(SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"价格：{lark_price_value(analysis.get('last'))}，BN基差：{lark_plain_value(analysis.get('basis'), 4, '%')}，BN资费：{lark_plain_value(analysis.get('funding_rate'), 4, '%')}",
        thought_window_line(validation, "30MIN", "30m"),
        thought_window_line(validation, "1H", "1h"),
        thought_window_line(validation, "2H", "2h"),
        f"判断：{thought_structure_summary(analysis, direction)}",
        thought_key_zone(analysis),
        f"COINGLASS：https://www.coinglass.com/tv/zh/Binance_{symbol.replace('/', '')}",
    ])


def send_thought_analysis_push(only_symbols=None):
    lock_connection = None
    try:
        lock_connection, acquired = acquire_thought_push_lock()
        if not acquired:
            return False
        return send_thought_analysis_push_locked(only_symbols=only_symbols)
    finally:
        release_thought_push_lock(lock_connection)


def send_thought_analysis_push_locked(only_symbols=None):
    webhook = os.getenv("LARK_THOUGHT_ANALYSIS_WEBHOOK", "").strip()
    if not webhook:
        return False
    sections = []
    push_records = []
    for analysis in thought_watch_snapshots(only_symbols=only_symbols):
        if analysis.get("source") not in {"live", "db_fallback"}:
            continue
        direction = thought_push_direction(analysis)
        if not direction:
            continue
        signal_key = thought_signal_key(analysis, direction)
        signal_repeat_window = 0 if analysis["symbol"] == "AKE/USDT" else 6 * 3600
        existing = LarkPushState.query.filter_by(channel="thought_analysis", symbol=analysis["symbol"], signal_key=signal_key).first()
        if existing and signal_repeat_window and (datetime.now() - existing.pushed_at).total_seconds() < signal_repeat_window:
            continue
        metrics = thought_push_metrics(analysis, direction, signal_key)
        previous_snapshot = ThoughtPushSnapshot.query.filter_by(symbol=analysis["symbol"]).first()
        if not thought_push_has_new_information(previous_snapshot, metrics):
            continue
        message = thought_lark_message(analysis, direction)
        event = ThoughtPushEvent(
            symbol=analysis["symbol"],
            direction=direction,
            signal_key=signal_key,
            reservation_key=thought_push_reservation_key(previous_snapshot, metrics),
            trigger_reason=thought_push_trigger_reason(previous_snapshot, metrics),
            status="reserved",
            snapshot_json=json.dumps(metrics, ensure_ascii=False, default=str),
            message_text=message,
        )
        db.session.add(event)
        sections.append(message)
        push_records.append((existing, analysis, signal_key, metrics, event))
    if not sections:
        return False

    # 先把“准备发送”及比较快照提交。这样即使 webhook 已送达后网站立刻重启，
    # 新进程也能看到本次信号已经被占位，不会把相同判断再发一遍。
    reserved_at = datetime.now()
    try:
        for existing, analysis, signal_key, metrics, event in push_records:
            if existing:
                existing.pushed_at = reserved_at
            else:
                db.session.add(LarkPushState(channel="thought_analysis", symbol=analysis["symbol"], signal_key=signal_key, pushed_at=reserved_at))
            event.reserved_at = reserved_at
            upsert_thought_push_snapshot(analysis["symbol"], metrics)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return False

    payload = lark_trend_card(sections)
    try:
        request_obj = Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "ArbiScope/1.0"})
        with urlopen(request_obj, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not (result.get("code", 0) == 0 or result.get("StatusCode", 0) == 0):
            for *_, event in push_records:
                event.status = "rejected"
                event.error_text = str(result)[:500]
            db.session.commit()
            return False
        sent_at = datetime.now()
        for *_, event in push_records:
            event.status = "sent"
            event.sent_at = sent_at
        db.session.commit()
        return True
    except Exception as exc:
        # 网络异常可能发生在 Lark 已接收之后，因此保留预占位，不自动重发。
        # 事件状态可用于之后人工核对，避免“响应丢失 → 无限重试”的重复消息。
        db.session.rollback()
        try:
            event_ids = [record[-1].id for record in push_records if record[-1].id]
            for event in ThoughtPushEvent.query.filter(ThoughtPushEvent.id.in_(event_ids)).all():
                event.status = "uncertain"
                event.error_text = str(exc)[:500]
            db.session.commit()
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
        "trade_side": "观察",
        "trade_status": "底部反抽盯盘 / 先多后空",
        "entry": t["entry"],
        "entry_time": t["entry_time"],
        "exit": None,
        "exit_time": None,
        "last": t["last"],
        "profit_pct": None,
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
        "thought_summary": "T 现在切换为两阶段盯盘：第一阶段看 0.0040-0.0041 横盘后的向上异动，若 5MIN/15MIN 出现放量、CVD 转强、持仓不塌，就提醒短线做多；第二阶段等反抽到 0.00428-0.00445 一带后，如果放量不涨、CVD 转弱、多空人数比回升、负资费/负基差没有修复，就提醒平多并观察反手做空。",
        "user_mistakes": ["风险点：负资费会增加做空持仓成本，若价格横盘不跌，不能只靠负资费继续硬扛。"],
        "assistant_mistakes": ["需要持续验证 0.0045 是否真正反抽失败，不能只因为前期出货迹象就忽略二次吸筹可能。"],
        "thesis_win_rate": {"wins": 1, "losses": 0, "pending": 1, "rate": 100.0, "note": "T 的 0.0045 附近诱多后偏空思路已被后续回落初步验证；当前新的底部反抽再转空剧本仍在继续观察。"},
        "my_thesis": "你的主线思路：T 在 7 月 11 日到 7 月 12 日出现持仓涨、多空人数比跌、CVD 涨的典型犄型走势，币价从约 0.003 拉到约 0.006，接近翻倍。拉升过程中放量拉基差，资费跟随基差走，在结算时顶满，且结算周期从 4H 变成 1H。你认为这是主力出货换手信号。后续价格缩量下跌，中间几次小反弹依然维持 1H 结算，多头每小时可以收资费，更像诱多。当前这波反弹虽然 CVD 在涨，但多空人数比也跟着持仓一起涨，你认为这不是健康主升，而是更多账户追多、主力借反弹布置空单；所以 0.0045 附近做空胜率更大。",
        "assistant_thesis": "我的验证思路：你的空单逻辑是连贯的，核心不是单纯看跌，而是看到前期主升后的出货换手特征：高位放量、基差打开、资费极端化、结算周期缩短、随后缩量下跌。对于 T，CVD 上涨不能机械解释为看多；如果 CVD 涨的同时 OI 上升、多空人数比也上升，说明反弹中有更多账户站到多头一侧，可能给主力空单提供对手盘。若价格不能有效站回 0.0045-0.0047，且负资费、负基差持续，反弹更偏诱多。风险点是：若价格放量站稳 0.0047 上方，基差修复、负资费缓和，并且后续下跌无法延续，空单逻辑才需要降级。",
        "challenge_points": [
            "需要警惕：负资金费会让做空方付费，如果价格长时间横盘不跌，持仓成本会变高。",
            "反证条件：价格放量重新站稳 0.0045 上方，CVD 转正，基差从 -1% 以下快速修复，说明这次可能不是诱多而是重新吸筹。",
            "执行重点：不要只因为资费负就加空，必须看价格是否反抽失败、量能是否衰减、持仓是否配合。"
        ],
        "validation_view": "T 当前按 0.0040-0.0041 底部箱体盯盘。若价格站上 0.00412，且 5MIN/15MIN CVD 转强、成交量放大、持仓没有快速下降，就按短线反抽做多提醒；若反抽到 0.00428-0.00445 后放量不涨、CVD 转弱或多空人数比回升，同时负资费/负基差没有修复，就按平多并反手做空观察提醒。",
        "take_profit": [
            "短线多单第一目标：0.00428-0.00438，一旦放量冲高但价格推进变慢，优先落袋，不恋战。",
            "若 0.00438 上方继续放量站稳且 CVD、持仓仍同步增强，可以保留小仓观察 0.00445-0.0047，但这不是主线目标。",
        ],
        "stop_loss": [
            "做多失败条件：站上 0.00412 后又跌回 0.00405-0.00408，且 CVD 转弱，说明反抽启动失败。",
            "反手空条件：反抽后放量不涨、CVD 转弱、多空人数比回升、负资费/负基差没有修复；跌回 0.00412 下方后，下方重新看 0.0040 甚至更低。",
        ],
        "review_notes": [
            "新增做空思路：T 0.0045 附近建立空单。",
            "核心依据：前期犄型主升后出现基差、资费、结算周期异常，疑似出货换手；后续缩量下跌与多次小反弹更像诱多。",
            "后续验证：重点跟踪价格是否反抽失败；若 CVD 上涨但 OI 与多空人数比也同步上涨，需要优先按诱多/主力布空假设观察，而不是机械看多。",
            "2026-07-18 新增剧本：0.0040-0.0041 横盘先抓向上反抽；上涨停滞后，不恋多，按主力反复拉高出货和诱多的路径观察反手空。",
        ],
    }


def thought_tlm_item(tlm):
    short_profit = percent_delta(tlm["entry"], tlm["last"]) if tlm.get("last") and tlm.get("entry") else None
    entry = tlm.get("entry") or 0.0018855
    last = tlm.get("last") or entry
    support = tlm.get("support") or min(entry, last) * 0.94
    resistance = tlm.get("resistance") or max(entry, last) * 1.08
    return {
        "symbol": tlm["symbol"],
        "trade_side": "做空",
        "trade_status": "诱多转弱盯盘",
        "entry": entry,
        "entry_time": tlm["entry_time"],
        "exit": None,
        "exit_time": None,
        "last": tlm["last"],
        "profit_pct": short_profit,
        "realized_profit_pct": None,
        "support": support,
        "resistance": resistance,
        "oi_value": tlm["oi_value"],
        "oi_change_pct": tlm["oi_change_pct"],
        "ratio_value": tlm["ratio_value"],
        "ratio_change_pct": tlm["ratio_change_pct"],
        "cvd": tlm["cvd"],
        "change_30m": tlm["change_30m"],
        "change_4h": tlm["change_4h"],
        "funding_rate": tlm["funding_rate"],
        "basis": tlm["basis"],
        "validation": tlm.get("validation") or {},
        "source": tlm["source"],
        "screenshot_url": None,
        "thought_summary": "TLM 持续做空观察：当前在 0.0018 附近震荡，但持仓上涨不少，多空人数比也小幅上涨，CVD 仍小幅走低。这个组合更像反抽横盘里的诱多/空头换手，而不是健康反转。若后续价格跌回震荡下沿、CVD 继续走低、人数比继续上升，则确认偏空；若价格放量站稳上沿、CVD 转正且资费/基差修复，则提醒做空假设失效。",
        "user_mistakes": [
            "需要注意：负资费对空单不友好，若价格长时间横盘不跌，持仓成本会侵蚀利润。",
            "不能只因为前面放量上涨后下跌就认定后面必跌，必须让 CVD、人数比、量能和关键位继续验证。",
        ],
        "assistant_mistakes": [
            "我需要避免把短线反抽直接看成继续下跌，必须区分诱多反抽和二次吸筹反转。",
            "若 CVD 转正、负资费快速修复、价格放量站稳压力带，我要及时提醒你降低空头假设权重。"
        ],
        "thesis_win_rate": {"wins": 0, "losses": 0, "pending": 1, "rate": 0.0, "note": "TLM 为新增实盘做空思路，等待后续盯盘验证。"},
        "my_thesis": "你的主线思路：TLM 在 7月19日-7月20日左右放量上涨很多，随后一直走下跌。一方面可能是主力手上的多单没有完全平完，另一方面主力可能已经在高点埋伏空单。当前价格在 0.0018 附近震荡，但持仓上涨了不少，多空人数比也小幅上涨，CVD 还在小幅走低；你认为这说明反抽过程中追多/接多账户增加，但主动卖出没有消失，从数据结构来看偏利空。你希望系统继续盯住：一旦确认偏空，或者出现反转信号，就立刻推送。",
        "assistant_thesis": "我的验证思路：我认可你这次的偏空观察，但会把它拆成可验证条件。偏空确认不是“横盘就空”，而是 0.0018 附近震荡时持仓继续增加、人数比继续回升、CVD 继续为负或走低，同时价格无法站稳震荡上沿；这代表新增仓位没有推动价格有效上行，反而可能是在给空头换手或诱多。反转条件也必须同步盯：如果价格放量站上震荡上沿，CVD 转正，人数比不再追高，负资费/负基差修复，就说明你的空头假设需要降级。",
        "challenge_points": [
            "空头优势：负资费 + 负基差 + CVD 走跌 + 多空人数比上升，符合反抽诱多后的做空观察。",
            "风险点：负资费会让空单付费，若主力选择横盘磨人，空单成本和心理压力都会变高。",
            "反证条件：放量站稳压力带、CVD 转正、负资费/负基差修复，必须降低做空权重，不能死扛。"
        ],
        "validation_view": "TLM 当前按 0.0018 附近震荡偏空盯盘：若价格无法站稳震荡上沿，持仓继续增加、人数比继续小幅上升、CVD 继续走低，就按诱多转弱/空头换手确认推送；若价格放量站稳上沿，CVD 转正，且资费或基差明显修复，则按反转信号推送。",
        "take_profit": [
            f"第一观察目标：{support:.8f} 附近。理由是近端支撑/回落低位，若放量跌破，说明诱多反抽失败概率增加。",
            f"第二目标：{entry * 0.90:.8f}-{entry * 0.94:.8f}。只有跌破第一支撑后反抽无力，才看这里，不提前幻想一口气砸穿。",
            "若下跌过程中资费继续极负但价格跌不动，要小心主力横盘磨空，不要只看方向不看成本。",
        ],
        "stop_loss": [
            f"结构止损：{resistance * 0.995:.8f}-{resistance * 1.015:.8f} 放量站稳，并且 CVD 转正、负资费/负基差修复，则做空逻辑降级。",
            f"硬风控参考：若价格回到入场上方约 4%-6% 且没有快速跌回，即 {entry * 1.04:.8f}-{entry * 1.06:.8f}，需要主动复核，不要被负资费诱导硬扛。",
        ],
        "review_notes": [
            "2026-07-22 新增：用户在 TLM 当前点位建立空单，入场先按本地快照 0.0018855 记录；若实际成交不同，需要校准。",
            "用户判断：7月19日-7月20日放量拉升后持续下跌，说明主力可能高位换手并埋伏空单；当前短拉更像诱多而非新主升。",
            "关键观察：多空人数比上涨、CVD 下跌、资费极负、基差偏负。如果这几项继续共振，空头逻辑增强。",
            "反证记录：若价格放量站稳压力带、CVD 转正、负资费/负基差修复，必须记录为做空假设缺陷，而不是继续按诱多解释。",
            "2026-07-23 新增：价格在 0.0018 附近震荡，用户观察到持仓上涨不少、多空人数比小幅上涨、CVD 小幅走低；当前按偏空结构继续盯盘，确认或反转都要推送。"
        ],
    }


def thought_turnover_item(item):
    coin = item["symbol"].split("/")[0]
    last = item.get("last")
    funding = item.get("funding_rate")
    basis = item.get("basis")
    both_negative = funding is not None and basis is not None and funding < 0 and basis < 0
    if both_negative:
        status = "资费跟随转负观察"
    elif basis is not None and basis < 0:
        status = "基差先行转负"
    else:
        status = "等待换手做空"
    support = item.get("support") or (last * 0.94 if last else None)
    resistance = item.get("resistance") or (last * 1.06 if last else None)
    return {
        "symbol": item["symbol"],
        "trade_side": "等待做空",
        "trade_status": status,
        "entry": None,
        "entry_time": item.get("entry_time"),
        "exit": None,
        "exit_time": None,
        "last": last,
        "profit_pct": None,
        "realized_profit_pct": None,
        "support": support,
        "resistance": resistance,
        "oi_value": item.get("oi_value"),
        "oi_change_pct": item.get("oi_change_pct"),
        "ratio_value": item.get("ratio_value"),
        "ratio_change_pct": item.get("ratio_change_pct"),
        "cvd": item.get("cvd"),
        "change_30m": item.get("change_30m"),
        "change_4h": item.get("change_4h"),
        "funding_rate": funding,
        "basis": basis,
        "validation": item.get("validation") or {},
        "source": item.get("source"),
        "screenshot_url": None,
        "thought_summary": f"{coin} 主升后换手做空观察：以BN基差由正转负作为先行提醒，随后观察资费是否跟随下移；不再要求资费低于-1%。真正做空还要等待价格滞涨、CVD转弱以及持仓/人数结构出现换手。",
        "user_mistakes": [
            "两天上涨、CVD与持仓走强说明值得关注，但多空人数比会随观察窗口变化，不能把所有周期都概括成同步上涨。",
            "负资费会让空单承担资金成本，也可能说明空头拥挤；它是换手证据之一，不是固定数值开仓条件。",
        ],
        "assistant_mistakes": [
            "不能把极负资费机械解释成下跌确认；需要区分主力换手出货和利用拥挤空头继续逼空。",
            "必须保留反证：价格继续放量创新高、CVD保持上涨且持仓继续增加时，做空窗口尚未成熟。",
        ],
        "thesis_win_rate": {"wins": 0, "losses": 0, "pending": 1, "rate": 0.0, "note": f"{coin} 为新增换手做空假设，等待真实盯盘验证。"},
        "my_thesis": f"你的思路：{coin}主升进入换手时，基差会先由正转负，资金费再慢慢跟随。系统应先盯基差，同时持续观察币价、CVD、持仓、人数比和成交量，不应把资金费低于-1%设为硬要求。",
        "assistant_thesis": "我的验证思路：拆成基差先行转负、资费跟随转负、价格与资金结构确认三层。基差负责尽早发现，资费负责验证情绪传导；只有价格滞涨、CVD转弱，并伴随持仓退出、人数比追多或异常放量之一，才更像主力高位换手完成。",
        "challenge_points": [
            "不同周期的人数比可能方向不同；后续推送必须注明具体周期，不再笼统写成全部上涨。",
            "若基差转负但价格、CVD和持仓继续强势上破，先按短暂贴水或逼空延续处理，不急于追空。",
        ],
        "validation_view": "后台5分钟做完整结构检查；30秒检查基差是否稳定转负。基差先负即提醒，资费跟随转负时升级，随后验证滞涨、CVD、持仓和人数比是否构成换手做空。",
        "take_profit": ["目前没有开仓，不预设盈利目标；形成换手确认后再根据当时放量区、近端低点和持仓结构制定。"],
        "stop_loss": ["若做空观察后价格放量突破近端高点、CVD继续上升且持仓同步增长，按逼空延续处理，禁止机械追空。"],
        "review_notes": [
            f"2026-07-28：新增{coin}换手做空观察。",
            "监控重点：区分普通回调、暂时贴水、逼空延续与真正的高位换手。",
            "修正规则：BN基差稳定转负即提醒，不再等待资金费低于-1%；资费跟随转负和结构转弱分别作为后续升级。",
        ],
    }


def thought_soon_item(soon):
    item = thought_turnover_item(soon)
    item.update({
        "trade_status": "正溢价主升观察" if (soon.get("basis") or 0) >= 0 else item["trade_status"],
        "thought_summary": "SOON独立盯盘：它此前长期保持正基差、正资费，当前核心不是看见一点回调就猜出货，而是观察持续正溢价何时稳定破坏。基差转负是第一阶段，资费跟随和价格/CVD/持仓转弱是后续确认。",
        "my_thesis": "你的SOON思路：主升换手时基差会先转负，资费再跟随；系统要持续盯价格和资金面，等待真正换手后寻找做空机会。",
        "assistant_thesis": "我的SOON判断：当前仍以正溢价主升为基准。人数比与价格、持仓一起上涨说明追涨账户增加，既可能继续推升，也提高后期换手风险；只有正基差稳定破坏并与主动卖出共振，才升级做空。",
        "challenge_points": [
            "SOON的核心基准是持续正资费、正基差，不能套用ZAMA已经负基差的解释。",
            "人数比上涨表示更多账户偏多，后期若价格滞涨而持仓继续增加，才更像追多换手。",
            "基差靠近零但没有连续转负时，只是溢价收窄，不等于结构已经破坏。",
        ],
        "validation_view": "SOON单独验证正溢价主升是否破坏：基差连续转负→资费跟随→价格/CVD/持仓转弱；任何一步重新修复都降低做空权重。",
    })
    item["assistant_mistakes"] = [
        "2026-07-28 20:17 的‘换手转弱/做空确认观察’属于阶段判断错误：当时只放大了5MIN/15MIN回落与混合CVD，却没有让1H/2H仍成立的价格上涨、持仓增加、人数比下降犄角拥有否决权。",
        "当时基差仅约-0.12%，资费仍为+0.0337%，负基差幅度浅且没有传导到资费；它更可能是短时贴水或洗盘，证据不足以称为正溢价主升结构已经破坏。",
        "推送后SOON由约0.2527继续上涨到约0.278附近，基差与资费重新转正，直接反证了当时的做空升级时点。",
    ] + item.get("assistant_mistakes", [])
    item["review_notes"] = item.get("review_notes", []) + [
        "阶段复盘：SOON从20:17推送价约0.2527继续上涨，当前约0.278；这次应记录为‘做空升级过早’，而不是等待中的换手假设已经验证。",
        "规则修正：1H/2H若仍满足价格涨、持仓涨、人数比跌的犄角核心，即使CVD混合、短周期回调和浅负基差同时出现，也只能保留贴水观察，禁止升级做空确认。",
        "后续只有中周期犄角破坏、价格跌破近端结构且反抽失败，并伴随CVD持续转弱或持仓退出，才重新提高换手做空权重。",
    ]
    item["thesis_win_rate"] = {
        "wins": 0,
        "losses": 1,
        "pending": 1,
        "rate": 0.0,
        "note": "SOON长期等待换手的主假设仍待验证；20:17的阶段性做空升级已被后续上涨和正溢价修复判为错误。",
    }
    item["thought_summary"] = "SOON当前仍是主升延续优先。20:17的浅负基差与短周期转弱没有破坏1H/2H犄角，随后价格继续上涨且基差、资费修复，说明我的做空升级过早。" + item.get("thought_summary", "")
    return item


def thought_zama_item(zama):
    item = thought_turnover_item(zama)
    basis = zama.get("basis")
    funding = zama.get("funding_rate")
    item.update({
        "trade_status": "负基差 / 偏多犄角背离" if basis is not None and basis < 0 and (funding is None or funding >= 0) else item["trade_status"],
        "thought_summary": "ZAMA独立盯盘：负基差已经出现，但当前中周期仍可见价格与持仓上升、人数比下降、CVD上涨的偏多犄角。这里先解释为合约滞后或空头压力与强势结构并存，不能因为贴水就直接认定主力已经出货。",
        "my_thesis": "你的ZAMA要求：它的数据和走势与SOON不同，必须单独分析；继续盯基差、资费、币价、CVD、持仓、人数比和成交量，不能复制SOON结论。",
        "assistant_thesis": "我的ZAMA判断：当前最重要的是负基差与偏多犄角的背离。若价格、持仓和CVD继续上升，负基差可能成为逼空燃料；只有CVD转弱、持仓退出并跌破近端结构，负基差才升级为换手做空证据。",
        "challenge_points": [
            "负基差是早期异常，不等于趋势已经转空；ZAMA当前偏多犄角仍是强反证。",
            "1H价格小幅回落但持仓增加、人数比下降、CVD为正，更像承接或布多，尚非出货确认。",
            "资费若转负但偏多犄角未破坏，仍需先防继续拉升和逼空。",
        ],
        "validation_view": "ZAMA单独验证负基差是否演化为换手：先检查偏多犄角是否失效，再看资费跟随和价格破位；犄角延续时禁止机械追空。",
    })
    return item


def thought_era_item(era):
    return {
        "symbol": era["symbol"],
        "trade_side": "做空观察",
        "trade_status": "弱支撑 / 主动卖出观察",
        "entry": era["entry"],
        "entry_time": era["entry_time"],
        "exit": None,
        "exit_time": None,
        "last": era["last"],
        "profit_pct": None,
        "realized_profit_pct": None,
        "support": era["support"],
        "resistance": era["resistance"],
        "oi_value": era["oi_value"],
        "oi_change_pct": era["oi_change_pct"],
        "ratio_value": era["ratio_value"],
        "ratio_change_pct": era["ratio_change_pct"],
        "cvd": era["cvd"],
        "change_30m": era["change_30m"],
        "change_4h": era["change_4h"],
        "funding_rate": era["funding_rate"],
        "basis": era["basis"],
        "validation": era.get("validation") or {},
        "source": era["source"],
        "screenshot_url": None,
        "thought_summary": "ERA 当前按偏空观察处理：0.093 附近虽然是近端支撑，但这个区域前面没有明显放过大量，支撑质量偏弱；同时 CVD 持续走低、持仓缓慢下降，说明主动卖出占优，且多头仓位可能在慢慢撤退。若跌破 0.0937 后反抽无力，偏空确认度会提高。",
        "user_mistakes": [
            "需要注意：支撑位附近没放大量，确实代表支撑可能弱，但不能等同于一定会跌破；如果主力选择在弱支撑附近缩量吸筹，价格也可能先横住。",
            "CVD 走跌说明主动卖出占优，但如果价格没有继续下破，可能存在被动买盘承接；需要同时看跌破后的反抽质量。"
        ],
        "assistant_mistakes": [
            "我不能只因为 CVD 为负就机械看空；必须继续验证价格是否真的跌破支撑、持仓下降是否伴随价格失守，以及资金费/基差是否继续偏负。",
            "如果后续价格放量收回 0.0965-0.098 区域，同时 CVD 转正、持仓不再下滑，我要及时把偏空假设降级，而不是固执延续空头判断。"
        ],
        "thesis_win_rate": {"wins": 0, "losses": 0, "pending": 1, "rate": 0.0, "note": "ERA 为新增偏空观察样本，等待后续跌破/反抽失败或反证信号验证。"},
        "my_thesis": "你的主线思路：ERA 盘面偏空。你观察到价格下方近端支撑在 0.093 附近，但这个区域没有出现过明显大成交量，所以你认为支撑力比较弱；同时 CVD 一直走跌，代表主动卖出的人偏多；持仓也在缓步下跌，可能说明主力或多头资金正在缓慢平多。因此你倾向认为 ERA 后续继续下行的概率更高。",
        "assistant_thesis": "我的验证思路：我认同 ERA 当前偏空，但会把它拆成两个阶段。第一阶段是支撑测试：如果 0.0937 附近被放量跌破，随后反抽无法重新站回 0.0965-0.098，那么你的弱支撑判断基本被验证。第二阶段是下跌延续：如果跌破后 CVD 继续为负、持仓继续下降或反抽时持仓增加但价格不涨，就更像诱多/换手后继续下行。反证条件是价格重新站回 0.098 上方，CVD 转正，持仓不再下降，且资金费和基差开始修复。",
        "challenge_points": [
            "偏空证据：近端 30M/1H/2H 价格回落，CVD 为负，持仓下降，BN 资费和基差也偏负，说明合约端情绪和主动成交都不强。",
            "关键风险：0.093 附近如果跌不动，且成交量缩小，可能变成横盘吸筹或短线空头获利了结区，不能在支撑上方无脑追空。",
            "确认方式：跌破 0.0937 后的第一次反抽最重要；反抽无量、CVD 不修复、价格站不回 0.0965，才是更好的偏空确认。"
        ],
        "validation_view": "ERA 后续按 0.0937 支撑位验证：跌破并反抽失败，则偏空确认；若放量收回 0.0965-0.098 且 CVD 转正、持仓不再下滑，则看空假设降级。",
        "take_profit": [
            "若后续有空单入场，第一目标看 0.0937 下方的破位延续；跌破后如果反抽失败，再看更低一档。",
            "不建议在 0.093 附近已经贴近支撑时盲目追空，最好等跌破后的反抽确认，或者等反弹到 0.0965-0.098 附近走弱。"
        ],
        "stop_loss": [
            "若价格放量站回 0.098 上方，同时 CVD 转正、持仓不再下降，偏空逻辑需要降级。",
            "若 0.093 附近持续横住且卖盘衰竭，说明弱支撑判断可能被承接反证，不应继续机械看空。"
        ],
        "review_notes": [
            "2026-07-24 新增：用户提出 ERA 偏空思路，核心依据是 0.093 支撑量能不足、CVD 持续走跌、持仓缓慢下降。",
            "实时快照记录：ERA 最新价约 0.09405，近端支撑约 0.0937，近端压力约 0.10086；30M/1H/2H 价格均回落，CVD 为负，持仓从近端窗口看也在下降；BN 资费约 -0.07995%，BN 基差约 -0.7108%。",
            "后续验证：重点看 0.0937 是否跌破，以及跌破后的反抽是否无法站回 0.0965-0.098。"
        ],
    }


def early_trend_thought_item(row):
    strong = row.signal_type == "strong_focus"
    stage_note = f"【{row.stage_label}】{row.stage_reason}"
    return {
        "symbol": row.symbol, "trade_side": "观察", "trade_status": row.stage_label,
        "entry": None, "entry_time": row.created_at.strftime("%Y-%m-%d %H:%M"),
        "exit": None, "exit_time": None, "last": row.last_price, "profit_pct": None,
        "realized_profit_pct": None, "support": None, "resistance": None,
        "oi_value": None, "oi_change_pct": row.oi_change_5, "ratio_value": None,
        "ratio_change_pct": row.ratio_change_5, "cvd": row.cvd_change_5,
        "change_30m": row.price_change_5, "change_4h": None,
        "funding_rate": None, "basis": None,
        "validation": {"30m": {"price_change": row.price_change_5, "oi_change": row.oi_change_5,
            "ratio_change": row.ratio_change_5, "cvd": row.cvd_change_5, "volume": None}},
        "source": "live", "screenshot_url": None, "stage_label": row.stage_label,
        "thought_summary": stage_note,
        "user_mistakes": ["强看多假设不等于可以忽略追高风险；阶段越靠后，越需要防高位换手。"],
        "assistant_mistakes": ["不能再用统一结构分决定去留；本条由5根整体涨幅、CVD与犄角条件直接触发。"],
        "thesis_win_rate": {"wins": 0, "losses": 0, "pending": 1, "rate": 0.0, "note": "新信号待盯盘验证，暂不计胜负。"},
        "my_thesis": "你的判断：最近5根30分钟K线整体涨幅超过50%，同期CVD上涨，持仓上涨而多空人数比下跌时，属于强看多重点；同时要用前置K线判断启动阶段。",
        "assistant_thesis": ("我的判断：该组合已经确认趋势启动，不能再叫启动前；重点是区分首次点火、第二段加速和后段过热。" if strong else
            "我的判断：价格尚未明显启动，但持仓、人数比和CVD已经先行，属于启动前蓄势观察；仍需等待价格与成交量确认。"),
        "challenge_points": ["5根整体涨幅按第一根开盘到第五根收盘计算，不把每根百分比机械相加。"],
        "validation_view": f"5根价格 {row.price_change_5:+.2f}% · 持仓 {row.oi_change_5:+.2f}% · 人数比 {row.ratio_change_5:+.2f}% · 量能 {row.volume_ratio or 0:.2f}倍。",
        "take_profit": ["先作为重点分析对象，不自动给追涨入场；结合阶段、回踩承接和放量质量再制定交易计划。"],
        "stop_loss": ["若持仓快速回落、人数比明显回升、CVD转负且价格跌回点火区，本次强看多假设降级。"],
        "review_notes": [stage_note, "后续持续记录阶段判断是否正确，并按真实走势修正阶段模型。"],
    }


def thought_watchlist_payload():
    rows = ThoughtWatch.query.order_by(ThoughtWatch.active.desc(), ThoughtWatch.started_at.desc()).all()
    history_conditions = []
    for row in rows:
        start_bucket = int(row.started_at.replace(tzinfo=SHANGHAI_TZ).timestamp())
        condition = and_(FuturesPriceHistory.symbol == row.symbol, FuturesPriceHistory.bucket_at >= start_bucket)
        if not row.active and row.stopped_at:
            stop_bucket = int(row.stopped_at.replace(tzinfo=SHANGHAI_TZ).timestamp())
            condition = and_(condition, FuturesPriceHistory.bucket_at <= stop_bucket)
        history_conditions.append(condition)
    history_ranges = {}
    if history_conditions:
        history_ranges = {
            symbol: (low_price, high_price)
            for symbol, low_price, high_price in db.session.query(
                FuturesPriceHistory.symbol,
                func.min(FuturesPriceHistory.price),
                func.max(FuturesPriceHistory.price),
            ).filter(or_(*history_conditions)).group_by(FuturesPriceHistory.symbol).all()
        }
    latest_pushes = {}
    symbols = [row.symbol for row in rows]
    if symbols:
        for push in ThoughtPushEvent.query.filter(
            ThoughtPushEvent.symbol.in_(symbols), ThoughtPushEvent.status == "sent"
        ).order_by(ThoughtPushEvent.sent_at.desc()).all():
            latest_pushes.setdefault(push.symbol, push)
    items = []
    for row in rows:
        snapshot = thought_fast_snapshot(row.symbol)
        current_price = snapshot.get("last")
        effective_price = current_price if row.active else (row.stop_price or current_price)
        low_recorded, high_recorded = history_ranges.get(row.symbol, (None, None))
        start_price = row.start_price or effective_price
        if row.start_price is None and start_price:
            row.start_price = start_price
        high_price = high_recorded if high_recorded is not None else effective_price
        low_price = low_recorded if low_recorded is not None else effective_price
        latest_push = latest_pushes.get(row.symbol)
        end_at = datetime.now() if row.active else (row.stopped_at or datetime.now())
        items.append({
            "symbol": row.symbol,
            "active": row.active,
            "started_at": row.started_at.strftime("%Y-%m-%d %H:%M"),
            "stopped_at": row.stopped_at.strftime("%Y-%m-%d %H:%M") if row.stopped_at else None,
            "start_price": start_price,
            "current_price": current_price,
            "stop_price": row.stop_price,
            "change_pct": percent_delta(effective_price, start_price) if effective_price and start_price else None,
            "high_price": high_price,
            "high_change_pct": percent_delta(high_price, start_price) if high_price and start_price else None,
            "low_price": low_price,
            "low_change_pct": percent_delta(low_price, start_price) if low_price and start_price else None,
            "duration_seconds": max(int((end_at - row.started_at).total_seconds()), 0),
            "note": row.note,
            "last_push_at": latest_push.sent_at.strftime("%Y-%m-%d %H:%M:%S") if latest_push and latest_push.sent_at else None,
            "last_direction": latest_push.direction if latest_push else None,
        })
    db.session.commit()
    return items


@app.get("/api/thought-watchlist")
def thought_watchlist_api():
    items = thought_watchlist_payload()
    return jsonify({"items": items, "active_count": sum(item["active"] for item in items), "updated_at": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")})


def normalize_thought_watch_symbol(value):
    compact = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if compact.endswith("USDT"):
        compact = compact[:-4]
    return f"{compact}/USDT" if compact else None


@app.post("/api/thought-watchlist")
def add_thought_watch():
    body = request.get_json(silent=True) or {}
    canonical = normalize_thought_watch_symbol(body.get("symbol"))
    if not canonical:
        return jsonify({"ok": False, "error": "请输入币种名称，例如 SOON 或 SOON/USDT"}), 400
    if is_rwa_stock_pair(canonical):
        return jsonify({"ok": False, "error": "美股 RWA 代币暂不加入普通盯盘"}), 400
    context = thought_market_context(canonical) or {}
    current_price = context.get("last")
    if not current_price:
        try:
            ticker = get_json(
                "https://fapi.binance.com/fapi/v1/ticker/price?" + urlencode({"symbol": canonical.replace("/", "")}),
                timeout=3,
            )
            current_price = float(ticker.get("price", 0) or 0)
        except Exception:
            current_price = None
    if not current_price:
        return jsonify({"ok": False, "error": "Binance 暂未找到该币的 USDT 永续合约行情"}), 400
    now = datetime.now()
    row = ThoughtWatch.query.filter_by(symbol=canonical).first()
    if row and row.active:
        return jsonify({"ok": True, "symbol": canonical, "active": True, "already_active": True})
    if row:
        row.active = True
        row.started_at = now
        row.start_price = current_price
        row.stopped_at = None
        row.stop_price = None
        row.note = "由网页重新加入盯盘"
    else:
        row = ThoughtWatch(
            symbol=canonical,
            active=True,
            started_at=now,
            start_price=current_price,
            note="由网页手动加入盯盘",
        )
        db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "symbol": canonical, "active": True, "start_price": current_price})


@app.post("/api/thought-watchlist/<symbol>/state")
def update_thought_watch_state(symbol):
    canonical = normalize_thought_watch_symbol(symbol)
    row = ThoughtWatch.query.filter_by(symbol=canonical).first()
    if not row:
        return jsonify({"ok": False, "error": "未找到该盯盘币种"}), 404
    action = (request.get_json(silent=True) or {}).get("action", "stop")
    snapshot = thought_fast_snapshot(canonical)
    current_price = snapshot.get("last")
    now = datetime.now()
    if action == "resume":
        row.active = True
        row.started_at = now
        row.start_price = current_price
        row.stopped_at = None
        row.stop_price = None
        row.note = "由网页重新开始盯盘"
    else:
        row.active = False
        row.stopped_at = now
        row.stop_price = current_price
        row.note = "由网页停止盯盘"
        TURNOVER_DIRECTION_CANDIDATES.pop(canonical, None)
        TURNOVER_BASIS_STATE.pop(canonical, None)
    db.session.commit()
    return jsonify({"ok": True, "symbol": canonical, "active": row.active})


@app.get("/api/daily-report/thoughts")
def daily_report_thoughts():
    ake = thought_fast_snapshot("AKE/USDT")
    us = thought_fast_snapshot("US/USDT")
    t = thought_fast_snapshot("T/USDT")
    soon = thought_fast_snapshot("SOON/USDT")
    zama = thought_fast_snapshot("ZAMA/USDT")
    era = thought_fast_snapshot("ERA/USDT")
    ake_support = ake.get("support") or ake["entry"] * 0.99
    payload = {
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
                f"{ake_support:.8f} 附近：趋势失效必须结合量能和合约端证据，不能只看价格跌破；若跌破伴随放量砸盘、资费转负、基差/价差异常打开，才按主力出货处理。",
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
        }, thought_us_item(us), thought_t_item(t), thought_soon_item(soon), thought_zama_item(zama), thought_era_item(era)]
    }
    report_date = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    focus_rows = EarlyTrendSignal.query.filter_by(report_date=report_date).order_by(
        EarlyTrendSignal.stage_number.asc(), EarlyTrendSignal.price_change_5.desc()
    ).limit(20).all()
    existing = {item["symbol"]: item for item in payload["items"]}
    for row in focus_rows:
        if row.symbol in existing:
            existing[row.symbol]["stage_label"] = row.stage_label
            existing[row.symbol]["trade_status"] = row.stage_label
            existing[row.symbol]["thought_summary"] = f"【{row.stage_label}】{row.stage_reason} " + existing[row.symbol]["thought_summary"]
        else:
            payload["items"].append(early_trend_thought_item(row))
    watch_states = {row.symbol: row.active for row in ThoughtWatch.query.all()}
    for item in payload["items"]:
        if item["symbol"] in watch_states:
            item["watch_active"] = watch_states[item["symbol"]]
            if not watch_states[item["symbol"]]:
                item["trade_status"] = "已停止盯盘 / 历史复盘"
    return jsonify(payload)


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
        "market_thesis": "LAB 是“低成本参与者 + 高位合约套保 + 主力拉爆套保空单 + 解锁前砸回低位”的典型观察样本。用户补充的关键点是：LAB 拉到 20 美元以上时，市值已经接近或进入全市场前 20，这和项目叙事、生态、用户体量明显不匹配，普通支撑/阻力意义下降，更像人为制造市值并收割合约套保盘。",
        "watch_rules": [
            "活动/质押成本远低于二级价格，且上线后合约深度快速变厚，要默认存在大量套保空单。",
            "如果价格被拉到活动成本的数十倍，同时 OI 暴涨、资金费极端、基差失真，要警惕主力正在收割套保盘。",
            "如果市值排名短时间冲进全市场前列，但基本面和叙事承载不了，要把“支撑/阻力”降级，把“市值荒谬性、低成本筹码、套保爆仓、解锁倒计时”升为主线。",
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
    seconds = TREND_WINDOWS.get(period, 24 * 60 * 60)
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    target_bucket = now_bucket - seconds
    dual_rows = LatestDualFuturesSnapshot.query.all()
    dual_symbols = sorted({item.symbol for item in dual_rows if not is_rwa_stock_pair(item.symbol)})
    dual_history = {
        key[0]: price
        for key, price in nearest_trend_points(
            FuturesPriceHistory.query.filter(
                FuturesPriceHistory.symbol.in_(dual_symbols),
                FuturesPriceHistory.bucket_at.in_(trend_candidate_buckets([target_bucket])),
            ).all(),
            lambda item: (item.symbol,),
            [target_bucket],
        ).items()
    }
    seen_dual_symbols = set()
    for item in dual_rows:
        if item.symbol in seen_dual_symbols or is_rwa_stock_pair(item.symbol):
            continue
        current = volume = None
        if item.long_exchange == "Binance":
            current = (item.long_ask + item.long_bid) / 2
            volume = item.long_volume
        elif item.short_exchange == "Binance":
            current = (item.short_ask + item.short_bid) / 2
            volume = item.short_volume
        previous = dual_history.get(item.symbol)
        if current and previous:
            rows.append({"symbol": item.symbol, "change": (current - previous) / previous * 100, "price": current, "volume_24h": volume})
        seen_dual_symbols.add(item.symbol)
    rows = [row for row in rows if row["change"] is not None]
    def dedupe(items, reverse=False):
        best = {}
        for row in items:
            key = canonical_market_symbol(row["symbol"])
            current = best.get(key)
            if current is None or (row["change"] > current["change"] if reverse else row["change"] < current["change"]):
                best[key] = row
        return sorted(best.values(), key=lambda row: row["change"], reverse=reverse)[:50]
    return jsonify({"period": period, "updated_at": snapshot["updated_at"], "rising": dedupe(rows, True), "falling": dedupe(rows, False)})


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
        target_buckets = [now_bucket - seconds for seconds in TREND_WINDOWS.values()]
        points = {
            key[1]: price
            for key, price in nearest_trend_points(
                FuturesPriceHistory.query.filter_by(symbol=symbol).filter(FuturesPriceHistory.bucket_at.in_(trend_candidate_buckets(target_buckets))).all(),
                lambda item: (item.symbol,),
                target_buckets,
            ).items()
        }
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


def funding_trend_growth_metrics(symbol):
    raw_symbol = symbol.replace("/", "")
    try:
        oi_rows = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": "30m", "limit": 24}), timeout=5)
        kline_rows = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({"symbol": raw_symbol, "interval": "30m", "limit": 25}), timeout=5)[:-1]
    except Exception:
        return {"oi_change_12h": None, "volume_change_12h": None, "volume_ratio": None}
    oi_change = None
    volume_change = None
    volume_ratio = None
    try:
        if len(oi_rows) >= 2:
            first_oi = float(oi_rows[0].get("sumOpenInterestValue", 0) or 0)
            last_oi = float(oi_rows[-1].get("sumOpenInterestValue", 0) or 0)
            oi_change = percent_delta(last_oi, first_oi)
        if len(kline_rows) >= 12:
            early = sum(float(row[7]) for row in kline_rows[:12])
            recent = sum(float(row[7]) for row in kline_rows[-12:])
            volume_change = percent_delta(recent, early)
            prior_average = early / 12 if early else None
            recent_average = recent / 12 if recent else None
            volume_ratio = recent_average / prior_average if prior_average else None
    except Exception:
        pass
    return {"oi_change_12h": oi_change, "volume_change_12h": volume_change, "volume_ratio": volume_ratio}


def funding_trend_score(row, group):
    change_24h = group["rows"][0].get("change_24h")
    change_3d = group["rows"][0].get("change_3d")
    change_7d = group["rows"][0].get("change_7d")
    funding_current = row.get("funding_rate")
    funding_previous = row.get("funding_previous")
    funding_24h = row.get("funding_24h")
    funding_3d = row.get("funding_3d")
    score = 0.0
    score += max(0, min((change_24h or 0) / 30, 1)) * 16
    score += max(0, min((change_3d or 0) / 80, 1)) * 14
    score += max(0, min((change_7d or 0) / 150, 1)) * 10
    score += 16 if (funding_current or 0) > 0 else 0
    score += 10 if (funding_previous or 0) > 0 else 0
    score += max(0, min((funding_24h or 0) / 0.08, 1)) * 14
    score += max(0, min((funding_3d or 0) / 0.24, 1)) * 10
    score += 8 if (row.get("basis") or 0) > 0 else 0
    score += 8 if (row.get("open_spread") or 0) > 0 else 0
    return score


@app.get("/api/arbitrage-thinking/funding-trend")
def funding_trend_arbitrage():
    snapshot = load_latest_market_snapshot()
    if not snapshot:
        return jsonify({"updated_at": None, "items": []})
    enrich_price_changes(snapshot["symbols"])
    enrich_funding_statistics(snapshot["symbols"])
    candidates = []
    for group in snapshot["symbols"]:
        if is_rwa_stock_pair(group["symbol"]):
            continue
        best = None
        for row in group["rows"]:
            if (row.get("funding_rate") or 0) <= 0:
                continue
            item = {
                "symbol": group["symbol"],
                "long_exchange": row["long_exchange"],
                "short_exchange": "Binance",
                "funding_current": row.get("funding_rate"),
                "funding_previous": row.get("funding_previous"),
                "funding_24h": row.get("funding_24h"),
                "funding_3d": row.get("funding_3d"),
                "funding_7d": row.get("funding_7d"),
                "basis": row.get("basis"),
                "open_spread": row.get("open_spread"),
                "close_spread": row.get("close_spread"),
                "change_24h": group["rows"][0].get("change_24h"),
                "change_3d": group["rows"][0].get("change_3d"),
                "change_7d": group["rows"][0].get("change_7d"),
                "spot_volume": row.get("spot_volume"),
                "futures_volume": row.get("futures_volume"),
                "futures_open_interest": row.get("futures_open_interest"),
                "funding_interval": row.get("funding_interval_hours"),
            }
            item["score"] = funding_trend_score(row, group)
            if best is None or item["score"] > best["score"]:
                best = item
        if best and best["score"] >= 32:
            candidates.append(best)
    candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)[:40]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(funding_trend_growth_metrics, item["symbol"]): item for item in candidates[:24]}
        for future in as_completed(futures):
            item = futures[future]
            growth = future.result()
            item.update(growth)
            oi_growth = growth.get("oi_change_12h")
            volume_growth = growth.get("volume_change_12h")
            if oi_growth is not None:
                item["score"] += max(0, min(oi_growth / 30, 1)) * 8
            if volume_growth is not None:
                item["score"] += max(0, min(volume_growth / 80, 1)) * 6
            item["score"] = round(item["score"], 1)
    candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)[:20]
    for item in candidates:
        if item["score"] >= 78:
            item["level"] = "重点机会"
        elif item["score"] >= 58:
            item["level"] = "重点盯盘"
        else:
            item["level"] = "观察"
    return jsonify({"updated_at": snapshot["updated_at"], "items": candidates})


@app.get("/api/alerts")
def alerts():
    all_events = AlertEvent.query.order_by(AlertEvent.created_at.desc()).limit(800).all()
    # 旧版拉升报警没有连续采样确认凭证，不能再作为可靠信号展示。
    all_events = [item for item in all_events if (item.alert_type == "basis_threshold" or item.alert_type.startswith("rapid_") or "确认后的" in item.message) and not is_rwa_stock_pair(item.symbol)]
    grouped_events = {}
    for item in all_events:
        grouped_events.setdefault(item.symbol, []).append(item)
    grouped_events = dict(list(grouped_events.items())[:80])
    latest_events = [items[0] for items in grouped_events.values()]
    active_events = [item for item in latest_events if (datetime.now() - item.created_at).total_seconds() <= 120]
    tracking = [item for item in BasisTracking.query.filter_by(resolved_at=None).order_by(BasisTracking.max_abs_basis.desc()).limit(50).all() if not is_rwa_stock_pair(item.symbol)]
    event_symbols = list(grouped_events)
    dual_context = {
        (item.symbol, item.long_exchange, item.short_exchange): item
        for item in LatestDualFuturesSnapshot.query.filter(LatestDualFuturesSnapshot.symbol.in_(event_symbols)).all()
    } if event_symbols else {}
    market_context = {}
    if event_symbols:
        for item in LatestMarketSnapshot.query.filter(LatestMarketSnapshot.symbol.in_(event_symbols)).all():
            market_context.setdefault(item.symbol, item)

    def alert_context(item):
        if item.strategy == "futures_futures":
            latest = dual_context.get((item.symbol, item.long_exchange, item.short_exchange))
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
        latest = market_context.get(item.symbol)
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


def latest_validation_price(symbol):
    candle = TradeValidationCandle.query.filter_by(symbol=symbol, interval=TRADE_VALIDATION_INTERVAL).order_by(TradeValidationCandle.bucket_at.desc()).first()
    if candle:
        return candle.close
    history = FuturesPriceHistory.query.filter_by(symbol=symbol).order_by(FuturesPriceHistory.bucket_at.desc()).first()
    if history:
        return history.price
    spot = LatestMarketSnapshot.query.filter_by(symbol=symbol).first()
    if spot:
        return (spot.short_bid + spot.short_ask) / 2
    dual = LatestDualFuturesSnapshot.query.filter_by(symbol=symbol, short_exchange="Binance").first()
    if dual:
        return (dual.short_bid + dual.short_ask) / 2
    return None


def validation_pnl(plan, price):
    if price is None or plan.status == "planned":
        return 0.0
    ref_price = plan.exit_price if plan.status == "closed" and plan.exit_price else price
    if plan.direction == "long":
        return (ref_price - plan.entry_price) / plan.entry_price * plan.stake_usdt * plan.leverage
    return (plan.entry_price - ref_price) / plan.entry_price * plan.stake_usdt * plan.leverage


def refresh_validation_plan(plan, price):
    if price is None or plan.status == "closed":
        return
    now = datetime.now()
    if plan.status == "planned":
        triggered = price >= plan.entry_price if plan.direction == "long" else price <= plan.entry_price
        if triggered:
            plan.status = "open"
            plan.opened_at = now
    if plan.status != "open":
        return
    if plan.direction == "long":
        if price <= plan.stop_price:
            plan.status, plan.exit_price, plan.exit_reason, plan.closed_at = "closed", price, "stop", now
        elif price >= plan.take_profit_2:
            plan.status, plan.exit_price, plan.exit_reason, plan.closed_at = "closed", price, "take_profit", now
    else:
        if price >= plan.stop_price:
            plan.status, plan.exit_price, plan.exit_reason, plan.closed_at = "closed", price, "stop", now
        elif price <= plan.take_profit_2:
            plan.status, plan.exit_price, plan.exit_reason, plan.closed_at = "closed", price, "take_profit", now


def replay_validation_plan(plan, candles):
    """按K线时间顺序回放趋势验证，避免先止损后又显示止盈的假剧情。"""
    if not candles:
        refresh_validation_plan(plan, latest_validation_price(plan.symbol))
        return []
    is_short = plan.direction == "short"
    entry_idx = None
    events = []
    tp1_recorded = False
    final_status = "planned"
    opened_at = closed_at = None
    exit_price = None
    exit_reason = None
    # A newly-created plan must never be replayed against older cached candles.
    # If an older buggy replay wrote opened_at before created_at, ignore it and
    # start from created_at so the chart cannot "time travel" into a fake trade.
    replay_from = plan.created_at
    if plan.opened_at and (not plan.created_at or plan.opened_at >= plan.created_at - timedelta(seconds=PRICE_HISTORY_BUCKET_SECONDS)):
        replay_from = plan.opened_at
    for idx, candle in enumerate(candles):
        high, low = float(candle.high), float(candle.low)
        bucket_time = datetime.fromtimestamp(candle.bucket_at)
        if replay_from and bucket_time < replay_from - timedelta(seconds=PRICE_HISTORY_BUCKET_SECONDS):
            continue
        if entry_idx is None:
            entered = low <= plan.entry_price if is_short else high >= plan.entry_price
            if not entered:
                continue
            entry_idx = idx
            opened_at = bucket_time
            final_status = "open"
            events.append({"type": "entry", "idx": idx, "price": plan.entry_price, "label": "买入" if not is_short else "开空"})

        # 同一根K线内无法知道先后，趋势验证采用保守规则：止损优先于止盈。
        stop_hit = high >= plan.stop_price if is_short else low <= plan.stop_price
        tp2_hit = low <= plan.take_profit_2 if is_short else high >= plan.take_profit_2
        tp1_hit = low <= plan.take_profit_1 if is_short else high >= plan.take_profit_1
        if stop_hit:
            final_status = "closed"
            closed_at = bucket_time
            exit_price = plan.stop_price
            exit_reason = "stop"
            events.append({"type": "stop", "idx": idx, "price": plan.stop_price, "label": "止损"})
            break
        if tp1_hit and not tp1_recorded:
            tp1_recorded = True
            events.append({"type": "tp1", "idx": idx, "price": plan.take_profit_1, "label": "止盈1"})
        if tp2_hit:
            final_status = "closed"
            closed_at = bucket_time
            exit_price = plan.take_profit_2
            exit_reason = "take_profit"
            events.append({"type": "tp2", "idx": idx, "price": plan.take_profit_2, "label": "止盈2"})
            break

    plan.status = final_status
    plan.opened_at = opened_at
    plan.closed_at = closed_at
    plan.exit_price = exit_price
    plan.exit_reason = exit_reason
    return events


def sync_trade_validation_candles(symbol):
    """趋势验证专用K线缓存：只补缺口和最新K线，绘图从 MySQL 读取。"""
    raw_symbol = symbol.replace("/", "")
    now_bucket = int(time.time()) // PRICE_HISTORY_BUCKET_SECONDS * PRICE_HISTORY_BUCKET_SECONDS
    latest = TradeValidationCandle.query.filter_by(symbol=symbol, interval=TRADE_VALIDATION_INTERVAL).order_by(TradeValidationCandle.bucket_at.desc()).first()
    start_bucket = (latest.bucket_at + PRICE_HISTORY_BUCKET_SECONDS) if latest else now_bucket - TRADE_VALIDATION_CHART_SECONDS
    if start_bucket >= now_bucket:
        return
    start_ms, end_ms = start_bucket * 1000, now_bucket * 1000
    rows = []
    while start_ms < end_ms:
        payload = get_json("https://fapi.binance.com/fapi/v1/klines?" + urlencode({
            "symbol": raw_symbol,
            "interval": TRADE_VALIDATION_INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms - 1,
            "limit": 1000,
        }), timeout=8)
        if not payload:
            break
        for item in payload:
            bucket_at = int(item[0] // 1000)
            if bucket_at < now_bucket:
                rows.append({
                    "symbol": symbol,
                    "interval": TRADE_VALIDATION_INTERVAL,
                    "bucket_at": bucket_at,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                    "quote_volume": float(item[7]),
                })
        next_start = int(payload[-1][0]) + PRICE_HISTORY_BUCKET_SECONDS * 1000
        if next_start <= start_ms:
            break
        start_ms = next_start
    if rows:
        existing = {
            item.bucket_at for item in TradeValidationCandle.query.filter_by(
                symbol=symbol, interval=TRADE_VALIDATION_INTERVAL
            ).filter(TradeValidationCandle.bucket_at.in_([row["bucket_at"] for row in rows])).all()
        }
        for row in rows:
            if row["bucket_at"] not in existing:
                db.session.add(TradeValidationCandle(**row))
    cutoff = now_bucket - TRADE_VALIDATION_CANDLE_RETENTION_SECONDS
    TradeValidationCandle.query.filter(
        TradeValidationCandle.symbol == symbol,
        TradeValidationCandle.interval == TRADE_VALIDATION_INTERVAL,
        TradeValidationCandle.bucket_at < cutoff,
    ).delete(synchronize_session=False)


def validation_candles(symbol, limit=None, sync=True):
    if sync:
        sync_trade_validation_candles(symbol)
    cutoff = int(time.time()) - TRADE_VALIDATION_CHART_SECONDS
    query = TradeValidationCandle.query.filter_by(symbol=symbol, interval=TRADE_VALIDATION_INTERVAL).filter(
        TradeValidationCandle.bucket_at >= cutoff
    ).order_by(TradeValidationCandle.bucket_at)
    if limit:
        query = query.limit(limit)
    rows = query.all()
    if not rows:
        rows = list(reversed(FuturesPriceHistory.query.filter_by(symbol=symbol).order_by(FuturesPriceHistory.bucket_at.desc()).limit(288).all()))
        return [{
            "time": datetime.fromtimestamp(row.bucket_at).strftime("%m-%d %H:%M"),
            "open": row.price,
            "high": row.price,
            "low": row.price,
            "close": row.price,
            "volume": None,
        } for row in rows]
    return [{
        "time": datetime.fromtimestamp(row.bucket_at).strftime("%m-%d %H:%M"),
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "volume": row.quote_volume,
    } for row in rows]


def validation_auto_metrics(symbol):
    rows = TradeValidationCandle.query.filter_by(
        symbol=symbol, interval=TRADE_VALIDATION_INTERVAL
    ).order_by(TradeValidationCandle.bucket_at.desc()).limit(60).all()
    rows = list(reversed(rows))
    if len(rows) < 30:
        return None
    closes = [row.close for row in rows]
    highs = [row.high for row in rows]
    lows = [row.low for row in rows]
    volumes = [row.quote_volume or 0 for row in rows]
    cvd_1h = sum(((row.close - row.open) / max(row.high - row.low, row.close * 0.001)) * (row.quote_volume or 0) for row in rows[-12:])
    cvd_30m = sum(((row.close - row.open) / max(row.high - row.low, row.close * 0.001)) * (row.quote_volume or 0) for row in rows[-6:])
    price = closes[-1]
    atr = sum(high - low for high, low in zip(highs[-14:], lows[-14:])) / 14
    raw_symbol = symbol.replace("/", "")
    oi_1h = ratio_1h = oi_4h = ratio_4h = None
    try:
        oi = get_json("https://fapi.binance.com/futures/data/openInterestHist?" + urlencode({"symbol": raw_symbol, "period": "5m", "limit": 49}), timeout=5)
        ratios = get_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?" + urlencode({"symbol": raw_symbol, "period": "5m", "limit": 49}), timeout=5)
        if isinstance(oi, list) and len(oi) >= 13:
            oi_values = [float(item.get("sumOpenInterest", 0) or 0) for item in oi]
            oi_1h = percent_delta(oi_values[-1], oi_values[-13])
            oi_4h = percent_delta(oi_values[-1], oi_values[0]) if len(oi_values) >= 49 else None
        if isinstance(ratios, list) and len(ratios) >= 13:
            ratio_values = [float(item.get("longShortRatio", 0) or 0) for item in ratios]
            ratio_1h = percent_delta(ratio_values[-1], ratio_values[-13])
            ratio_4h = percent_delta(ratio_values[-1], ratio_values[0]) if len(ratio_values) >= 49 else None
    except Exception:
        pass
    return {
        "price": price,
        "high_30m": max(highs[-6:]),
        "low_30m": min(lows[-6:]),
        "high_1h": max(highs[-12:]),
        "low_1h": min(lows[-12:]),
        "price_30m": percent_delta(closes[-1], closes[-7]) or 0,
        "price_1h": percent_delta(closes[-1], closes[-13]) or 0,
        "price_4h": percent_delta(closes[-1], closes[-49]) if len(closes) >= 49 else 0,
        "cvd_30m": cvd_30m,
        "cvd_1h": cvd_1h,
        "volume_1h": sum(volumes[-12:]),
        "atr": atr,
        "oi_1h": oi_1h,
        "oi_4h": oi_4h,
        "ratio_1h": ratio_1h,
        "ratio_4h": ratio_4h,
    }


def validation_signal_plan(symbol):
    metrics = validation_auto_metrics(symbol)
    if not metrics:
        return None
    price = metrics["price"]
    atr = max(metrics["atr"], price * 0.012)
    oi_1h = metrics["oi_1h"] if metrics["oi_1h"] is not None else 0
    ratio_1h = metrics["ratio_1h"] if metrics["ratio_1h"] is not None else 0
    long_score = 0
    long_score += 18 if metrics["price_30m"] > 0.35 and metrics["price_1h"] > 0.2 else 0
    long_score += 18 if metrics["cvd_30m"] > 0 and metrics["cvd_1h"] > 0 else 0
    long_score += 16 if oi_1h > 0.4 else 0
    long_score += 16 if ratio_1h < -0.8 else 0
    long_score += 10 if price >= metrics["high_30m"] * 0.995 else 0
    short_score = 0
    short_score += 18 if metrics["price_30m"] < -0.35 and metrics["price_1h"] < -0.2 else 0
    short_score += 18 if metrics["cvd_30m"] < 0 and metrics["cvd_1h"] < 0 else 0
    short_score += 16 if oi_1h > -0.5 else 0
    short_score += 16 if ratio_1h > 0.8 else 0
    short_score += 10 if price <= metrics["low_30m"] * 1.005 else 0
    if max(long_score, short_score) < 52:
        return None
    if long_score >= short_score:
        entry = max(price * 1.001, metrics["high_30m"] * 1.0005)
        risk = max(atr * 1.8, entry * 0.025)
        direction = "long"
        stop = entry - risk
        tp1 = entry + risk * 0.9
        tp2 = entry + risk * 1.7
        thesis = f"{symbol} 自动续盯做多：30M/1H 价格转强，CVD 同步为正，持仓与多空人数比条件给到趋势验证。等待突破 {entry:.6g} 后才触发，跌回 {stop:.6g} 说明多头验证失败。"
    else:
        entry = min(price * 0.999, metrics["low_30m"] * 0.9995)
        risk = max(atr * 1.8, entry * 0.03)
        direction = "short"
        stop = entry + risk
        tp1 = entry - risk * 0.9
        tp2 = entry - risk * 1.7
        thesis = f"{symbol} 自动续盯做空：30M/1H 价格走弱，CVD 同步为负，多空人数比回升或持仓未明显塌陷，按反弹转弱/诱多失败验证。等待跌破 {entry:.6g} 后才触发，站回 {stop:.6g} 说明空头验证失败。"
    return {
        "symbol": symbol,
        "direction": direction,
        "entry_price": round(entry, 8),
        "stop_price": round(stop, 8),
        "take_profit_1": round(tp1, 8),
        "take_profit_2": round(tp2, 8),
        "thesis": thesis,
    }


def ensure_trade_validation_auto_plans():
    for symbol in TRADE_VALIDATION_AUTO_SYMBOLS:
        active = TradeValidation.query.filter(
            TradeValidation.symbol == symbol,
            TradeValidation.status.in_(["planned", "open"]),
        ).first()
        if active:
            continue
        sync_trade_validation_candles(symbol)
        signal = validation_signal_plan(symbol)
        if not signal:
            continue
        db.session.add(TradeValidation(
            symbol=signal["symbol"],
            direction=signal["direction"],
            entry_price=signal["entry_price"],
            stop_price=signal["stop_price"],
            take_profit_1=signal["take_profit_1"],
            take_profit_2=signal["take_profit_2"],
            stake_usdt=100,
            leverage=1,
            status="planned",
            thesis=signal["thesis"],
            created_at=datetime.now(),
        ))


def seed_trade_validation():
    if TradeValidation.query.first():
        return
    db.session.add(TradeValidation(
        symbol="ACE/USDT",
        direction="long",
        entry_price=0.1226,
        stop_price=0.1168,
        take_profit_1=0.1278,
        take_profit_2=0.1340,
        stake_usdt=100,
        leverage=1,
        status="planned",
        thesis="30M、4H、犄角延续三榜共振第一；不追跌中反抽，等待重新站回 0.1226 后验证多头转强。",
    ))


@app.get("/api/trade-validation")
def trade_validation():
    plans = TradeValidation.query.order_by(TradeValidation.created_at.desc()).all()
    replay_events = {}
    for plan in plans:
        if plan.status == "closed":
            continue
        sync_trade_validation_candles(plan.symbol)
        cutoff = int(time.time()) - TRADE_VALIDATION_CHART_SECONDS
        candles = TradeValidationCandle.query.filter_by(
            symbol=plan.symbol, interval=TRADE_VALIDATION_INTERVAL
        ).filter(TradeValidationCandle.bucket_at >= cutoff).order_by(TradeValidationCandle.bucket_at).all()
        replay_events[plan.id] = replay_validation_plan(plan, candles)
    db.session.commit()
    ensure_trade_validation_auto_plans()
    db.session.commit()
    plans = TradeValidation.query.order_by(TradeValidation.created_at.desc()).all()
    closed = [plan for plan in plans if plan.status == "closed"]
    wins = [plan for plan in closed if validation_pnl(plan, latest_validation_price(plan.symbol)) > 0]
    total_pnl = sum(validation_pnl(plan, latest_validation_price(plan.symbol)) for plan in plans)

    def payload(plan):
        price = latest_validation_price(plan.symbol)
        return {
            "id": plan.id,
            "symbol": plan.symbol,
            "direction": plan.direction,
            "entry_price": plan.entry_price,
            "stop_price": plan.stop_price,
            "take_profit_1": plan.take_profit_1,
            "take_profit_2": plan.take_profit_2,
            "stake_usdt": plan.stake_usdt,
            "leverage": plan.leverage,
            "status": plan.status,
            "thesis": plan.thesis,
            "current_price": price,
            "pnl": validation_pnl(plan, price),
            "opened_at": plan.opened_at.strftime("%m-%d %H:%M:%S") if plan.opened_at else None,
            "closed_at": plan.closed_at.strftime("%m-%d %H:%M:%S") if plan.closed_at else None,
            "exit_price": plan.exit_price,
            "exit_reason": plan.exit_reason,
            "events": replay_events.get(plan.id, []),
        }

    return jsonify({
        "summary": {
            "total": len(plans),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": (len(wins) / len(closed) * 100) if closed else None,
            "total_pnl": total_pnl,
        },
        "plans": [payload(plan) for plan in plans],
    })


@app.get("/api/trade-validation/<int:plan_id>/detail")
def trade_validation_detail(plan_id):
    plan = db.get_or_404(TradeValidation, plan_id)
    if plan.status != "closed":
        sync_trade_validation_candles(plan.symbol)
    cutoff = int(time.time()) - TRADE_VALIDATION_CHART_SECONDS
    candles = TradeValidationCandle.query.filter_by(
        symbol=plan.symbol, interval=TRADE_VALIDATION_INTERVAL
    ).filter(TradeValidationCandle.bucket_at >= cutoff).order_by(TradeValidationCandle.bucket_at).all()
    events = replay_validation_plan(plan, candles)
    db.session.commit()
    price = latest_validation_price(plan.symbol)
    return jsonify({
        "id": plan.id,
        "symbol": plan.symbol,
        "direction": plan.direction,
        "entry_price": plan.entry_price,
        "stop_price": plan.stop_price,
        "take_profit_1": plan.take_profit_1,
        "take_profit_2": plan.take_profit_2,
        "stake_usdt": plan.stake_usdt,
        "leverage": plan.leverage,
        "status": plan.status,
        "thesis": plan.thesis,
        "current_price": price,
        "pnl": validation_pnl(plan, price),
        "opened_at": plan.opened_at.strftime("%m-%d %H:%M:%S") if plan.opened_at else None,
        "closed_at": plan.closed_at.strftime("%m-%d %H:%M:%S") if plan.closed_at else None,
        "exit_price": plan.exit_price,
        "exit_reason": plan.exit_reason,
        "events": events,
        "candles": validation_candles(plan.symbol, limit=None, sync=plan.status != "closed"),
    })


with app.app_context():
    db.create_all()
    seed_symbol_aliases()
    seed_trade_validation()
    seed_thought_watches()
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
        time.sleep(max(0.25, MARKET_REFRESH_SECONDS - (time.time() - cycle_started_at)))


def background_dual_market_refresh():
    # 与现多期空错峰，避免两轮交易所请求和 MySQL 写入每 5 秒同时抢占资源。
    time.sleep(MARKET_REFRESH_SECONDS / 2)
    while True:
        cycle_started_at = time.time()
        try:
            with app.app_context():
                dual_futures_snapshot()
        except Exception:
            with app.app_context():
                db.session.rollback()
        time.sleep(max(0.25, MARKET_REFRESH_SECONDS - (time.time() - cycle_started_at)))


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
                groups = []
                spot_snapshot = load_latest_market_snapshot()
                if spot_snapshot:
                    groups.extend(spot_snapshot["symbols"])
                dual_snapshot = load_latest_dual_futures_snapshot()
                if dual_snapshot:
                    seen = {group["symbol"] for group in groups}
                    for group in dual_snapshot["symbols"]:
                        if group["symbol"] in seen or is_rwa_stock_pair(group["symbol"]):
                            continue
                        if any(row.get("long_exchange") == "Binance" or row.get("short_exchange") == "Binance" for row in group["rows"]):
                            groups.append(group)
                            seen.add(group["symbol"])
                if groups:
                    backfill_price_history(groups)
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


def persist_thought_watch_basis(symbol, basis, funding):
    """将思路盯盘里的深基差按 1% 打开、每扩大 0.2% 留痕，不产生通用报警噪声。"""
    strategy = "thought_watch"
    active_trackings = BasisTracking.query.filter_by(resolved_at=None).all()
    active_by_symbol = {(item.strategy or "spot_futures", item.symbol): item for item in active_trackings}
    active_by_key = {(item.strategy or "spot_futures", item.symbol, item.direction): item for item in active_trackings}
    row = {
        "basis": basis,
        "funding_rate": funding,
        "open_spread": 0,
        "close_spread": 0,
        "long_exchange": "Binance",
        "short_exchange": "Binance",
    }
    track_basis(symbol, row, active_by_symbol, active_by_key, strategy=strategy, emit_alert=False)
    db.session.commit()


def background_turnover_basis_watch():
    """ZAMA每5秒、SOON每30秒检查基差；连续两次确认后触发完整结构分析。"""
    time.sleep(12)
    symbols = {"SOON/USDT": "SOONUSDT", "ZAMA/USDT": "ZAMAUSDT"}
    negative_counts = {symbol: 0 for symbol in symbols}
    last_phases = {symbol: "positive" for symbol in symbols}
    active_turnover_symbols = set(symbols)
    status_tick = 0
    while True:
        try:
            update_status = status_tick % 60 == 0
            if update_status:
                with app.app_context():
                    mark_automation_status("turnover_basis_watch", "started")
            should_push = False
            if status_tick % 6 == 0:
                with app.app_context():
                    active_turnover_symbols = set(symbols) & active_thought_symbols()
            due_symbols = {}
            if "ZAMA/USDT" in active_turnover_symbols:
                due_symbols["ZAMA/USDT"] = symbols["ZAMA/USDT"]
            if status_tick % 6 == 0 and "SOON/USDT" in active_turnover_symbols:
                due_symbols["SOON/USDT"] = symbols["SOON/USDT"]
            for symbol, raw_symbol in due_symbols.items():
                premium = get_json(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={raw_symbol}", timeout=5)
                funding = float(premium.get("lastFundingRate", 0) or 0) * 100
                mark_price = float(premium.get("markPrice", 0) or 0)
                index_price = float(premium.get("indexPrice", 0) or 0)
                basis = percent_delta(mark_price, index_price) if index_price else None
                if basis is not None and basis < 0:
                    negative_counts[symbol] += 1
                else:
                    negative_counts[symbol] = 0
                stable = negative_counts[symbol] >= 2
                if not stable:
                    phase = "positive" if basis is None or basis >= 0 else "negative_pending"
                else:
                    if basis <= -2.0:
                        basis_zone = "below-200"
                    elif basis <= -1.0:
                        basis_zone = "minus-100-200"
                    elif basis <= -0.5:
                        basis_zone = "minus-050-100"
                    elif basis <= -0.2:
                        basis_zone = "minus-020-050"
                    else:
                        basis_zone = "negative"
                    funding_phase = "funding_negative" if funding < 0 else "funding_positive"
                    phase = f"{funding_phase}:{basis_zone}"
                TURNOVER_BASIS_STATE[symbol] = {
                    "stable": stable,
                    "basis": basis,
                    "funding": funding,
                    "phase": phase,
                    "updated_at": datetime.now(SHANGHAI_TZ).isoformat(),
                }
                if symbol == "ZAMA/USDT" and basis is not None:
                    with app.app_context():
                        persist_thought_watch_basis(symbol, basis, funding)
                if stable and phase != last_phases[symbol]:
                    should_push = True
                last_phases[symbol] = phase
            # 每30秒只复核SOON/ZAMA两枚重点币，使结构方向候选能在约30秒后完成二次确认，
            # 不必等待全市场5分钟扫描，也不会让其它币被高频重复扫描。
            structural_recheck = status_tick % 6 == 0
            if active_turnover_symbols and (should_push or structural_recheck):
                with app.app_context():
                    send_thought_analysis_push(only_symbols=active_turnover_symbols)
            if update_status:
                with app.app_context():
                    mark_automation_status("turnover_basis_watch", "success")
        except Exception as exc:
            with app.app_context():
                db.session.rollback()
                mark_automation_status("turnover_basis_watch", "error", exc)
        status_tick += 1
        time.sleep(5)


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
    threading.Thread(target=background_turnover_basis_watch, daemon=True, name="turnover-basis-watch").start()


if __name__ == "__main__":
    start_background_workers()
    app.run(host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "5000")), debug=False)
