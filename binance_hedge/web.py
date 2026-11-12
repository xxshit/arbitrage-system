"""Authenticated account setup / confirmation. Placement lives in worker.py."""
import base64
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import SQLAlchemyError

from .client import BinanceClient
from .planner import GuardError, dec
from .store import Store, Vault
from .wakeup import notify


def configured_vault():
    path = os.getenv("BINANCE_KEYRING_FILE") or str(Path(__file__).resolve().parents[1] / ".binance_api_encryption.key")
    file = Path(path)
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise GuardError("币安密钥保险箱权限不正确")
    return Vault(file.read_text().strip())


def public_intent(item):
    # No UID, key ciphertext or account version is exposed to the browser.
    plan = {k: v for k, v in item["plan"].items() if k != "account_uid"}
    result = {k: v for k, v in item["result"].items() if k != "prepared_boot"}
    if "plan" in result:
        result["plan"] = {k: v for k, v in result["plan"].items() if k != "account_uid"}
    return dict(id=item["id"], state=item["state"], plan=plan, result=result,
                created_at=item["created_at"], expires_at=item["expires_at"])


def account_summary(spot, future, permissions):
    return dict(
        checked_at_epoch=time.time(),
        balances=[{k: b[k] for k in ("asset", "free", "locked")} for b in spot["balances"] if dec(b["free"]) or dec(b["locked"])],
        futures_assets=[{k: b.get(k) for k in ("asset", "walletBalance", "availableBalance", "unrealizedProfit")} for b in future["assets"] if dec(b.get("walletBalance", "0"))],
        futures_positions=[{k: p.get(k) for k in ("symbol", "positionSide", "positionAmt", "entryPrice", "unrealizedProfit", "initialMargin", "notional")} for p in future.get("positions", []) if dec(p.get("positionAmt", "0"))],
        spot_trading=permissions.get("enableSpotAndMarginTrading") is True,
        futures_trading=permissions.get("enableFutures") is True)


