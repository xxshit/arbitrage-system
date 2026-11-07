"""Durable, single-clip paired FOK coordinator, run outside the web worker.

At-most-once submission: an intent is marked DISPATCHING before either order.
Recovery queries those same IDs; it never resubmits an unknown order. No
subsequent clip is sent automatically, including after partial execution.
"""
from decimal import Decimal
import threading
import time

from .client import ExchangeError, SendBlocked
from .planner import GuardError, dec, floor, ceil, text, validate_books

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def order_params(plan, market, ident):
    return dict(symbol=plan["symbol"], side="BUY" if market == "spot" else "SELL", type="LIMIT",
                timeInForce="FOK", quantity=plan["quantity"],
                price=plan["buy_cap"] if market == "spot" else plan["sell_cap"],
                newClientOrderId="ah" + ident + ("s" if market == "spot" else "f"),
                **({"newOrderRespType": "FULL"} if market == "spot" else
                   {"newOrderRespType": "RESULT", "positionSide": plan["position_side"]}))


def sanitized_order(raw, params):
    if raw.get("clientOrderId") != params["newClientOrderId"] or raw.get("symbol") != params["symbol"] or raw.get("side") != params["side"]:
        raise GuardError("成交回报的订单身份不匹配")
    quantity = dec(raw["executedQty"])
    if quantity < 0 or quantity > dec(params["quantity"]):
        raise GuardError("成交数量超出本次授权")
    return dict(order_id=str(raw["orderId"]), status=raw["status"], executed_qty=text(quantity),
                quote=text(raw.get("cummulativeQuoteQty", raw.get("cumQuote", "0"))),
                exchange_time_ms=int(raw.get("transactTime", raw.get("updateTime", 0))))


def final_fill(client, market, params, saved=None):
    if saved and saved.get("explicit_rejection"):
        return saved
    raw = client.query(market, params["symbol"], params["newClientOrderId"])
    result = sanitized_order(raw, params)
    if result["status"] not in TERMINAL:
        raise GuardError("FOK 订单尚未确认终态，暂停后续操作")
    executed = dec(result["executed_qty"])
    fees, quantity, quote, seen, fill_times = {}, Decimal(0), Decimal(0), set(), []
    if executed:
        fills = client.fills(market, params["symbol"], result["order_id"])
        for fill in fills:
            if str(fill.get("orderId")) != result["order_id"] or fill.get("symbol") != params["symbol"] or fill["id"] in seen:
                raise GuardError("成交明细身份重复或不匹配")
            seen.add(fill["id"])
            fill_times.append(int(fill["time"]))
            q, price, fee = dec(fill["qty"]), dec(fill["price"]), dec(fill["commission"])
            if q <= 0 or price <= 0 or fee < 0:
                raise GuardError("成交明细数量或手续费异常")
            maker = fill.get("isMaker" if market == "spot" else "maker")
            buyer = fill.get("isBuyer" if market == "spot" else "buyer")
            if maker is not False or buyer is not (params["side"] == "BUY"):
                raise GuardError("成交明细未证明预期方向的吃单成交")
            if (params["side"] == "BUY" and price > dec(params["price"])) or (params["side"] == "SELL" and price < dec(params["price"])):
                raise GuardError("成交价格违反确认限价，立即暂停核验")
            quantity += q
            quote += q * price
            asset = str(fill["commissionAsset"])
            fees[asset] = fees.get(asset, Decimal(0)) + fee
        # Includes the 1000-row pagination boundary: no guessed fees / quantities.
        if quantity != executed:
            raise GuardError("成交明细尚未完整，不能按估算数量对冲")
    result.update(quote=text(quote), fees={k: text(v) for k, v in fees.items()}, fills_verified=True,
                  first_fill_time_ms=min(fill_times) if fill_times else None,
                  last_fill_time_ms=max(fill_times) if fill_times else None)
    return result


