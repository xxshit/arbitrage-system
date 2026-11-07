"""Restricted Binance HTTPS adapter; no withdrawals, transfers or mode changes.

One persistent TLS connection per market. Errors never expose signed URLs,
request bodies, API key material or exchange error messages.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import hmac
import http.client
import json
import ssl
import threading
import time
from urllib.parse import urlencode

from .planner import Book, GuardError, Rules, dec, plan_clip, symbol_pair


class ExchangeError(GuardError):
    def __init__(self, code="NETWORK", uncertain=False):
        self.code, self.uncertain = str(code), uncertain
        super().__init__(f"币安请求未确认（{self.code}）；请核验状态")


class SendBlocked(ExchangeError):
    """Raised before socket write; unlike a timeout this proves no submission."""
    def __init__(self):
        super().__init__("SEND_GUARD", uncertain=False)


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
        self.before_order_send = None

    def close(self):
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()
        self._key = self._secret = ""

    def request(self, market, path, params=None, *, method="GET", signed=False):
        if market not in READS or (method == "GET" and path not in READS[market]):
            raise GuardError("接口不在允许清单内")
        is_order = method == "POST" and path == ("/api/v3/order" if market == "spot" else "/fapi/v1/order")
        if method != "GET" and (not self.allow_orders or not is_order or not signed):
            raise GuardError("真实下单未启用")
        if self.rate_guard:
            self.rate_guard()
        if time.monotonic() < self._cooldown_until:
            raise ExchangeError("RATE_LIMIT", uncertain=is_order)
        values = dict(params or {})
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if signed:
            if not self._key or not self._secret or market not in self._offset:
                raise GuardError("私有 API 尚未完成身份与时钟校验")
            offset, synced = self._offset[market]
            if time.monotonic() - synced > 60:
                raise GuardError("签名时钟已过期")
            values.update(timestamp=int(time.time() * 1000 + offset), recvWindow=2000)
            headers["X-MBX-APIKEY"] = self._key
        body = urlencode(values)
        if signed:
            body += "&signature=" + hmac.new(self._secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        host = "api.binance.com" if market == "spot" else "fapi.binance.com"
        with self._locks[market]:
            try:
                connection = self._connections.get(market)
                if connection is None:
                    connection = http.client.HTTPSConnection(host, timeout=2, context=self._tls)
                    self._connections[market] = connection
                target = path + ("?" + body if method == "GET" and body else "")
                if method == "POST":
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    if self.before_order_send:
                        self.before_order_send(market)
                connection.request(method, target, body=body if method == "POST" else None, headers=headers)
                response = connection.getresponse()
                raw = response.read(4 * 1024 * 1024 + 1)
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
                    # Only explicit validation / insufficient-balance rejections
                    # prove absence. Duplicate ID and transport errors do not.
                    known_reject = response.status == 400 and code in {-1013, -1100, -1102, -1111, -1116, -1121, -2019, -2027, -4164}
                    raise ExchangeError(code, uncertain=is_order and not known_reject)
                return payload
            except ExchangeError:
                raise
            except Exception:
                connection = self._connections.pop(market, None)
                if connection:
                    connection.close()
                raise ExchangeError(uncertain=is_order) from None

    def sync_clocks(self):
        def sync(market):
            start = time.time() * 1000
            payload = self.request(market, "/api/v3/time" if market == "spot" else "/fapi/v1/time")
            end = time.time() * 1000
            if not 0 <= end - start <= 400:
                raise GuardError("交易链路时延超过 400ms")
            self._offset[market] = (int(payload["serverTime"]) - (start + end) / 2, time.monotonic())
        self.parallel(lambda: sync("spot"), lambda: sync("futures"))

    @staticmethod
    def parallel(first, second):
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-pair") as pool:
            a, b = pool.submit(first), pool.submit(second)
            return a.result(), b.result()

    def accounts(self):
        self.sync_clocks()
        spot, future = self.parallel(
            lambda: self.request("spot", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True),
            lambda: self.request("futures", "/fapi/v3/account", signed=True))
        permissions = self.request("spot", "/sapi/v1/account/apiRestrictions", signed=True)
        if permissions.get("enableReading") is not True or permissions.get("enableWithdrawals") is not False:
            raise GuardError("请使用开启读取、关闭提现权限的专用 API Key")
        if permissions.get("ipRestrict") is not True:
            raise GuardError("请先将 API Key 限定为本站服务器 IP")
        if any(permissions.get(k) is not False for k in ("enableInternalTransfer", "permitsUniversalTransfer")):
            raise GuardError("请关闭 API Key 的转账权限")
        if not spot.get("uid"):
            raise GuardError("无法核验币安账户身份")
        return spot, future, permissions

    def depth(self, market, symbol):
        start = time.monotonic()
        payload = self.request(market, "/api/v3/depth" if market == "spot" else "/fapi/v1/depth", {"symbol": symbol, "limit": 100})
        return Book.parse(payload, start, time.monotonic())

    def prepare(self, request, *, anchor=None):
        symbol = symbol_pair(request.get("symbol"))
        spot, future, permissions = self.accounts()
        config_account = self.request("futures", "/fapi/v1/accountConfig", signed=True)
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
        spot_open, future_open = self.parallel(
            lambda: self.request("spot", "/api/v3/openOrders", {"symbol": symbol}, signed=True),
            lambda: self.request("futures", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True))
        if spot_open or future_open:
            raise GuardError("该币种已有挂单；请先在币安核对")
        configs = self.request("futures", "/fapi/v1/symbolConfig", {"symbol": symbol}, signed=True)
        config = next((r for r in configs if r["symbol"] == symbol), None)
        if not config or not 1 <= int(config["leverage"]) <= 10 or dec(request.get("leverage")) != dec(config["leverage"]):
            raise GuardError("所选杠杆须与币安现有 1–10 倍设置一致；本站不会自动调杠杆")
        sr, fr = self.parallel(
            lambda: self.request("spot", "/api/v3/exchangeInfo", {"symbol": symbol}),
            lambda: self.request("futures", "/fapi/v1/exchangeInfo"))
        spot_info = next((s for s in sr["symbols"] if s["symbol"] == symbol), {})
        sr = Rules.parse(spot_info)
        fr = Rules.parse(next((s for s in fr["symbols"] if s["symbol"] == symbol), {}), True)
        sc, fc = self.parallel(
            lambda: self.request("spot", "/api/v3/account/commission", {"symbol": symbol}, signed=True),
            lambda: self.request("futures", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True))
        # Conservative undiscounted rate, including special and tax commissions.
        spot_fee = sum((dec(sc[k]["taker"]) + dec(sc[k]["buyer"]) for k in ("standardCommission", "specialCommission", "taxCommission")), Decimal(0))
        future_fee = dec(fc["takerCommissionRate"])
        sb, fb = self.parallel(lambda: self.depth("spot", symbol), lambda: self.depth("futures", symbol))
        plan = plan_clip(request, sr, fr, sb, fb, time.monotonic(), spot_fee, future_fee, anchor)
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

    def submit(self, market, params):
        return self.request(market, "/api/v3/order" if market == "spot" else "/fapi/v1/order", params, method="POST", signed=True)

    def query(self, market, symbol, client_id):
        return self.request(market, "/api/v3/order" if market == "spot" else "/fapi/v1/order",
                            {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def fills(self, market, symbol, order_id):
        return self.request(market, "/api/v3/myTrades" if market == "spot" else "/fapi/v1/userTrades",
                            {"symbol": symbol, "orderId": order_id, "limit": 1000}, signed=True)
