"""Short transactions for credentials and durable execution intents.

Schema installation is explicit. Importing this module neither connects nor
creates tables. No ORM session / connection is held over an exchange request.
"""
import base64
import hashlib
import json
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import MetaData, Table, Column, Integer, String, Text, Double, select, insert, update, delete
from sqlalchemy.exc import IntegrityError

from .planner import GuardError

metadata = MetaData()
accounts = Table("binance_account_link", metadata,
    Column("user_id", Integer, primary_key=True), Column("uid_hash", String(64), unique=True, nullable=False),
    Column("sealed", Text, nullable=False), Column("key_suffix", String(8), nullable=False),
    Column("version", String(32), nullable=False), Column("active_intent", String(32)),
    Column("updated_at", Double, nullable=False), Column("next_read_at", Double, nullable=False, default=0))
intents = Table("binance_hedge_intent", metadata,
    Column("id", String(32), primary_key=True), Column("user_id", Integer, nullable=False, index=True),
    Column("account_version", String(32), nullable=False), Column("state", String(24), nullable=False, index=True),
    Column("request_json", Text, nullable=False), Column("plan_json", Text, nullable=False),
    Column("result_json", Text, nullable=False), Column("created_at", Double, nullable=False),
    Column("updated_at", Double, nullable=False), Column("expires_at", Double, nullable=False))
workers = Table("binance_executor_health", metadata,
    Column("id", Integer, primary_key=True), Column("boot", String(32), nullable=False),
    Column("seen_at", Double, nullable=False))


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Vault:
    def __init__(self, encoded):
        try:
            key = base64.b64decode(encoded, validate=True)
            if len(key) != 32:
                raise ValueError()
        except Exception:
            raise GuardError("服务器尚未配置独立的币安密钥加密密钥") from None
        self.cipher = AESGCM(key)

    def seal(self, user_id, key, secret):
        nonce = os.urandom(12)
        ciphertext = self.cipher.encrypt(nonce, dump({"key": key, "secret": secret}).encode(), str(user_id).encode())
        return base64.b64encode(nonce + ciphertext).decode()

    def open(self, user_id, sealed):
        try:
            data = base64.b64decode(sealed, validate=True)
            return json.loads(self.cipher.decrypt(data[:12], data[12:], str(user_id).encode()))
        except Exception:
            raise GuardError("账户密钥无法解密；请核对恢复密钥") from None