def register(app, db, current_user, *, client_factory=BinanceClient, vault_factory=configured_vault):
    bp = Blueprint("binance_hedge", __name__, url_prefix="/api/binance")

    def store():
        return Store(db.engine)

    def protect(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return jsonify(error="请先登录"), 401
            if user.role != "admin":
                return jsonify(error="币安账户与实盘首版仅对管理员开放"), 403
            if request.method != "GET":
                token = str(session.get("csrf_token") or "")
                if not token or not secrets.compare_digest(token, request.headers.get("X-CSRF-Token", "")):
                    return jsonify(error="请求校验失败，请刷新页面"), 403
                if request.content_length and request.content_length > 8192:
                    return jsonify(error="请求过大"), 413
            try:
                # Capture scalar ID before returning the application's auth
                # connection. A private exchange call must not occupy MySQL.
                user_id = int(user.id)
                db.session.remove()
                return view(user_id, *args, **kwargs)
            except GuardError as exc:
                return jsonify(error=str(exc)), 409
            except SQLAlchemyError:
                return jsonify(error="币安执行存储暂不可用；未授权重发订单"), 503
            except Exception:
                return jsonify(error="币安模块暂不可用，请检查服务器配置或重新核验"), 503
        return wrapped

    @bp.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    @contextmanager
    def linked_client(user_id):
        row = store().account(user_id)
        if not row:
            raise GuardError("请先在“我的仓位”安全接入币安账户")
        store().reserve_read(user_id)
        credentials = vault_factory().open(user_id, row["sealed"])
        client = client_factory(credentials["key"], credentials["secret"], allow_orders=False)
        client.rate_guard, client.rate_pause = store().check_rate, store().pause_rate
        credentials.clear()
        try:
            yield client, row
        finally:
            client.close()

    def execution_ready():
        return os.getenv("BINANCE_EXECUTION_ENABLED", "0") == "1" and store().ready()

    @bp.get("/account")
    @protect
    def status(user_id):
        row = store().account(user_id)
        try:
            vault_factory()
            configured = True
        except Exception:
            configured = False
        ready = execution_ready()
        return jsonify(configured=configured, connected=bool(row), key_suffix=row["key_suffix"] if row else None,
                       active_intent=row["active_intent"] if row else None, execution_enabled=ready,
                       reason="独立执行器就绪，每次仍需确认实际订单" if ready else "账户读取和盘口核验可用；真实下单尚未启用",
                       executions=[public_intent(i) for i in store().recent(user_id)])

    @bp.post("/account")
    @protect
    def connect(user_id):
        if not request.is_secure and not app.testing:
            raise GuardError("密钥只能经 HTTPS 提交")
        body = request.get_json(silent=True) or {}
        key, secret = body.get("api_key", ""), body.get("api_secret", "")
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]{32,128}", v) for v in (key, secret)):
            raise GuardError("请输入币安 HMAC API Key 和 Secret；密钥不会在保存后返回")
        row = store().account(user_id)
        if row and row["active_intent"]:
            raise GuardError("存在未核清订单，不能更换密钥")
        vault = vault_factory()
        client = client_factory(key, secret, allow_orders=False)
        client.rate_guard, client.rate_pause = store().check_rate, store().pause_rate
        try:
            spot, future, permissions = client.accounts()
            sealed = vault.seal(user_id, key, secret)
            store().save_account(user_id, spot["uid"], sealed, key[-4:])
            return jsonify(connected=True, account=account_summary(spot, future, permissions))
        finally:
            client.close()

    @bp.post("/account/refresh")
    @protect
    def refresh(user_id):
        with linked_client(user_id) as (client, _row):
            return jsonify(account=account_summary(*client.accounts()))

    @bp.delete("/account")
    @protect
    def disconnect(user_id):
        store().disconnect(user_id)
        return jsonify(connected=False)

    @bp.post("/preview")
    @protect
    def preview(user_id):
        body = request.get_json(silent=True) or {}
        allowed = {"symbol", "quote_amount", "clip_quote", "slippage_bps", "min_net_bps", "leverage", "residual_policy", "exit_slippage_bps"}
        if set(body) - allowed or body.get("residual_policy") not in {"reduce", "hold"}:
            raise GuardError("请明确确认单腿差额处置规则")
        with linked_client(user_id) as (client, row):
            if row["active_intent"]:
                raise GuardError("上一组订单尚未核清，请先处理")
            plan, _, _ = client.prepare(body)
            item = store().preview(user_id, row["version"], body, plan)
        notify()
        return jsonify(intent=public_intent(item), execution_enabled=execution_ready())

    @bp.post("/intents/<ident>/confirm")
    @protect
    def confirm(user_id, ident):
        if not execution_ready():
            raise GuardError("独立执行器尚未启用或心跳过期")
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "确认双腿限价FOK和单腿风险" or body.get("intent_id") != ident:
            raise GuardError("请核对实际数量、限价、单批风险和处置规则后确认")
        store().enqueue(user_id, ident)
        notify()
        return jsonify(intent=public_intent(store().get(user_id, ident))), 202

    @bp.get("/intents/<ident>")
    @protect
    def intent(user_id, ident):
        return jsonify(intent=public_intent(store().get(user_id, ident)), execution_enabled=execution_ready())

    @bp.post("/intents/<ident>/reconcile")
    @protect
    def reconcile(user_id, ident):
        if not store().ready():
            raise GuardError("独立核验器未就绪，保留原订单等待恢复")
        store().request_reconcile(user_id, ident)
        notify()
        return jsonify(intent=public_intent(store().get(user_id, ident))), 202

    @bp.post("/intents/<ident>/acknowledge")
    @protect
    def acknowledge(user_id, ident):
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "接受已确认差额并自行处理":
            raise GuardError("请明确确认接管差额；这不会代你补单或平仓")
        store().acknowledge(user_id, ident, body.get("residual_qty"))
        return jsonify(intent=public_intent(store().get(user_id, ident)))

    app.register_blueprint(bp)
