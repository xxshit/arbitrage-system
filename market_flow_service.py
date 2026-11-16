"""Public market-flow collection, durable evidence and authenticated web views."""
import asyncio
from functools import wraps
import json
import logging
import secrets
import shutil
import threading
import time
from urllib.parse import urlencode

from flask import Blueprint, jsonify, request, session
from sqlalchemy import Table, Column, String, Text, BigInteger, select, func, delete
from sqlalchemy.exc import IntegrityError

from market_flow import FlowEngine, LIMITS, MAX_BARS, MAX_MANUAL, MAX_FOCUS, contract, notice
from market_transport import PublicMarketHttp

CATALOGUE_URL = ("https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT"
                 "&symbolStatus=TRADING&showPermissionSets=false")

class FlowStore:
    def __init__(self, engine, tables):
        self.engine = engine
        self.tags, self.events, self.state = tables

    def initialize(self):
        try:
            with self.engine.begin() as conn:
                conn.execute(self.state.insert().values(key="registry", value_json="{}"))
        except IntegrityError:
            pass

    def tag_list(self):
        with self.engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(select(self.tags).order_by(self.tags.c.added_at, self.tags.c.contract))]

    def tag(self, symbol, label, now, *, remove=False):
        # The single registry row serializes the shared quota across HTTP workers.
        with self.engine.begin() as conn:
            row = conn.execute(select(self.state.c.key).where(self.state.c.key == "registry").with_for_update()).first()
            if row is None:
                raise ValueError("监控尚未初始化，请稍后再试")
            if remove:
                conn.execute(delete(self.tags).where(self.tags.c.contract == symbol))
                return
            present = conn.execute(select(self.tags.c.contract).where(self.tags.c.contract == symbol)).first()
            if present:
                conn.execute(self.tags.update().where(self.tags.c.contract == symbol).values(label=label))
                return
            if conn.execute(select(func.count()).select_from(self.tags)).scalar_one() >= MAX_MANUAL:
                raise ValueError(f"手动标签最多{MAX_MANUAL}个，请先移除不再重点监控的币")
            conn.execute(self.tags.insert().values(contract=symbol, label=label, added_at=int(now)))

    def advance(self, row, now):
        key = "notice:"+row["contract"]
        if row["status"] != "live" or not row["can_notify"]:
            return None
        with self.engine.begin() as conn:
            raw = conn.execute(select(self.state.c.value_json).where(self.state.c.key == key).with_for_update()).scalar_one_or_none()
            previous = json.loads(raw) if raw else {}
            event, state = notice(row, previous, now)
            if raw is None and not event:
                return None
            if raw is None:
                conn.execute(self.state.insert().values(key=key, value_json=json.dumps(state, separators=(",", ":"))))
            if event:
                conn.execute(self.events.insert().values(id=event["id"], contract=event["contract"],
                             observed_at=int(now), evidence_json=json.dumps(event, ensure_ascii=False, separators=(",", ":"))))
            if raw is not None and state != previous:
                conn.execute(self.state.update().where(self.state.c.key == key).values(value_json=json.dumps(state, separators=(",", ":"))))
        return event

    def history(self, now, *, page=1, symbol=None, active=False):
        query = select(self.events.c.evidence_json).where(self.events.c.observed_at <= int(now))
        if active:
            query = query.where(self.events.c.observed_at >= int(now-120))
        if symbol:
            query = query.where(self.events.c.contract == symbol)
        query = query.order_by(self.events.c.observed_at.desc(), self.events.c.id.desc())
        limit = MAX_FOCUS if active else 30
        with self.engine.connect() as conn:
            # Page by page; no full history JSON materialization on 5s polling.
            rows = list(conn.execute(query.offset((page-1)*limit).limit(limit+1)))
        return dict(items=[json.loads(row[0]) for row in rows[:limit]], page=page,
                    has_more=len(rows)>limit, page_size=limit)


def cached_market(cache, now, monotonic_now):
    values = []
    for endpoint in ("ticker/24hr", "ticker/bookTicker"):
        row = cache.get("https://api.binance.com/api/v3/"+endpoint, {})
        at = row.get("fetched_at")
        values.append((row.get("payload"), now-(monotonic_now-at) if isinstance(at, (int, float)) else 0))
    return values