class Store:
    def __init__(self, engine):
        self.engine = engine

    def install(self):
        metadata.create_all(self.engine)
        with self.engine.begin() as con:
            if not con.execute(select(workers.c.id).where(workers.c.id == 2)).first():
                con.execute(insert(workers).values(id=2, boot="rate-limit", seen_at=0))

    def check_rate(self):
        with self.engine.connect() as con:
            until = con.execute(select(workers.c.seen_at).where(workers.c.id == 2)).scalar()
        if until is None or time.time() < until:
            raise GuardError("币安请求已暂停，等待限流冷却结束")

    def pause_rate(self, seconds):
        # Shared by website and executor; a new client/process cannot reset a
        # 429/418 pause. Only aggregates, never credentials or request params.
        until = time.time() + seconds
        with self.engine.begin() as con:
            con.execute(update(workers).where(workers.c.id == 2, workers.c.seen_at < until).values(seen_at=until))

    def account(self, user_id):
        with self.engine.connect() as con:
            row = con.execute(select(accounts).where(accounts.c.user_id == user_id)).mappings().first()
            return dict(row) if row else None

    def save_account(self, user_id, uid, sealed, suffix):
        values = dict(uid_hash=hashlib.sha256(str(uid).encode()).hexdigest(), sealed=sealed,
                      key_suffix=suffix, version=secrets.token_hex(16), updated_at=time.time())
        try:
            with self.engine.begin() as con:
                row = con.execute(select(accounts).where(accounts.c.user_id == user_id).with_for_update()).mappings().first()
                if row and row["active_intent"]:
                    raise GuardError("该账户仍有未核清的订单，不能替换密钥")
                if row:
                    con.execute(update(accounts).where(accounts.c.user_id == user_id).values(**values))
                else:
                    con.execute(insert(accounts).values(user_id=user_id, **values))
        except IntegrityError:
            raise GuardError("该币安账户已被绑定，请使用原站内账户") from None

    def disconnect(self, user_id):
        with self.engine.begin() as con:
            row = con.execute(select(accounts).where(accounts.c.user_id == user_id).with_for_update()).mappings().first()
            if row and row["active_intent"]:
                raise GuardError("存在未核清订单，不能移除用于对账的密钥")
            con.execute(delete(accounts).where(accounts.c.user_id == user_id))

    def preview(self, user_id, account_version, request, plan):
        now, ident = time.time(), secrets.token_hex(16)
        with self.engine.begin() as con:
            con.execute(insert(intents).values(id=ident, user_id=user_id, account_version=account_version,
                state="PREVIEW", request_json=dump(request), plan_json=dump(plan), result_json="{}",
                created_at=now, updated_at=now, expires_at=now+15))
        return self.get(user_id, ident)

    def reserve_read(self, user_id):
        now = time.time()
        with self.engine.begin() as con:
            changed = con.execute(update(accounts).where(accounts.c.user_id == user_id,
                accounts.c.next_read_at <= now).values(next_read_at=now+5))
            if changed.rowcount != 1:
                raise GuardError("账户请求正在处理或过于频繁，请稍后再试")

    @staticmethod
    def unpack(row):
        if not row:
            raise GuardError("订单意图不存在")
        result = dict(row)
        for key in ("request", "plan", "result"):
            result[key] = json.loads(result.pop(key + "_json"))
        return result

    def get(self, user_id, ident):
        with self.engine.connect() as con:
            return self.unpack(con.execute(select(intents).where(intents.c.id == ident, intents.c.user_id == user_id)).mappings().first())

    def recent(self, user_id):
        with self.engine.connect() as con:
            return [self.unpack(row) for row in con.execute(select(intents).where(intents.c.user_id == user_id,
                intents.c.state != "PREVIEW").order_by(intents.c.created_at.desc()).limit(50)).mappings()]

    def enqueue(self, user_id, ident):
        with self.engine.begin() as con:
            # Account row serializes all intents, including repeated requests
            # with different idempotency IDs. No in-process-only mutex.
            account = con.execute(select(accounts).where(accounts.c.user_id == user_id).with_for_update()).mappings().first()
            intent = con.execute(select(intents).where(intents.c.id == ident, intents.c.user_id == user_id).with_for_update()).mappings().first()
            if not account or not intent:
                raise GuardError("账户或订单意图不存在")
            if intent["state"] != "PREVIEW":
                return  # same confirmation cannot create another order
            if account["active_intent"] or account["version"] != intent["account_version"]:
                raise GuardError("账户已有未核清订单或密钥已变更")
            if time.time() > intent["expires_at"]:
                raise GuardError("确认已超过 15 秒，请重新读取盘口")
            con.execute(update(accounts).where(accounts.c.user_id == user_id).values(active_intent=ident))
            con.execute(update(intents).where(intents.c.id == ident).values(state="QUEUED", updated_at=time.time()))

    def request_reconcile(self, user_id, ident):
        with self.engine.begin() as con:
            changed = con.execute(update(intents).where(intents.c.id == ident, intents.c.user_id == user_id,
                intents.c.state.in_(["UNKNOWN", "ATTENTION"])).values(state="QUERY_QUEUED", updated_at=time.time()))
            if changed.rowcount != 1:
                raise GuardError("当前状态不需要再次对账，或已经在处理中")

    def acknowledge(self, user_id, ident, residual):
        with self.engine.begin() as con:
            row = con.execute(select(intents).where(intents.c.id == ident, intents.c.user_id == user_id).with_for_update()).mappings().first()
            if not row or row["state"] != "ATTENTION":
                raise GuardError("只有已确认终态的差额可以由本人接管；未知订单不能解除")
            result = json.loads(row["result_json"])
            if str(result.get("residual_qty")) != str(residual) or not all(result.get(m, {}).get("fills_verified") or result.get(m, {}).get("explicit_rejection") for m in ("spot", "futures")):
                raise GuardError("差额或成交证据不一致，请重新核对")
            result["acknowledged_at"] = time.time()
            result["reason"] = "用户已核对并接管剩余风险；不表示完全对冲或已经平仓"
            con.execute(update(intents).where(intents.c.id == ident).values(state="ACKNOWLEDGED", result_json=dump(result), updated_at=time.time()))
            con.execute(update(accounts).where(accounts.c.user_id == user_id, accounts.c.active_intent == ident).values(active_intent=None))

    def claim(self, allow_orders=True):
        with self.engine.begin() as con:
            row = con.execute(select(intents).where(intents.c.state.in_(["QUEUED", "QUERY_QUEUED"] if allow_orders else ["QUERY_QUEUED"])).order_by(intents.c.created_at).limit(1).with_for_update()).mappings().first()
            if not row:
                return None
            result = con.execute(update(intents).where(intents.c.id == row["id"], intents.c.state == row["state"]).values(state="CHECKING" if row["state"] == "QUEUED" else "RECONCILING", updated_at=time.time()))
            return self.unpack(row) if result.rowcount == 1 else None

    def state(self, ident, state, result, *, release=False):
        with self.engine.begin() as con:
            con.execute(update(intents).where(intents.c.id == ident).values(state=state, result_json=dump(result), updated_at=time.time()))
            if release:
                con.execute(update(accounts).where(accounts.c.active_intent == ident).values(active_intent=None))

    def unfinished(self):
        with self.engine.connect() as con:
            return [self.unpack(r) for r in con.execute(select(intents).where(intents.c.state.in_(
                ["CHECKING", "DISPATCHING", "RECONCILING", "REDUCING", "UNKNOWN", "ATTENTION"]))).mappings()]

    def beat(self, boot):
        with self.engine.begin() as con:
            if con.execute(select(workers.c.id).where(workers.c.id == 1)).first():
                con.execute(update(workers).where(workers.c.id == 1).values(boot=boot, seen_at=time.time()))
            else:
                con.execute(insert(workers).values(id=1, boot=boot, seen_at=time.time()))

    def ready(self):
        with self.engine.connect() as con:
            at = con.execute(select(workers.c.seen_at).where(workers.c.id == 1)).scalar()
        return at is not None and 0 <= time.time() - at < 15
