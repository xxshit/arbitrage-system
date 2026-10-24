"""Deterministic synthetic load for the two public snapshot writers.

Only test runners with an already-isolated database may instantiate this.
Never imports app, deletes rows, changes DB settings or commits for the caller.
"""
import copy
from datetime import datetime, timedelta
import random

from sqlalchemy import DateTime, Float, Integer, JSON, String, select

from snapshot_persistence import equity_payload_update, update_snapshots


class SnapshotWriteWorkload:
    def __init__(self, session, quote_tables, equity_table):
        assert {table.name for table in quote_tables} == {
            "latest_market_snapshot", "latest_dual_futures_snapshot"}
        assert equity_table.name == "equity_spot_snapshot"
        self.tables, self.equity = tuple(quote_tables), equity_table
        self.random = random.Random(17462)
        self.at = datetime(2026, 10, 24)
        self.expected, self.fields = {}, {}
        self.cycles = 0
        self.row_updates = 0
        self.payload = {"metadata": "synthetic issuer reference" * 200,
                        "rows": [{"id": i, "price": 1, "rate": .005, "flag": True,
                                  "evidence": "synthetic only" * 15} for i in range(100)]}
        for table in self.tables:
            assert session.execute(select(table.c.id).limit(1)).first() is None, "requires empty synthetic tables"
            self.fields[table.name] = [column.name for column in table.c if isinstance(column.type, Float)]
            assert len(self.fields[table.name]) >= 9
            rows = []
            for identity in range(1, 101):
                values = {"id": identity}
                for column in table.c:
                    if column.name == "id":
                        continue
                    if isinstance(column.type, Float):
                        values[column.name] = 0.0
                    elif isinstance(column.type, DateTime):
                        values[column.name] = self.at
                    elif isinstance(column.type, String):
                        values[column.name] = f"SYN{identity}/USDT" if column.name == "symbol" else "synthetic"
                    elif isinstance(column.type, Integer):
                        values[column.name] = 0
                    else:
                        raise AssertionError("unexpected synthetic column type")
                rows.append(values)
            session.execute(table.insert(), rows)
            self.expected[table.name] = {row["id"]: dict(row) for row in rows}
        assert session.execute(select(equity_table.c.id).limit(1)).first() is None
        session.execute(equity_table.insert(), dict(id=1, payload=self.payload, updated_at=self.at))

    def step(self, session):
        stamp = self.at + timedelta(seconds=(self.cycles + 1) * 5)
        for table in self.tables:
            fields = self.random.sample(self.fields[table.name], self.random.randint(9, len(self.fields[table.name])))
            count = self.random.randint(25, 100)
            rows = []
            for identity in range(1, count + 1):
                # Exactly representable even by MySQL FLOAT; NULLs exercise
                # mixed CASE arms without changing storage precision policy.
                values = {key: (None if table.c[key].nullable and (identity + self.cycles) % 13 == 0
                                else float((self.cycles % 10000) * 1000 + identity * 4 + offset) / 4)
                          for offset, key in enumerate(fields)}
                values["captured_at"] = stamp
                rows.append(dict(id=identity, **values))
                self.expected[table.name][identity].update(values)
            update_snapshots(session, table, rows)
            self.row_updates += count
        current = copy.deepcopy(self.payload)
        for identity in self.random.sample(range(100), self.random.randint(1, 40)):
            current["rows"][identity].update(price=self.cycles + identity / 4,
                                              rate=None if identity % 3 else 0,
                                              flag=bool(self.cycles % 2))
        if self.cycles % 2:
            current.pop("temporary", None)
        else:
            current["temporary"] = {"odd\"key": self.cycles}
        equity_payload_update(session, self.equity, self.payload, current, stamp)
        self.payload = current
        self.cycles += 1

    def verify(self, session):
        for table in self.tables:
            rows = {row["id"]: dict(row) for row in session.execute(select(table)).mappings()}
            assert rows == self.expected[table.name], "synthetic quote values/counts differ"
        row = session.execute(select(self.equity)).mappings().one()
        assert row["id"] == 1 and row["payload"] == self.payload
        assert row["updated_at"] == self.at + timedelta(seconds=self.cycles * 5)
        return {"exact_values": True, "quote_rows": 200, "equity_rows": 1,
                "cycles": self.cycles, "row_updates": self.row_updates}


def synthetic_tables(metadata):
    """Standalone Linux/MySQL load without importing the application."""
    from sqlalchemy import Column, Table
    quotes = tuple(Table(name, metadata, Column("id", Integer, primary_key=True, autoincrement=False),
                         *(Column(f"field{i}", Float) for i in range(20)), Column("captured_at", DateTime))
                   for name in ("latest_market_snapshot", "latest_dual_futures_snapshot"))
    equity = Table("equity_spot_snapshot", metadata, Column("id", Integer, primary_key=True),
                   Column("payload", JSON), Column("updated_at", DateTime))
    return quotes, equity