def exposure(result, base):
    spot, future = result["spot"], result["futures"]
    net_spot = dec(spot["executed_qty"]) - dec(spot.get("fees", {}).get(base, "0"))
    short = dec(future["executed_qty"])
    if result.get("reduction_fill"):
        fill, market = result["reduction_fill"], result["reduction"]["market"]
        if market == "spot":
            net_spot -= dec(fill["executed_qty"]) + dec(fill.get("fees", {}).get(base, "0"))
        else:
            short -= dec(fill["executed_qty"])
    if net_spot < 0 or short < 0:
        raise GuardError("成交净数量异常，需要人工核对")
    return net_spot, short


class Executor:
    def __init__(self, store, client):
        self.store, self.client = store, client

    def execute(self, intent, account_uid):
        ident, request, anchor = intent["id"], intent["request"], intent["plan"]
        result = {}
        try:
            if time.time() > intent["expires_at"]:
                raise GuardError("确认过期，未发送订单")
            fresh, sr, fr = self.client.prepare(request, anchor=anchor)
            if fresh["account_uid"] != account_uid or fresh["account_uid"] != anchor["account_uid"]:
                raise GuardError("币安账户身份已变化")
            # Never silently change the displayed quantity / position mode.
            if fresh["quantity"] != anchor["quantity"] or fresh["position_side"] != anchor["position_side"]:
                raise GuardError("可成交数量或持仓模式已变化，请重新确认")
            params = {m: order_params(fresh, m, ident) for m in ("spot", "futures")}
            result = {"orders": params, "plan": fresh, "dispatch": {}}
            self.store.state(ident, "DISPATCHING", result)
            # Durable write can take time; recheck books afterwards using the
            # warmed connections. No DB transaction remains open here.
            sb, fb = self.client.parallel(lambda: self.client.depth("spot", fresh["symbol"]),
                                          lambda: self.client.depth("futures", fresh["symbol"]))
            from .planner import plan_clip
            check = plan_clip(request, sr, fr, sb, fb, time.monotonic(), dec(fresh["spot_fee_rate"]), dec(fresh["futures_fee_rate"]), fresh)
            if check["quantity"] != fresh["quantity"] or time.time() > intent["expires_at"]:
                raise GuardError("落单前深度变化或确认过期，未发送订单")
            barrier, dispatch_lock = threading.Barrier(2), threading.Lock()
            deadline = min(sb.started, fb.started) + 1

            def before_send(market):
                # Called inside the adapter's connection lock, after signing
                # and shared rate-limit checks, immediately before socket IO.
                with dispatch_lock:
                    at = time.monotonic()
                    prior = list(result["dispatch"].values())
                    if at > deadline or (prior and abs(at-prior[0]["monotonic"]) > .05):
                        raise SendBlocked()
                    result["dispatch"][market] = {"monotonic": at, "utc_ms": int(time.time()*1000)}
            self.client.before_order_send = before_send

            def submit(market):
                try:
                    barrier.wait(timeout=.5)
                    # Exactly ONE placement request. Timeouts are queried later.
                    return sanitized_order(self.client.submit(market, params[market]), params[market])
                except ExchangeError as exc:
                    if not exc.uncertain:
                        return {"status": "REJECTED", "executed_qty": "0", "fees": {}, "explicit_rejection": True, "code": exc.code}
                    return {"status": "UNKNOWN", "code": exc.code}
                except Exception:
                    return {"status": "UNKNOWN", "code": "UNCONFIRMED"}

            result["spot"], result["futures"] = self.client.parallel(lambda: submit("spot"), lambda: submit("futures"))
            self.client.before_order_send = None
            times = [v["monotonic"] for v in result["dispatch"].values()]
            result["dispatch_skew_ms"] = round(abs(times[0]-times[1])*1000, 3) if len(times) == 2 else None
            self.store.state(ident, "RECONCILING", result)
        except GuardError as exc:
            # This path is only before submission; after submit all failures
            # stay in reconcile, including process/DB failures.
            result["reason"] = str(exc)
            self.store.state(ident, "ABORTED", result, release=True)
            return
        self.reconcile(intent, result, rules=(sr, fr), allow_reduction=True)

    def reconcile(self, intent, result, *, rules=None, allow_reduction=False):
        ident, plan = intent["id"], result.get("plan", intent["plan"])
        try:
            orders = result.get("orders")
            if not orders:
                self.store.state(ident, "ABORTED", {"reason": "在发送前中断，未产生订单"}, release=True)
                return
            self.client.sync_clocks()
            for market in ("spot", "futures"):
                result[market] = final_fill(self.client, market, orders[market], result.get(market))
            if result.get("reduction"):
                r = result["reduction"]
                result["reduction_fill"] = final_fill(self.client, r["market"], r["params"], result.get("reduction_fill"))
            net_spot, short = exposure(result, plan["base"])
            gap = net_spot - short
            result.update(net_spot_qty=text(net_spot), short_qty=text(short), residual_qty=text(gap),
                          residual_quote=text(abs(gap) * max(dec(plan["buy_cap"]), dec(plan["sell_cap"]))),
                          paired_qty=text(min(net_spot, short)))
            result["exchange_fill_skew_ms"] = abs(result["spot"]["first_fill_time_ms"]-result["futures"]["first_fill_time_ms"]) if net_spot and short else None
            fill_times = [result[m].get(k) for m in ("spot", "futures") for k in ("first_fill_time_ms", "last_fill_time_ms") if result[m].get(k) is not None]
            result["fill_window_ms"] = max(fill_times)-min(fill_times) if fill_times else None
            result["timing_note"] = "发送时差为本机调度测量；成交时差来自两市场成交时间戳，不代表原子成交"
            if any(dec(result[m]["executed_qty"]) not in (0, dec(orders[m]["quantity"])) for m in ("spot", "futures")):
                result["reason"] = "FOK 回报出现非整批成交，暂停自动处理并核验交易所回报"
                self.store.state(ident, "ATTENTION", result)
                return
            if gap and allow_reduction and rules and not result.get("reduction") and intent["request"].get("residual_policy") == "reduce":
                self.reduce(intent, result, gap, rules)
                return
            if gap:
                # Even sub-lot fee dust is explicit; no fake "fully hedged".
                state, release = "ATTENTION", False
                result["reason"] = "双腿净数量不一致；已暂停该账户新单，请核对差额和实际成交"
            elif not net_spot and not short:
                state, release = "NO_FILL", True
            elif result["exchange_fill_skew_ms"] is None or result["fill_window_ms"] > 500 or result.get("dispatch_skew_ms") is None or result["dispatch_skew_ms"] > 50:
                state, release = "ATTENTION", False
                result["reason"] = "数量已配对，但时差超限或缺少时差证据，暂停后续下单"
            else:
                state, release = "HEDGED", True
            self.store.state(ident, state, result, release=release)
        except Exception as exc:
            result["reason"] = str(exc) if isinstance(exc, GuardError) else "对账未完成，保留原订单号，禁止重发"
            self.store.state(ident, "UNKNOWN", result)

    def reduce(self, intent, result, gap, rules):
        plan, request = result["plan"], intent["request"]
        market, rule = ("spot", rules[0]) if gap > 0 else ("futures", rules[1])
        quantity = floor(abs(gap) / (1 + dec(plan["spot_fee_rate"])) if market == "spot" else abs(gap), rule.step)
        # One bounded reduction, no attempt to enlarge the missing leg.
        slip = dec(request.get("slippage_bps", "10")) / 10000
        price = ceil(dec(plan["buy_cap"]) * (1-slip), rule.tick) if market == "spot" else floor(dec(plan["sell_cap"]) * (1+slip), rule.tick)
        try:
            rule.check(quantity, price)
        except GuardError:
            self.reconcile(intent, result)
            return
        params = dict(symbol=plan["symbol"], type="LIMIT", timeInForce="IOC", quantity=text(quantity), price=text(price),
                      side="SELL" if market == "spot" else "BUY", newClientOrderId="ah"+intent["id"]+"r",
                      **({"newOrderRespType": "FULL"} if market == "spot" else {"newOrderRespType": "RESULT", "positionSide": plan["position_side"]}))
        if market == "futures" and plan["position_side"] == "BOTH":
            params["reduceOnly"] = "true"
        result["reduction"] = {"market": market, "params": params}
        self.store.state(intent["id"], "REDUCING", result)
        try:
            result["reduction_fill"] = sanitized_order(self.client.submit(market, params), params)
        except Exception:
            result["reduction_fill"] = {"status": "UNKNOWN"}
        self.reconcile(intent, result)
