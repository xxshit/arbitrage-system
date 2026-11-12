"""Bounded execution-only books and order events. Never imports the website.

One active symbol/account, two 1000-level snapshots and <=3 armed orders.
No ticker/price history, account contents, URLs or credentials are logged.
Every new session bootstraps again; a gap/disconnect invalidates readiness.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
import re
import threading
import time

from websockets.asyncio.client import connect

from .budget import budget
from .planner import Book, GuardError, dec, symbol_pair


MARKETS = ("spot", "futures")
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
MAX_TRADES = 1000


class DepthBook:
    """Spot U/u and Futures U/u/pu sequence validation after REST snapshot."""
    def __init__(self, market, symbol, snapshot):
        self.market, self.symbol = market, symbol
        self.last = snapshot.update_id
        self.bids, self.asks = dict(snapshot.bids), dict(snapshot.asks)
        self.bid_floor, self.ask_ceiling = snapshot.bids[-1][0], snapshot.asks[-1][0]
        self.bridged = False
        self.current = None

    def apply(self, data, received, age):
        if data.get("e") != "depthUpdate" or data.get("s") != self.symbol:
            raise GuardError("盘口事件身份不匹配")
        first, last = int(data["U"]), int(data["u"])
        if first > last or first < 0:
            raise GuardError("盘口序号无效")
        if last < self.last or (last == self.last and (self.bridged or self.market == "spot")):
            return False
        if not self.bridged:
            target = self.last if self.market == "futures" else self.last+1
            if not first <= target <= last:
                raise GuardError("盘口快照与增量未衔接")
        elif (self.market == "futures" and int(data["pu"]) != self.last) or (self.market == "spot" and first > self.last+1):
            raise GuardError("盘口更新有缺口，须重新读取快照")
        for key, levels in (("b", self.bids), ("a", self.asks)):
            if len(data[key]) > 2000:
                raise GuardError("盘口更新超出容量")
            for p, q in data[key]:
                price, quantity = dec(p), dec(q)
                if price <= 0 or quantity < 0:
                    raise GuardError("盘口档位无效")
                if quantity:
                    levels[price] = quantity
                else:
                    levels.pop(price, None)
            if len(levels) > 4096:
                raise GuardError("盘口达到容量上限，须重新读取快照")
        # Only the range fully covered by the snapshot is known. Do not
        # manufacture deep liquidity from sparse updates beyond its boundary.
        bids = sorted(((p, q) for p, q in self.bids.items() if p >= self.bid_floor), reverse=True)[:100]
        asks = sorted((p, q) for p, q in self.asks.items() if p <= self.ask_ceiling)[:100]
        self.current = Book.parse(dict(lastUpdateId=last, bids=bids, asks=asks), received-age, received)
        self.last, self.bridged = last, True
        return True


class OrderEvents:
    """Only armed client IDs are retained; duplicate/conflicting fills fail closed."""
    def __init__(self):
        self.orders = {}
        self.lock = threading.Lock()

    def arm(self, market, params):
        key = market, params["newClientOrderId"]
        with self.lock:
            if key in self.orders:
                if self.orders[key]["params"] != params:
                    raise GuardError("已登记订单参数发生变化")
                return
            if len(self.orders) >= 3:
                raise GuardError("单次执行事件容量已满")
            self.orders[key] = dict(params=dict(params), raw=None, trades={}, invalid=False)

    def feed(self, market, event):
        if event.get("e") not in ("executionReport", "ORDER_TRADE_UPDATE"):
            return
        row = event if market == "spot" else event["o"]
        key = market, row.get("c")
        with self.lock:
            item = self.orders.get(key)
            if item is None:
                return  # unrelated account activity is neither stored nor logged
            p = item["params"]
            try:
                if (row["s"] != p["symbol"] or row["S"] != p["side"] or row["o"] != p["type"]
                    or row["f"] != p["timeInForce"] or dec(row["q"]) != dec(p["quantity"])
                    or dec(row["p"]) != dec(p["price"])):
                    raise ValueError()
                if market == "futures" and (row["ps"] != p["positionSide"]
                    or row["R"] is not (p.get("reduceOnly") == "true")):
                    raise ValueError()
                quantity = dec(row["z"])
                if not 0 <= quantity <= dec(p["quantity"]):
                    raise ValueError()
                raw = dict(clientOrderId=row["c"], symbol=row["s"], side=row["S"], orderId=row["i"],
                           executedQty=row["z"], status=row["X"], updateTime=int(row["T"]))
                previous = item["raw"]
                if previous and str(previous["orderId"]) != str(raw["orderId"]):
                    raise ValueError()
                if row["x"] == "TRADE":
                    ident = int(row["t"])
                    trade = dict(id=ident, orderId=row["i"], symbol=row["s"], qty=row["l"], price=row["L"],
                                 commission=row["n"], commissionAsset=row["N"], time=int(row["T"]),
                                 isMaker=row["m"], maker=row["m"], isBuyer=row["S"] == "BUY", buyer=row["S"] == "BUY")
                    if ident < 0 or not isinstance(row["N"], str) or not row["N"]:
                        raise ValueError()
                    old = item["trades"].get(ident)
                    if old is not None and old != trade:
                        raise ValueError()
                    if old is None and len(item["trades"]) >= MAX_TRADES:
                        raise ValueError()
                    item["trades"][ident] = trade
                if previous:
                    pq = dec(previous["executedQty"])
                    if quantity < pq or raw["updateTime"] < previous["updateTime"]:
                        return  # an older fill may complete evidence, never roll state back
                    if previous["status"] in TERMINAL and raw["status"] not in TERMINAL:
                        return
                    if previous["status"] in TERMINAL and (quantity != pq or raw["status"] != previous["status"]):
                        raise ValueError()
                item["raw"] = raw
            except (KeyError, ValueError, TypeError, GuardError):
                item["invalid"] = True

    def evidence(self, market, params):
        with self.lock:
            item = self.orders.get((market, params["newClientOrderId"]))
            if not item or item["invalid"] or not item["raw"] or item["raw"]["status"] not in TERMINAL:
                return None
            trades = list(item["trades"].values())
            if sum((dec(t["qty"]) for t in trades), dec(0)) != dec(item["raw"]["executedQty"]):
                return None
            return deepcopy(item["raw"]), deepcopy(trades)


class ExecutionStreams:
    """One asyncio thread for four sockets; fixed lifetime, no history cache.

    Failure is latched for this preview. A new preview builds a fresh session;
    there is no silent reconnect that might hide a missing order event.
    """
    def __init__(self, client, display_symbol):
        self.client, self.symbol = client, symbol_pair(display_symbol)
        self.events = OrderEvents()
        self.lock = threading.Lock()
        self.books, self.live = {}, set()
        self.fault = False
        self.started = time.monotonic()
        self.loop = self.task = None
        self.stopped = threading.Event()
        self.logger = logging.Logger("binance-execution-streams", level=logging.CRITICAL+1)
        self.logger.propagate = False
        self.thread = threading.Thread(target=self._run, name="binance-streams", daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        with self.lock:
            self.fault = True
            self.books.clear()
        if self.loop and self.task and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass  # the stream thread may have closed its loop meanwhile
        self.thread.join(timeout=3)

    def require_ready(self):
        with self.lock:
            if self.fault or self.live != {"spot-depth", "futures-depth", "spot-user", "futures-user"} or time.monotonic()-self.started > 40:
                raise GuardError("盘口或成交订阅未就绪、已断开或已过期，请重新核验")

    def book(self, market, symbol):
        self.require_ready()
        with self.lock:
            book = self.books.get(market)
            if symbol != self.symbol or not book or not 0 <= time.monotonic()-book.started <= .4:
                raise GuardError("持续盘口已过期，请重新核验")
            return book

    def arm(self, market, params):
        self.require_ready()
        self.events.arm(market, params)

    def evidence(self, market, params):
        # Complete, already received fills remain valid after a disconnect;
        # missing events never become evidence of zero execution.
        return self.events.evidence(market, params)

    def _ready(self, name):
        with self.lock:
            self.live.add(name)

    def _socket(self, url):
        return connect(url, open_timeout=2, close_timeout=.2, ping_interval=5, ping_timeout=2,
                       compression=None, max_size=256*1024, max_queue=8, proxy=None, logger=self.logger)

    def _snapshot(self, market):
        start = time.monotonic()
        with budget(start+2):
            raw = self.client.request(market, "/api/v3/depth" if market == "spot" else "/fapi/v1/depth",
                                      {"symbol": self.symbol, "limit": 1000})
        return Book.parse(raw, start, time.monotonic())

    async def _depth(self, market):
        base = "wss://stream.binance.com:9443/ws/" if market == "spot" else "wss://fstream.binance.com/public/ws/"
        async with self._socket(base+self.symbol.lower()+"@depth@100ms") as ws:
            # Subscribe first, then snapshot while websocket's bounded queue
            # buffers deltas. Overflow/disconnect aborts the entire session.
            snapshot = await asyncio.to_thread(self._snapshot, market)
            state = DepthBook(market, self.symbol, snapshot)
            async for frame in ws:
                received = time.monotonic()
                data = json.loads(frame)
                age = self.client.stream_age(market, data["E"])
                if state.apply(data, received, age):
                    with self.lock:
                        self.books[market] = state.current
                    self._ready(market+"-depth")

    async def _spot_user(self):
        async with self._socket("wss://ws-api.binance.com:443/ws-api/v3") as ws:
            await ws.send(json.dumps(dict(id="subscribe", method="userDataStream.subscribe.signature",
                                          params=self.client.stream_signature())))
            subscription = None
            async for frame in ws:
                data = json.loads(frame)
                if data.get("id") == "subscribe":
                    if data.get("status") != 200 or type(data.get("result", {}).get("subscriptionId")) is not int:
                        raise GuardError("成交订阅未确认")
                    subscription = data["result"]["subscriptionId"]
                    self._ready("spot-user")
                elif subscription is not None and data.get("subscriptionId") == subscription:
                    event = data["event"]
                    if event.get("e") == "eventStreamTerminated":
                        raise GuardError("成交订阅已结束")
                    if event.get("e") == "executionReport":
                        self.client.stream_age("spot", event["E"])
                        self.events.feed("spot", event)

    async def _future_user(self):
        key = await asyncio.to_thread(self.client.request, "futures", "/fapi/v1/listenKey", method="POST")
        listen_key = key.get("listenKey")
        if not isinstance(listen_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", listen_key):
            raise GuardError("成交订阅未确认")
        # Subscription lifetime is <=45s, far below listenKey's 60min lifetime.
        # Do not DELETE a key that might be shared by another user session.
        async with self._socket("wss://fstream.binance.com/private/ws/"+listen_key) as ws:
            pong = await ws.ping()
            await asyncio.wait_for(pong, 2)
            self._ready("futures-user")
            async for frame in ws:
                event = json.loads(frame)
                if event.get("e") == "listenKeyExpired":
                    raise GuardError("成交订阅已过期")
                if event.get("e") == "ORDER_TRADE_UPDATE":
                    self.client.stream_age("futures", event["E"])
                    self.events.feed("futures", event)

    async def _serve(self):
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        # DNS and snapshot work share one helper, keeping TasksMax unchanged.
        self.loop.set_default_executor(ThreadPoolExecutor(max_workers=1, thread_name_prefix="binance-stream-io"))
        try:
            if self.stopped.is_set():
                return
            async with asyncio.timeout(45), asyncio.TaskGroup() as group:
                for market in MARKETS:
                    group.create_task(self._channel(self._depth(market)))
                group.create_task(self._channel(self._spot_user()))
                group.create_task(self._channel(self._future_user()))
                await asyncio.sleep(45)
        finally:
            with self.lock:
                self.fault = True
                self.books.clear()

    async def _channel(self, channel):
        try:
            await channel
            raise GuardError("订阅连接已关闭")
        finally:
            with self.lock:
                self.fault = True

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException:
            # Never format websocket exceptions: private URLs contain a token.
            with self.lock:
                self.fault = True
