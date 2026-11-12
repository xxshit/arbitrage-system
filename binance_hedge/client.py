"""Restricted Binance HTTPS adapter; no withdrawals, transfers or mode changes.

One persistent TLS connection per market. Errors never expose signed URLs,
request bodies, API key material or exchange error messages.
"""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import hmac
import http.client
import json
import ssl
import socket
import threading
import time
from urllib.parse import urlencode

from .planner import Book, GuardError, Rules, dec, plan_clip, symbol_pair
from .budget import BudgetExpired, deadline, remaining, submit


class ExchangeError(GuardError):
    def __init__(self, code="NETWORK", uncertain=False):
        self.code, self.uncertain = str(code), uncertain
        super().__init__(f"币安请求未确认（{self.code}）；请核验状态")


class SendBlocked(ExchangeError):
    """Raised before socket write; unlike a timeout this proves no submission."""
    def __init__(self):
        super().__init__("SEND_GUARD", uncertain=False)


class OrderGate:
    """Local readiness barrier, never an exchange-side atomic transaction."""
    def __init__(self, deadline, parties=2, check=None):
        self.deadline = deadline
        self.barrier = threading.Barrier(parties)
        self.lock = threading.Lock()
        self.first_write = None
        self.check = check

    def abort(self):
        self.barrier.abort()

    def ready(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.abort()
            raise SendBlocked()
        try:
            self.barrier.wait(timeout=remaining)
        except threading.BrokenBarrierError:
            raise SendBlocked() from None

    def before_write(self, timing):
        with self.lock:
            if self.check:
                self.check()
            now = time.monotonic()
            if self.barrier.broken or now > self.deadline or (self.first_write is not None and now-self.first_write > .05):
                raise SendBlocked()
            if self.first_write is None:
                self.first_write = now
            timing.update(write_started_monotonic=now, write_started_utc_ms=int(time.time()*1000))


class OrderConnection(http.client.HTTPSConnection):
    """Use stdlib HTTP framing, but stage the complete POST before release.

    The connection must already be established. No automatic reconnect is
    possible in write_order; any failure after a write attempt is ambiguous.
    Neither staged bytes nor credentials enter diagnostics.
    """
    _staged_parts = None

    def send(self, data):
        if self._staged_parts is None:
            return super().send(data)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("Order body must be bounded bytes")
        self._staged_parts.append(bytes(data))

    def stage_order(self, target, body, headers):
        self._staged_parts = []
        try:
            self.request("POST", target, body=body, headers=headers)
            data = b"".join(self._staged_parts)
            if len(data) > 8192:
                raise ValueError("Order request too large")
            return data
        finally:
            self._staged_parts = None

    def write_order(self, data, gate, timing):
        if self.sock is None:
            raise SendBlocked()
        self.sock.settimeout(max(.001, min(2, gate.deadline-time.monotonic())))
        gate.before_write(timing)
        # sendall may fail after sending a prefix. Never classify that as zero.
        self.sock.sendall(data)
        timing.update(write_completed_monotonic=time.monotonic(), write_completed_utc_ms=int(time.time()*1000))
        self.sock.settimeout(2)


READS = {
    "spot": {"/api/v3/time", "/api/v3/depth", "/api/v3/exchangeInfo", "/api/v3/account",
             "/api/v3/account/commission", "/api/v3/openOrders", "/api/v3/order", "/api/v3/myTrades",
             "/sapi/v1/account/apiRestrictions"},
    "futures": {"/fapi/v1/time", "/fapi/v1/depth", "/fapi/v1/exchangeInfo", "/fapi/v3/account",
                "/fapi/v1/positionSide/dual", "/fapi/v1/multiAssetsMargin", "/fapi/v1/symbolConfig",
                "/fapi/v1/commissionRate", "/fapi/v1/openOrders", "/fapi/v1/order", "/fapi/v1/userTrades", "/fapi/v1/accountConfig"},
}


class BinanceClient:
    def __init__(self, key="", secret="", *, allow_orders=False):
        self._key, self._secret, self.allow_orders = key, secret, allow_orders
        self._tls = ssl.create_default_context()
        self._connections = {}
        self._locks = {m: threading.Lock() for m in READS}
        self._offset = {}
        self._cooldown_until = 0
        self.rate_guard = None
        self.rate_pause = None
        self.streams = None
        self.fast_mode = False
        self._metadata = None  # one symbol, <=30 seconds, no account balances
        self._closed = False

    def close(self):
        self._closed = True
        if self.streams:
            self.streams.close()
            self.streams = None
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()
        self._key = self._secret = ""
        self._metadata = None

    @contextmanager
    def request_slot(self, market):
        """Bound lock wait AND the entire read, including slow trickle bodies.

        A socket timeout alone restarts on every read. A watchdog shuts down
        this request's socket at the fixed deadline. Late DNS/connect returns
        are checked again before any bytes can be sent.
        """
        end = min(deadline() or float("inf"), time.monotonic()+2)
        if self._closed or not self._locks[market].acquire(timeout=remaining()):
            raise BudgetExpired()
        expired, wire_socket = threading.Event(), [None]
        def expire():
            expired.set()
            sock = wire_socket[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
        def check(connection=None):
            if self._closed or expired.is_set() or time.monotonic() >= end:
                raise BudgetExpired()
            if connection is not None:
                connection.timeout = end-time.monotonic()
                if connection.sock is not None:
                    wire_socket[0] = connection.sock
                    connection.sock.settimeout(connection.timeout)
        timer = threading.Timer(max(0, end-time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        try:
            check()
            yield check
        finally:
            timer.cancel()
            timer.join()
            self._locks[market].release()

    def request(self, market, path, params=None, *, method="GET", signed=False, order_gate=None, timing=None):
        if market not in READS or (method == "GET" and path not in READS[market]):
            raise GuardError("接口不在允许清单内")
        is_order = method == "POST" and path == ("/api/v3/order" if market == "spot" else "/fapi/v1/order")
        is_stream = market == "futures" and path == "/fapi/v1/listenKey" and method == "POST" and not signed and not params
        if method != "GET" and not is_stream and (not self.allow_orders or not is_order or not signed):
            raise GuardError("真实下单未启用")
        gate = (order_gate or OrderGate(time.monotonic()+2, parties=1)) if is_order else None
        timing = timing if timing is not None else {}
        host = "api.binance.com" if market == "spot" else "fapi.binance.com"
        with self.request_slot(market) as check:
            try:
                if self.rate_guard:
                    self.rate_guard()
                check()
                if time.monotonic() < self._cooldown_until:
                    raise ExchangeError("RATE_LIMIT", uncertain=False)
                connection = self._connections.get(market)
                if connection is None:
                    connection = OrderConnection(host, timeout=2, context=self._tls)
                    self._connections[market] = connection
                check(connection)
                if is_order:
                    if time.monotonic() >= gate.deadline:
                        raise SendBlocked()
                    connection.timeout = min(2, gate.deadline-time.monotonic())
                    if connection.sock is None:
                        connection.connect()
                elif connection.sock is None:
                    connection.connect()
                check(connection)
                # Sign after connection preparation, before the common barrier.
                values = dict(params or {})
                headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
                if is_stream:
                    if not self._key:
                        raise GuardError("缺少用户流凭据")
                    headers["X-MBX-APIKEY"] = self._key
                if signed:
                    if not self._key or not self._secret or market not in self._offset:
                        raise GuardError("私有 API 尚未完成身份与时钟校验")
                    offset, synced = self._offset[market]
                    if time.monotonic() - synced > 60:
                        raise GuardError("签名时钟已过期")
                    values.update(timestamp=int(time.time()*1000 + offset), recvWindow=2000)
                    headers["X-MBX-APIKEY"] = self._key
                body = urlencode(values)
                if signed:
                    body += "&signature=" + hmac.new(self._secret.encode(), body.encode(), hashlib.sha256).hexdigest()
                target = path + ("?" + body if method == "GET" and body else "")
                if is_order:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    wire = connection.stage_order(target, body, headers)
                    if connection.sock is None:
                        raise SendBlocked()
                    timing["ready_monotonic"] = time.monotonic()
                    gate.ready()
                    check(connection)
                    connection.write_order(wire, gate, timing)
                    del wire
                else:
                    check(connection)
                    connection.request(method, target, headers=headers)
                check(connection)
                response = connection.getresponse()
                raw = response.read(4 * 1024 * 1024 + 1)
                check()
                if is_order:
                    timing["response_received_monotonic"] = time.monotonic()
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("oversized")
                payload = json.loads(raw)
                if response.status in (418, 429):
                    try:
                        delay = max(60, int(response.getheader("Retry-After", "60")))
                    except (TypeError, ValueError):
                        delay = 3600
                    self._cooldown_until = time.monotonic() + delay
                    if self.rate_pause:
                        self.rate_pause(delay)
                if response.status != 200:
                    code = payload.get("code", response.status) if isinstance(payload, dict) else response.status
                    # -5021 proves zero fill only for a Futures LIMIT FOK
                    # submission. Do not apply it to Spot, IOC or a 503 reply.
                    fok_reject = (is_order and market == "futures" and type(code) is int and code == -5021
                                  and values.get("type") == "LIMIT" and values.get("timeInForce") == "FOK")
                    # Duplicate IDs, missing orders and transport errors do not
                    # prove absence; recovery must keep querying the same ID.
                    known_reject = response.status == 400 and (fok_reject or code in {-1013, -1100, -1102, -1111, -1116, -1121, -2019, -2027, -4164})
                    raise ExchangeError(code, uncertain=is_order and not known_reject)
                return payload
            except ExchangeError:
                if gate:
                    gate.abort()
                    connection = self._connections.pop(market, None)
                    if connection:
                        connection.close()
                raise
            except Exception:
                if gate:
                    gate.abort()
                connection = self._connections.pop(market, None)
                if connection:
                    connection.close()
                raise ExchangeError(uncertain=is_order and "write_started_monotonic" in timing) from None

    def sync_clock(self, market):
        start = time.time() * 1000
        payload = self.request(market, "/api/v3/time" if market == "spot" else "/fapi/v1/time")
        end = time.time() * 1000
        if not 0 <= end - start <= 400:
            raise GuardError("交易链路时延超过 400ms")
        self._offset[market] = (int(payload["serverTime"]) - (start + end) / 2, time.monotonic())

    def sync_clocks(self):
        self.parallel(lambda: self.sync_clock("spot"), lambda: self.sync_clock("futures"))

    def ensure_clock(self, market):
        if market not in self._offset or time.monotonic()-self._offset[market][1] > 55:
            self.sync_clock(market)

    @staticmethod
    def parallel(first, second):
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-pair")
        jobs = []
        try:
            jobs.append(submit(pool, first))
            jobs.append(submit(pool, second))
            a, b = jobs
            _, pending = wait((a, b), timeout=remaining(30))
            if pending:
                raise BudgetExpired()
            return a.result(), b.result()
        finally:
            pool.shutdown(wait=all(j.done() for j in jobs), cancel_futures=True)

    def accounts(self):
        self.sync_clocks()
        spot, future = self.parallel(
            lambda: self.request("spot", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True),
            lambda: self.request("futures", "/fapi/v3/account", signed=True))
        permissions = self.request("spot", "/sapi/v1/account/apiRestrictions", signed=True)
        self.validate_identity(spot, permissions)
        return spot, future, permissions

    @staticmethod
    def validate_identity(spot, permissions):
        if permissions.get("enableReading") is not True or permissions.get("enableWithdrawals") is not False:
            raise GuardError("请使用开启读取、关闭提现权限的专用 API Key")
        if permissions.get("ipRestrict") is not True:
            raise GuardError("请先将 API Key 限定为本站服务器 IP")
        if any(permissions.get(k) is not False for k in ("enableInternalTransfer", "permitsUniversalTransfer")):
            raise GuardError("请关闭 API Key 的转账权限")
        if not spot.get("uid"):
            raise GuardError("无法核验币安账户身份")

    def depth(self, market, symbol):
        if self.streams:
            return self.streams.book(market, symbol)
        start = time.monotonic()
        payload = self.request(market, "/api/v3/depth" if market == "spot" else "/fapi/v1/depth", {"symbol": symbol, "limit": 100})
        return Book.parse(payload, start, time.monotonic())

    def metadata(self, symbol):
        cached = self._metadata
        if cached and cached[0] == symbol and 0 <= time.monotonic()-cached[1] < 30:
            return cached[2]
        if self.fast_mode:
            raise GuardError("交易规则或费率预热已过期，请重新核验")
        started = time.monotonic()
        def spot():
            info = self.request("spot", "/api/v3/exchangeInfo", {"symbol": symbol})
            fee = self.request("spot", "/api/v3/account/commission", {"symbol": symbol}, signed=True)
            return next((s for s in info["symbols"] if s["symbol"] == symbol), {}), fee
        def future():
            info = self.request("futures", "/fapi/v1/exchangeInfo")
            fee = self.request("futures", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True)
            return next((s for s in info["symbols"] if s["symbol"] == symbol), {}), fee
        pair = self.parallel(spot, future)
        self._metadata = symbol, started, pair
        return pair

    def warm(self, display_symbol):
        self.sync_clocks()
        self.metadata(symbol_pair(display_symbol))

    def stream_signature(self):
        self.ensure_clock("spot")
        values = dict(apiKey=self._key, recvWindow=2000,
                      timestamp=int(time.time()*1000+self._offset["spot"][0]))
        payload = urlencode(sorted(values.items()))
        values["signature"] = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return values

    def stream_age(self, market, event_ms):
        offset, at = self._offset[market]
        if not 0 <= time.monotonic()-at <= 45:
            raise GuardError("事件流时钟已过期")
        age = (time.time()*1000+offset-int(event_ms))/1000
        if not -.2 <= age <= .4:
            raise GuardError("事件流延迟超限")
        return max(0, age)

    def fresh_state(self, symbol):
        # No balance, position, open-order, permission or mode cache is used
        # for placement. Each market keeps its already-connected TLS lane.
        if not self.streams:
            raise GuardError("持续盘口与成交订阅尚未就绪")
        self.streams.require_ready()
        for market in READS:
            if market not in self._offset or time.monotonic()-self._offset[market][1] > 30:
                raise GuardError("预热时钟已过期，请重新核验")
        def spot():
            return (self.request("spot", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True),
                    self.request("spot", "/sapi/v1/account/apiRestrictions", signed=True),
                    self.request("spot", "/api/v3/openOrders", {"symbol": symbol}, signed=True))
        def future():
            return (self.request("futures", "/fapi/v3/account", signed=True),
                    self.request("futures", "/fapi/v1/accountConfig", signed=True),
                    self.request("futures", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True),
                    self.request("futures", "/fapi/v1/symbolConfig", {"symbol": symbol}, signed=True))
        (s, permissions, so), (f, config, fo, symbols) = self.parallel(spot, future)
        self.validate_identity(s, permissions)
        self.streams.require_ready()
        return s, f, permissions, config, so, fo, symbols

    def prepare(self, request, *, anchor=None):
        symbol = symbol_pair(request.get("symbol"))
        if self.fast_mode:
            spot, future, permissions, config_account, spot_open, future_open, configs = self.fresh_state(symbol)
        else:
            spot, future, permissions = self.accounts()
            config_account = self.request("futures", "/fapi/v1/accountConfig", signed=True)
            spot_open, future_open = self.parallel(
                lambda: self.request("spot", "/api/v3/openOrders", {"symbol": symbol}, signed=True),
                lambda: self.request("futures", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True))
            configs = self.request("futures", "/fapi/v1/symbolConfig", {"symbol": symbol}, signed=True)
        if spot.get("canTrade") is not True or config_account.get("canTrade") is not True:
            raise GuardError("币安账户当前不能交易")
        if any(permissions.get(k) is not False for k in ("enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer")):
            raise GuardError("实盘专用 Key 必须关闭提现与转账权限")
        if not all(permissions.get(k) is True for k in ("enableSpotAndMarginTrading", "enableFutures")):
            raise GuardError("API Key 尚未具备现货与合约交易权限")
        if config_account.get("multiAssetsMargin") is not False or type(config_account.get("dualSidePosition")) is not bool:
            raise GuardError("首版仅支持普通单资产 USDT 合约账户")
        if any(p.get("symbol") == symbol and dec(p.get("positionAmt", "0")) != 0 for p in future.get("positions", [])):
            raise GuardError("该合约已有仓位；首版不叠加或修改既有仓位")
        if spot_open or future_open:
            raise GuardError("该币种已有挂单；请先在币安核对")
        config = next((r for r in configs if r["symbol"] == symbol), None)
        if not config or not 1 <= int(config["leverage"]) <= 10 or dec(request.get("leverage")) != dec(config["leverage"]):
            raise GuardError("所选杠杆须与币安现有 1–10 倍设置一致；本站不会自动调杠杆")
        (spot_info, sc), (future_info, fc) = self.metadata(symbol)
        sr = Rules.parse(spot_info)
        fr = Rules.parse(future_info, True)
        # Conservative undiscounted rate, including special and tax commissions.
        spot_fee = sum((dec(sc[k]["taker"]) + dec(sc[k]["buyer"]) for k in ("standardCommission", "specialCommission", "taxCommission")), Decimal(0))
        spot_exit_fee = sum((dec(sc[k]["taker"]) + dec(sc[k]["seller"]) for k in ("standardCommission", "specialCommission", "taxCommission")), Decimal(0))
        future_fee = dec(fc["takerCommissionRate"])
        sb, fb = self.parallel(lambda: self.depth("spot", symbol), lambda: self.depth("futures", symbol))
        plan = plan_clip(request, sr, fr, sb, fb, time.monotonic(), spot_fee, future_fee, anchor, spot_exit_fee=spot_exit_fee)
        cash = next((dec(b["free"]) for b in spot["balances"] if b["asset"] == "USDT"), Decimal(0))
        available = next((dec(b["availableBalance"]) for b in future["assets"] if b["asset"] == "USDT"), Decimal(0))
        q, cap = dec(plan["quantity"]), dec(plan["buy_cap"])
        for filt in spot_info["filters"]:
            if filt["filterType"] == "MAX_POSITION":
                existing = sum((dec(b["free"]) + dec(b["locked"]) for b in spot["balances"] if b["asset"] == sr.base), Decimal(0))
                if existing + q > dec(filt["maxPosition"]):
                    raise GuardError("现货已有余额加本批数量超过交易所持仓上限")
        future_notional = max(dec(plan["futures_quote"]), q * dec(plan["sell_cap"]))
        margin = future_notional * (Decimal("1.05") / dec(config["leverage"]) + future_fee)
        if cash < q * cap * (1 + spot_fee) or available < margin:
            raise GuardError("现货可用 USDT 或合约可用保证金不足（已预留手续费和 5% 保证金余量）")
        if future_notional > dec(config["maxNotionalValue"]):
            raise GuardError("超过当前杠杆允许的名义金额")
        plan.update(position_side="SHORT" if config_account["dualSidePosition"] else "BOTH", leverage=int(config["leverage"]),
                    margin_type=config["marginType"], account_uid=str(spot["uid"]))
        return plan, sr, fr

    def submit(self, market, params, *, gate=None, timing=None):
        return self.request(market, "/api/v3/order" if market == "spot" else "/fapi/v1/order", params,
                            method="POST", signed=True, order_gate=gate, timing=timing)

    def query(self, market, symbol, client_id):
        return self.request(market, "/api/v3/order" if market == "spot" else "/fapi/v1/order",
                            {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def fills(self, market, symbol, order_id):
        return self.request(market, "/api/v3/myTrades" if market == "spot" else "/fapi/v1/userTrades",
                            {"symbol": symbol, "orderId": order_id, "limit": 1000}, signed=True)
