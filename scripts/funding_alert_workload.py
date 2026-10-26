"""Nonempty funding-alert load for an already isolated synthetic application.

Uses the real writer and real wall/monotonic clocks, never upstream HTTP. The
intent journal is captured before database serialization; final checks read
Core rows back, so the ORM identity map cannot hide persistence corruption.
"""
import json
from pathlib import Path
import threading
import time

from sqlalchemy import event, select


class FundingAlertWorkload:
    def __init__(self, module, contracts=524):
        engine = module.db.engine
        if engine.dialect.name == "sqlite":
            path = engine.url.database
            assert path in (None, "", ":memory:") or (
                Path(path).name == "synthetic.sqlite3"
                and Path(path).parent.name.startswith("arbiscope-public-soak-"))
        else:
            assert engine.dialect.name == "mysql" and not engine.dialect.is_mariadb
            assert engine.url.database.startswith("snapshot_soak_")
            socket_path = Path(engine.url.query.get("unix_socket", ""))
            assert socket_path.parent.parent == Path("/tmp")
            assert socket_path.parent.name.startswith("arbiscope-read-mysql.")
            assert socket_path.name == "mysql.sock"
        assert contracts >= 32
        assert module.FundingAlertState.query.count() == 0
        assert module.FundingAlertEvent.query.count() == 0
        assert not module.FUNDING_ALERT_LIVE_STATES
        self.module = module
        self.contracts = [f"SYNFUND{i:04d}USDT" for i in range(contracts)]
        self.expected_states, self.expected_events, self.latest = {}, {}, {}
        self.cycles = self.state_writes = self.http_checks = self.active_http_checks = 0
        self.http_lock = threading.Lock()

    def step(self, cycle):
        module = self.module
        now, fetched_at = time.time(), time.monotonic()
        source_ms = int(now * 1000)
        boundary_ms = (int(now // 3600) + 1) * 3600 * 1000
        instruments, rates, metadata, latest = [], [], [], {}
        for index, contract in enumerate(self.contracts):
            changed = 16 <= index < 32 and (cycle // 12) % 2 == 1
            interval = 1 if index < 16 or changed else 4
            rate = .01 if changed else .003
            instruments.append(dict(symbol=contract, status="TRADING", quoteAsset="USDT",
                                    contractType="PERPETUAL", underlyingType="COIN"))
            rates.append(dict(symbol=contract, lastFundingRate=rate, time=source_ms,
                              nextFundingTime=boundary_ms))
            metadata.append(dict(symbol=contract, adjustedFundingRateCap=.01,
                                 adjustedFundingRateFloor=-.01, fundingIntervalHours=interval))
            latest[contract] = dict(contract=contract, symbol=contract[:-4]+"/USDT",
                                    exchange="Binance", rate=rate*100, cap=1., floor=-1.,
                                    interval=float(interval), next_at=boundary_ms/1000)
        for endpoint, payload in (("exchangeInfo", {"symbols": instruments}),
                                  ("premiumIndex", rates), ("fundingInfo", metadata)):
            module.MARKET_PAYLOAD_CACHE["https://fapi.binance.com/fapi/v1/"+endpoint] = {
                "payload": payload, "fetched_at": fetched_at}
        pending_states, pending_events = {}, {}

        def capture(mapper, connection, target):
            values = {column.key: getattr(target, column.key) for column in mapper.columns}
            if isinstance(target, module.FundingAlertState):
                pending_states[target.contract] = values
            else:
                pending_events[target.id] = values

        listeners = [(model, action) for model in (module.FundingAlertState, module.FundingAlertEvent)
                     for action in ("after_insert", "after_update")]
        for model, action in listeners:
            event.listen(model, action, capture)
        try:
            emitted = module.refresh_funding_alerts()
        finally:
            for model, action in listeners:
                event.remove(model, action, capture)
        assert emitted == len(pending_events)
        live = module.FUNDING_ALERT_LIVE_STATES
        assert set(live) == set(self.contracts), "funding writer skipped generated contracts"
        for contract, values in latest.items():
            assert {key: live[contract][key] for key in values} == values
            assert abs(live[contract]["rate_at"] - source_ms/1000) < .01
            assert now-.01 <= live[contract]["info_at"] <= time.time()
        self.expected_states.update(pending_states)
        self.expected_events.update(pending_events)
        self.latest = latest
        self.state_writes += len(pending_states)
        self.cycles += 1
        return emitted

    def verify_http(self, payload):
        groups = {item["contract"]: item for item in payload["groups"]}
        assert len(groups) == len(payload["groups"]), "duplicate funding group"
        histories = {}
        for row in sorted(self.expected_events.values(), key=lambda item: (-item["observed_at_ms"], item["id"])):
            histories.setdefault(row["contract"], []).append(json.loads(row["evidence_json"]))
        required = set(self.contracts[:16]) | set(histories)
        assert set(groups) == required, "funding HTTP is empty or lost generated history"
        for contract, item in groups.items():
            assert {key: item[key] for key in self.latest[contract]} == self.latest[contract]
            assert item["history"] == histories.get(contract, []), "funding HTTP history values differ"
        active = payload["active_events"]
        assert len({item["contract"] for item in active}) == len(active)
        assert all(item["id"] in self.expected_events and item["notice"] for item in active)
        with self.http_lock:
            self.http_checks += 1
            self.active_http_checks += bool(active)

    def verify(self):
        module = self.module
        states = {row["contract"]: dict(row) for row in module.db.session.execute(
            select(module.FundingAlertState.__table__)).mappings()}
        events = {row["id"]: dict(row) for row in module.db.session.execute(
            select(module.FundingAlertEvent.__table__)).mappings()}
        assert states == self.expected_states, "full durable funding states differ"
        assert events == self.expected_events, "full durable funding events differ"
        assert self.http_checks > 0, "nonempty funding HTTP was not exercised"
        return dict(exact_values=True, state_rows=len(states), event_rows=len(events),
                    cycles=self.cycles, state_writes=self.state_writes,
                    http_checks=self.http_checks, active_http_checks=self.active_http_checks,
                    real_clocks=True, settlement_history="not fabricated; separate regressions cover confirmations")
