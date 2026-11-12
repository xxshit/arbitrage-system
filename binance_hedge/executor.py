"""Durable, single-clip paired FOK coordinator, run outside the web worker.

At-most-once submission: an intent is marked DISPATCHING before either order.
Recovery queries those same IDs; it never resubmits an unknown order. No
subsequent clip is sent automatically, including after partial execution.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from decimal import Decimal
import time

from .client import ExchangeError, OrderGate
from .planner import GuardError, dec, text, plan_clip, reduction_plan
from .budget import BudgetExpired, budget, remaining, submit

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
SEND_BUDGET_SECONDS = 2
RECONCILE_BUDGET_SECONDS = 8
RECOVERY_BUDGET_SECONDS = 5


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
    streams = getattr(client, "streams", None)
    evidence = streams.evidence(market, params) if streams else None
    if evidence:
        result = verify_fills(market, params, *evidence)
        result["evidence_source"] = "user_stream"
        return result
    raw = client.query(market, params["symbol"], params["newClientOrderId"])
    fills = client.fills(market, params["symbol"], str(raw["orderId"])) if dec(raw["executedQty"]) else []
    result = verify_fills(market, params, raw, fills)
    result["evidence_source"] = "rest"
    return result


def verify_fills(market, params, raw, fills):
    result = sanitized_order(raw, params)
    if result["status"] not in TERMINAL:
        raise GuardError("FOK 订单尚未确认终态，暂停后续操作")
    executed = dec(result["executed_qty"])
    if len(fills) > 1000 or (not executed and fills):
        raise GuardError("成交明细与终态数量不一致或超过容量")
    fees, quantity, quote, seen, fill_times = {}, Decimal(0), Decimal(0), set(), []
    if executed:
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

    def place(self, market, params, gate, timing):
        try:
            return sanitized_order(self.client.submit(market, params, gate=gate, timing=timing), params)
        except ExchangeError as exc:
            if not exc.uncertain:
                return {"status": "REJECTED", "executed_qty": "0", "fees": {}, "explicit_rejection": True,
                        "code": exc.code, "first_fill_time_ms": None, "last_fill_time_ms": None}
            return {"status": "UNKNOWN", "code": exc.code}
        except BudgetExpired:
            gate.abort()
            if "write_started_monotonic" not in timing:
                return {"status": "REJECTED", "executed_qty": "0", "fees": {}, "explicit_rejection": True,
                        "code": "DEADLINE", "first_fill_time_ms": None, "last_fill_time_ms": None}
            return {"status": "UNKNOWN", "code": "DEADLINE"}
        except Exception:
            # Includes validation of a response after an actual write.
            gate.abort()
            return {"status": "UNKNOWN", "code": "UNCONFIRMED"}

    def resolve(self, market, params, saved, end=None):
        try:
            with budget(end or time.monotonic()+5):
                if not (saved and saved.get("explicit_rejection")):
                    self.client.ensure_clock(market)
                try:
                    return final_fill(self.client, market, params, saved), None
                except Exception:
                    # An event may arrive while REST is unavailable. Its full
                    # per-trade evidence must pass exactly the same validator.
                    streams = getattr(self.client, "streams", None)
                    evidence = streams.evidence(market, params) if streams else None
                    if not evidence:
                        raise
                    remaining()
                    fill = verify_fills(market, params, *evidence)
                    fill["evidence_source"] = "user_stream"
                    return fill, None
        except Exception as exc:
            reason = str(exc) if isinstance(exc, GuardError) else "该腿成交核验未完成，保留原订单号"
            return saved or {"status": "UNKNOWN"}, reason

    def execute(self, intent, account_uid):
        confirmed = intent.get("result", {}).get("confirmed_at", time.time())
        ttl = min(SEND_BUDGET_SECONDS, intent["expires_at"]-time.time(), confirmed+SEND_BUDGET_SECONDS-time.time())
        if ttl <= 0:
            self.store.state(intent["id"], "ABORTED", {"reason": "确认排队或准备超过两秒；未发送订单"}, release=True)
            return
        send_end = time.monotonic()+ttl
        with budget(send_end):
            prepared = self.prepare_execution(intent, account_uid)
        if prepared is None:
            return
        fresh, sr, fr, params, result, sb, fb = prepared
        end = time.monotonic()+RECONCILE_BUDGET_SECONDS
        result["reconcile_deadline_epoch"] = time.time()+RECONCILE_BUDGET_SECONDS
        result["reconcile_budget_seconds"] = RECONCILE_BUDGET_SECONDS
        self.dispatch(intent, fresh, sr, fr, params, result, sb, fb, send_end, end)

    def prepare_execution(self, intent, account_uid):
        ident, request, anchor = intent["id"], intent["request"], intent["plan"]
        result = {}
        try:
            if time.time() > intent["expires_at"]:
                raise GuardError("确认过期，未发送订单")
            fresh, sr, fr = self.client.prepare(request, anchor=anchor)
            if fresh["account_uid"] != account_uid or fresh["account_uid"] != anchor["account_uid"]:
                raise GuardError("币安账户身份已变化")
            if fresh["quantity"] != anchor["quantity"] or fresh["position_side"] != anchor["position_side"]:
                raise GuardError("可成交数量或持仓模式已变化，请重新确认")
            params = {m: order_params(fresh, m, ident) for m in ("spot", "futures")}
            result = {"orders": params, "plan": fresh, "dispatch": {}, "reconciliation_errors": {}}
            self.store.state(ident, "DISPATCHING", result)
            sb, fb = self.client.parallel(lambda: self.client.depth("spot", fresh["symbol"]),
                                          lambda: self.client.depth("futures", fresh["symbol"]))
            check = plan_clip(request, sr, fr, sb, fb, time.monotonic(),
                              dec(fresh["spot_fee_rate"]), dec(fresh["futures_fee_rate"]), fresh,
                              spot_exit_fee=dec(fresh["spot_exit_fee_rate"]) if fresh.get("spot_exit_fee_rate") is not None else None)
            if check["quantity"] != fresh["quantity"] or time.time() > intent["expires_at"]:
                raise GuardError("落单前深度变化或确认过期，未发送订单")
            remaining()
            streams = getattr(self.client, "streams", None)
            if streams:
                for m, p in params.items():
                    streams.arm(m, p)
        except GuardError as exc:
            result["reason"] = str(exc)
            self.store.state(ident, "ABORTED", result, release=True)
            return
        return fresh, sr, fr, params, result, sb, fb

    def dispatch(self, intent, fresh, sr, fr, params, result, sb, fb, send_end, end):
        ident = intent["id"]
        streams = getattr(self.client, "streams", None)
        def live_check():
            for market in ("spot", "futures"):
                streams.book(market, fresh["symbol"])
        max_age = .4 if streams else 1
        gate = OrderGate(min(send_end, sb.started+max_age, fb.started+max_age,
                            time.monotonic()+intent["expires_at"]-time.time()), check=live_check if streams else None)
        timings = {m: {} for m in params}
        # A completed submission starts its own reconciliation immediately, even
        # while the other submission is still waiting for an HTTP response.
        # Only this coordinator writes the shared result/store, never a network
        # thread. Every per-leg result is durable before the next one is awaited.
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-execution")
        pending = {}
        try:
            with budget(end):
                pending = {submit(pool, self.place, m, p, gate, timings[m]): ("submit", m) for m, p in params.items()}
            while pending:
                done, _ = wait(pending, timeout=max(0, end-time.monotonic()), return_when=FIRST_COMPLETED)
                if not done:
                    gate.abort()
                    for _, market in pending.values():
                        result.setdefault(market, {"status": "UNKNOWN"})
                        result["reconciliation_errors"][market] = "核验超过总期限；禁止重发"
                    break
                for job in done:
                    phase, market = pending.pop(job)
                    if phase == "submit":
                        result[market] = job.result()
                        result["dispatch"][market] = dict(timings[market])
                        self.store.state(ident, "DISPATCHING", result)
                        pending[pool.submit(self.resolve, market, params[market], result[market], end)] = ("resolve", market)
                    else:
                        fill, error = job.result()
                        result[market] = fill
                        if error:
                            result["reconciliation_errors"][market] = error
                        self.store.state(ident, "RECONCILING", result)
        finally:
            pool.shutdown(wait=not pending, cancel_futures=True)
        self.timing(result)
        self.finish(intent, result, rules=(sr, fr), allow_reduction=True, end=end)

    @staticmethod
    def timing(result):
        dispatch = result.get("dispatch", {})
        starts = [dispatch.get(m, {}).get("write_started_monotonic") for m in ("spot", "futures")]
        ends = [dispatch.get(m, {}).get("write_completed_monotonic") for m in ("spot", "futures")]
        result["dispatch_skew_ms"] = round(abs(starts[0]-starts[1])*1000, 3) if all(t is not None for t in starts) else None
        result["request_write_span_ms"] = round((max(ends)-min(starts))*1000, 3) if all(t is not None for t in starts+ends) else None
        result["timing_note"] = "发送计时覆盖本机写入开始及完整请求写完；不代表交易所到达时间或原子成交"

    def reconcile(self, intent, result, *, rules=None, allow_reduction=False):
        ident = intent["id"]
        orders = result.get("orders")
        if not orders:
            self.store.state(ident, "ABORTED", {"reason": "在发送前中断，未产生订单"}, release=True)
            return
        jobs = {m: (m, orders[m]) for m in ("spot", "futures")}
        if result.get("reduction"):
            r = result["reduction"]
            jobs["reduction_fill"] = (r["market"], r["params"])
        result["reconciliation_errors"] = {}
        end = time.monotonic()+RECOVERY_BUDGET_SECONDS
        result["reconcile_deadline_epoch"] = time.time()+RECOVERY_BUDGET_SECONDS
        result["reconcile_budget_seconds"] = RECOVERY_BUDGET_SECONDS
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-reconcile")
        pending = {}
        try:
            pending = {pool.submit(self.resolve, m, p, result.get(key), end): key for key, (m, p) in jobs.items()}
            while pending:
                done, _ = wait(pending, timeout=max(0, end-time.monotonic()), return_when=FIRST_COMPLETED)
                if not done:
                    for key in pending.values():
                        result["reconciliation_errors"][key] = "核验超过总期限；禁止重发"
                    break
                for job in done:
                    key = pending.pop(job)
                    fill, error = job.result()
                    result[key] = fill
                    if error:
                        result["reconciliation_errors"][key] = error
                    self.store.state(ident, "RECONCILING", result)
        finally:
            pool.shutdown(wait=not pending, cancel_futures=True)
        self.timing(result)
        self.finish(intent, result, rules=rules, allow_reduction=allow_reduction, end=end)

    def finish(self, intent, result, *, rules=None, allow_reduction=False, end=None):
        ident, plan = intent["id"], result.get("plan", intent["plan"])
        try:
            if result.get("reconciliation_errors"):
                # Do not leave a stale aggregate usable as a current residual.
                for key in ("net_spot_qty", "short_qty", "residual_qty", "residual_quote", "paired_qty"):
                    result.pop(key, None)
                raise GuardError("部分成交核验未完成；已分别保存两腿结果，禁止重发或按未知数量减仓")
            orders = result["orders"]
            net_spot, short = exposure(result, plan["base"])
            gap = net_spot-short
            result.update(net_spot_qty=text(net_spot), short_qty=text(short), residual_qty=text(gap),
                          residual_quote=text(abs(gap)*max(dec(plan["buy_cap"]), dec(plan["sell_cap"]))),
                          paired_qty=text(min(net_spot, short)))
            first_times = [result[m].get("first_fill_time_ms") for m in ("spot", "futures")]
            result["exchange_fill_skew_ms"] = abs(first_times[0]-first_times[1]) if all(t is not None for t in first_times) else None
            fill_times = [result[m].get(k) for m in ("spot", "futures") for k in ("first_fill_time_ms", "last_fill_time_ms") if result[m].get(k) is not None]
            result["fill_window_ms"] = max(fill_times)-min(fill_times) if fill_times else None
            if any(dec(result[m]["executed_qty"]) not in (0, dec(orders[m]["quantity"])) for m in ("spot", "futures")):
                result["reason"] = "FOK 回报出现非整批成交，暂停自动处理并核验交易所回报"
                self.store.state(ident, "ATTENTION", result)
                return
            if gap and allow_reduction and rules and not result.get("reduction") and intent["request"].get("residual_policy") == "reduce":
                if end is not None and time.monotonic() >= end:
                    result["reason"] = "已确认差额，但本次处置总期限已过；未发送减仓"
                    self.store.state(ident, "ATTENTION", result)
                    return
                with budget(end or time.monotonic()+5):
                    self.reduce(intent, result, gap, rules, end=end)
                return
            if gap:
                state, release = "ATTENTION", False
                result["reason"] = "双腿净数量不一致；已暂停该账户新单，请核对差额和实际成交"
            elif not net_spot and not short:
                state, release = "NO_FILL", True
                result["reason"] = "没有剩余配对仓位；请核对实际成交和已支付费用"
            elif result["exchange_fill_skew_ms"] is None or result["fill_window_ms"] > 500 or result.get("dispatch_skew_ms") is None or result["dispatch_skew_ms"] > 50 or result.get("request_write_span_ms") is None or result["request_write_span_ms"] > 50:
                state, release = "ATTENTION", False
                result["reason"] = "数量已配对，但写入或成交时差超限或缺少证据，暂停后续下单"
            else:
                state, release = "HEDGED", True
                result["reason"] = "两腿净数量已核对，仍不自动继续剩余批次"
            self.store.state(ident, state, result, release=release)
        except GuardError as exc:
            result["reason"] = str(exc)
            self.store.state(ident, "UNKNOWN", result)

    def reduce(self, intent, result, gap, rules, *, end=None):
        plan, request = result["plan"], intent["request"]
        market, rule = ("spot", rules[0]) if gap > 0 else ("futures", rules[1])
        try:
            if request.get("exit_slippage_bps") != plan.get("exit_slippage_bps"):
                raise GuardError("退出价差限额与确认不一致，暂停减仓")
            entry = result[market]
            average = dec(entry["quote"])/dec(entry["executed_qty"])
            depth = self.client.depth(market, plan["symbol"])
            proposal = reduction_plan(request, market, rule, depth, time.monotonic(), gap, average,
                                      position_side=plan["position_side"])
        except GuardError as exc:
            result["reason"] = str(exc)
            result["reduction_blocked_reason"] = str(exc)
            self.store.state(intent["id"], "ATTENTION", result)
            return
        except Exception:
            result["reason"] = "退出盘口读取未完成；未发送减仓单，保留已核实差额"
            self.store.state(intent["id"], "ATTENTION", result)
            return
        params = dict(symbol=plan["symbol"], type="LIMIT", timeInForce="IOC", quantity=proposal["quantity"], price=proposal["price"],
                      side="SELL" if market == "spot" else "BUY", newClientOrderId="ah"+intent["id"]+"r",
                      **({"newOrderRespType": "FULL"} if market == "spot" else {"newOrderRespType": "RESULT", "positionSide": plan["position_side"]}))
        if proposal["reduce_only"]:
            params["reduceOnly"] = "true"
        streams = getattr(self.client, "streams", None)
        if streams:
            try:
                streams.arm(market, params)
            except GuardError:
                result["reason"] = "退出订阅已失效；未发送减仓单，保留已核实差额"
                self.store.state(intent["id"], "ATTENTION", result)
                return
        result["reduction"] = {"market": market, "params": params, "depth_check": proposal}
        self.store.state(intent["id"], "REDUCING", result)
        timing = {}
        gate = OrderGate(min(depth.started+(.4 if streams else 1), end or float("inf")), parties=1,
                         check=(lambda: streams.book(market, plan["symbol"])) if streams else None)
        saved = self.place(market, params, gate, timing)
        result["reduction_dispatch"] = timing
        result["reduction_fill"] = saved
        self.store.state(intent["id"], "REDUCING", result)
        fill, error = self.resolve(market, params, saved, end)
        result["reduction_fill"] = fill
        if error:
            result["reconciliation_errors"]["reduction_fill"] = error
        self.finish(intent, result)