class FlowMonitor:
    def __init__(self, app, db, tables, cache_reader):
        self.app, self.db, self.tables = app, db, tables
        self.cache_reader = cache_reader
        self.engine = FlowEngine()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.ready = False
        self.scout_symbols = []
        self.scouts_at = 0
        self.recommendations_at = 0
        self.recommended_symbols = []
        self.manual = []
        self.published = dict(rows=[], recommended=[], manual=[], checked_at=0)
        self.health = dict(running=False, stream_connected=False, persistence_error=False,
                           last_error=None, received_frames=0, received_bytes=0, rest_requests=0,
                           events_written=0,
                           scope="币安USDT现货；未接链上持仓或转账", status="starting")
        self.retry_at = {}
        self.catalog_retry = 0
        self.processed = {}

    def store(self):
        # APIs are already within Flask's app context. Background callers enter
        # only to capture the engine, then close SQL before any network waits.
        return FlowStore(self.db.engine, self.tables)

    def status(self):
        with self.lock:
            return dict(self.health)

    def diagnostics(self, now):
        payload = self.payload(now)
        return dict(**payload["health"], catalogue_count=payload["overview"]["catalogue_count"],
                    screened_count=payload["overview"]["screened_count"], focus_count=payload["overview"]["focus_count"],
                    retained_bars=payload["overview"]["retained_bars"],
                    live_count=sum(r["status"] == "live" for r in payload["rows"]),
                    candidate_count=len(payload["recommended"]),
                    control_age_seconds=max(0, now-payload["checked_at"]))

    def payload(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            published = self.published
        # Recheck freshness at read time, including while a collector is stalled.
        rows = []
        for original in published["rows"]:
            row = dict(original)
            stamp = row.get("stream_at")
            if not stamp or not -2 <= now-stamp <= 10:
                row.update(status="stale", status_reason="实时汇总已过期，暂停候选与新提醒", candidate=False)
            rows.append(row)
        eligible = {r["contract"] for r in rows if r["candidate"] and r["status"] == "live"}
        selected = [s for s in published["recommended"] if s in eligible] if now-published["checked_at"] <= 15 else []
        return dict(overview=self.engine.overview(now), health=self.status(), rows=rows,
                    manual=published["manual"], recommended=selected,
                    checked_at=published["checked_at"], valid_until=published["checked_at"]+15,
                    ranking="已核验候选中的观察优先级，非收益概率；不足3个不补齐",
                    chain_status="未接入链上验证；普通转账不生成吸筹信号")

    def alerts(self, now=None):
        now = time.time() if now is None else now
        if not self.ready:
            return dict(active_events=[], health=self.status())
        payload = self.payload(now)
        watch = {r["contract"] for r in payload["manual"]} | set(payload["recommended"])
        fresh = {r["contract"] for r in payload["rows"] if r["status"] == "live"}
        if now-payload["checked_at"] > 15 or self.health["persistence_error"]:
            return dict(active_events=[], health=self.status())
        try:
            events = self.store().history(now, active=True)["items"]
        except Exception:
            return dict(active_events=[], health={**self.status(), "persistence_error": True, "status": "storage_unavailable"})
        return dict(active_events=[{**event, "audible": True, "remaining_seconds": max(0, int(120-(now-event["observed_at"])))}
                                  for event in events if event["contract"] in watch & fresh], health=self.status())

    def cycle(self, now, fetch):
        used_http = False
        if now >= self.catalog_retry and now-self.engine.catalog_at >= 3600:
            self.catalog_retry = now+60
            self.health["rest_requests"] += 1
            self.engine.set_catalog(fetch(CATALOGUE_URL), time.time())
            used_http = True
        (tickers, at), (books, book_at) = cached_market(self.cache_reader(), now, time.monotonic())
        self.engine.ingest_market(tickers, books, at, now, book_at=book_at)
        with self.app.app_context():
            store = self.store()
            self.manual = store.tag_list()
        manual = [r["contract"] for r in self.manual]
        if now-self.scouts_at >= 600 or not self.scout_symbols:
            self.scout_symbols = self.engine.scouts(manual, now)
            self.scouts_at = now
        scouts = [s for s in self.scout_symbols if s not in manual and s in self.engine.catalog]
        focus = [s for s in manual if s in self.engine.catalog]+scouts
        self.engine.set_focus(focus)
        self.retry_at = {s: at for s, at in self.retry_at.items() if s in focus}
        # One bounded history fill per control cycle. Steady updates come from
        # one combined public socket; no per-symbol polling every five seconds.
        for symbol in focus:
            row = self.engine.inspect(symbol, now)
            needs_seed = row["status"] in {"warming", "gap"} or symbol not in self.engine.seeded_at
            if not used_http and needs_seed and now >= self.retry_at.get(symbol, 0):
                self.retry_at[symbol] = now+60
                self.health["rest_requests"] += 1
                data = fetch("https://api.binance.com/api/v3/klines?"+urlencode(dict(symbol=symbol, interval="1m", limit=MAX_BARS)))
                self.engine.seed(symbol, data, time.time())
                break
        now = time.time()
        rows = [self.engine.inspect(s, now) for s in focus]
        eligible = [r for r in rows if r["candidate"]]
        eligible.sort(key=lambda r: (-r["score"], r["contract"]))
        valid = {r["contract"] for r in eligible}
        self.recommended_symbols = [s for s in self.recommended_symbols if s in valid]
        if now-self.recommendations_at >= 60:
            self.recommended_symbols = [r["contract"] for r in eligible[:3]]
            self.recommendations_at = now
        watch = set(manual) | set(self.recommended_symbols)
        self.processed = {s: key for s, key in self.processed.items() if s in watch}
        with self.app.app_context():
            store = self.store()
            for row in rows:
                key = (row.get("evidence", {}).get("window_end") if row.get("evidence") else None, row["kind"], row["can_notify"])
                if row["contract"] in watch and self.processed.get(row["contract"]) != key:
                    if store.advance(row, now):
                        self.health["events_written"] += 1
                    self.processed[row["contract"]] = key
        # Unsupported delisted manual tags remain visible and are never deleted.
        rows.extend(self.engine.inspect(s, now) for s in manual if s not in focus)
        with self.lock:
            self.published = dict(rows=rows, recommended=list(self.recommended_symbols),
                                  manual=self.manual, checked_at=now)
            self.health.update(running=True, persistence_error=False, last_error=None, status="running")

    def run(self):
        with self.app.app_context():
            self.store().initialize()
        self.ready = True
        logger = logging.getLogger("arbitrage.market_flow")
        logger.setLevel(logging.INFO)
        logger.handlers = list(self.app.logger.handlers) or [logging.StreamHandler()]
        logger.propagate = False
        logged_at = 0
        client = PublicMarketHttp(connections_per_host=1)
        try:
            while not self.stop.is_set():
                start = time.monotonic()
                try:
                    # New monitoring yields under low disk; existing market and
                    # alarm workers are untouched. No history cleanup here.
                    if shutil.disk_usage(self.app.instance_path).free < 512*1024*1024:
                        self.engine.set_focus(())
                        self.health.update(running=False, status="resource_paused", last_error="disk_below_512MiB")
                    else:
                        self.cycle(time.time(), lambda url: client.get_json(url, timeout=4, weight_limit=4000))
                except Exception as exc:
                    self.health.update(last_error=type(exc).__name__, persistence_error=True, status="degraded")
                    # No arbitrary upstream message, SQL parameters or stack dump.
                    self.app.logger.warning("market_flow_cycle_failed type=%s", type(exc).__name__)
                if time.monotonic()-logged_at >= 60:
                    logger.info("market_flow_health %s", json.dumps(self.diagnostics(time.time()), separators=(",", ":")))
                    logged_at = time.monotonic()
                self.stop.wait(max(.25, 5-(time.monotonic()-start)))
        finally:
            client.close()
            self.ready = False

    def run_stream(self):
        asyncio.run(self._stream())

    async def _stream(self):
        from websockets.asyncio.client import connect
        delay = 1
        while not self.stop.is_set():
            if not self.engine.focus:
                await asyncio.sleep(1)
                continue
            try:
                async with connect("wss://stream.binance.com:443/ws", proxy=None,
                                   open_timeout=8, close_timeout=1, ping_interval=20,
                                   max_size=65536, max_queue=8, compression=None) as socket:
                    subscriptions = set()
                    updated = 0
                    self.health["stream_connected"] = True
                    while not self.stop.is_set():
                        if time.monotonic()-updated >= 5:
                            wanted = {s.lower()+"@kline_1m" for s in self.engine.focus}
                            if subscriptions-wanted:
                                await socket.send(json.dumps(dict(method="UNSUBSCRIBE", params=sorted(subscriptions-wanted), id=1)))
                            if wanted-subscriptions:
                                await socket.send(json.dumps(dict(method="SUBSCRIBE", params=sorted(wanted-subscriptions), id=2)))
                            subscriptions = wanted
                            updated = time.monotonic()
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            continue
                        self.health["received_frames"] += 1
                        self.health["received_bytes"] += len(message)
                        payload = json.loads(message)
                        if not isinstance(payload, dict) or "code" in payload or "error" in payload:
                            raise ValueError("subscription_rejected")
                        self.engine.ingest_stream(payload, time.time())
                        delay = 1
            except Exception as exc:
                self.health.update(stream_connected=False, last_error=type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(60, delay*2)
            finally:
                self.health["stream_connected"] = False


def register(app, db, current_user, cache_reader):
    tags = Table("market_flow_tag", db.metadata,
                 Column("contract", String(30), primary_key=True), Column("label", String(80), nullable=False),
                 Column("added_at", BigInteger, nullable=False))
    events = Table("market_flow_event", db.metadata,
                   Column("id", String(40), primary_key=True), Column("contract", String(30), nullable=False, index=True),
                   Column("observed_at", BigInteger, nullable=False, index=True), Column("evidence_json", Text, nullable=False))
    state = Table("market_flow_state", db.metadata,
                  Column("key", String(64), primary_key=True), Column("value_json", Text, nullable=False))
    monitor = FlowMonitor(app, db, (tags, events, state), cache_reader)
    bp = Blueprint("market_flow", __name__, url_prefix="/api/market-flow")

    def protect(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return jsonify(error="请先登录"), 401
            if request.method in {"POST", "DELETE"}:
                if user.role != "admin":
                    return jsonify(error="共享标签由管理员维护"), 403
                token = session.get("csrf_token")
                if not token or not secrets.compare_digest(str(token), request.headers.get("X-CSRF-Token", "")):
                    return jsonify(error="请求校验失败，请刷新页面"), 403
                if request.content_length is None or request.content_length > 1024:
                    return jsonify(error="标签请求过大"), 413
                if not monitor.ready:
                    return jsonify(error="监控初始化中，请稍后再试"), 503
            db.session.remove()
            try:
                return view(*args, **kwargs)
            except (ValueError, TypeError):
                return jsonify(error="参数不合法；请检查币安USDT现货代码、标签长度和20个手动标签上限"), 400
            except Exception:
                return jsonify(error="监控存储暂时不可用，未影响原有报警"), 503
        return wrapped

    @bp.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @bp.get("")
    @protect
    def view():
        data = monitor.payload()
        data["manual"] = monitor.store().tag_list()
        return jsonify(data)

    @bp.get("/events")
    @protect
    def history():
        page = int(request.args.get("page", "1"))
        if not 1 <= page <= 100000:
            raise ValueError("page")
        symbol = contract(request.args["symbol"]) if request.args.get("symbol") else None
        return jsonify(monitor.store().history(time.time(), page=page, symbol=symbol))

    @bp.route("/tags", methods=["POST", "DELETE"])
    @protect
    def tags_view():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("body")
        symbol = contract(value.get("symbol"))
        label = value.get("label", "手动关注")
        if not isinstance(label, str) or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise ValueError("label")
        if request.method == "POST" and (symbol not in monitor.engine.catalog or time.time()-monitor.engine.catalog_at > 7200):
            return jsonify(error="未在新鲜的币安USDT现货目录中找到该币；未建立监控"), 400
        monitor.store().tag(symbol, label, time.time(), remove=request.method == "DELETE")
        return jsonify(ok=True, manual=monitor.store().tag_list())

    app.register_blueprint(bp)
    app.extensions["market_flow"] = monitor
    return monitor
