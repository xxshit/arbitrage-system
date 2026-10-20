import copy
from datetime import datetime, timedelta
import json
import pathlib
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import Column, DateTime, Float, Integer, JSON, MetaData, Table, create_engine, event
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from snapshot_persistence import changed_values, equity_payload_update, json_delta, update_snapshots


class SnapshotDeltaTests(unittest.TestCase):
    def setUp(self):
        self.table = Table("latest_market_snapshot", MetaData(), Column("id", Integer, primary_key=True),
                           Column("price", Float), Column("rate", Float), Column("captured_at", DateTime))
        self.at = datetime(2026, 10, 21)

    def test_existing_mysql_float_precision_is_not_a_false_change(self):
        previous = SimpleNamespace(price=0.10000000149011612, rate=None, captured_at=self.at)
        self.assertEqual(changed_values(self.table, previous, {"price": .1, "rate": None, "captured_at": self.at}, "mysql"), {})
        self.assertEqual(changed_values(self.table, previous, {"price": .11, "rate": 0}, "mysql"), {"price": .11, "rate": 0})
        self.assertEqual(changed_values(self.table, previous, {"price": .1}, "sqlite"), {"price": .1})

    def test_heartbeat_is_not_dropped_even_when_prices_are_equal(self):
        prior = SimpleNamespace(price=1, rate=.005, captured_at=self.at)
        new = dict(price=1, rate=.005, captured_at=self.at+timedelta(seconds=15))
        self.assertEqual(changed_values(self.table, prior, new, "mysql"), {"captured_at": new["captured_at"]})

    def test_no_timestamp_promotion_beyond_existing_storage_precision(self):
        prior = SimpleNamespace(captured_at=self.at)
        self.assertEqual(changed_values(self.table, prior, {"captured_at": self.at.replace(microsecond=999999)}, "mysql"), {})

    def test_bounded_batch_preserves_values_nulls_and_untouched_rows(self):
        engine = create_engine("sqlite://")
        self.table.metadata.create_all(engine)
        statements = []
        event.listen(engine, "before_cursor_execute", lambda conn, cur, sql, params, ctx, many: statements.append(sql))
        with Session(engine) as session:
            session.execute(self.table.insert(), [dict(id=i, price=i, rate=.005, captured_at=self.at) for i in range(251)])
            session.commit()
            updates = [dict(id=i, price=None if i % 3 == 0 else i+.2, captured_at=self.at+timedelta(seconds=15)) for i in range(250)]
            statements.clear()
            update_snapshots(session, self.table, updates)
            session.commit()
            self.assertEqual(len([s for s in statements if s.startswith("UPDATE")]), 3)
            rows = session.execute(self.table.select().order_by(self.table.c.id)).mappings().all()
            for row, expected in zip(rows, updates):
                self.assertEqual(row["price"], expected["price"])
                self.assertEqual(row["captured_at"], expected["captured_at"])
                self.assertEqual(row["rate"], .005)
            self.assertEqual((rows[-1]["price"], rows[-1]["captured_at"]), (250, self.at))
            update_snapshots(session, self.table, [dict(id=1, price=9999)])
            session.rollback()
            self.assertEqual(session.execute(self.table.select().where(self.table.c.id == 1)).mappings().one()["price"], 1.2)
        engine.dispose()

    def test_empty_updates_do_not_emit_sql_and_account_tables_are_rejected(self):
        session = Mock()
        update_snapshots(session, self.table, [dict(id=1)])
        session.execute.assert_not_called()
        with self.assertRaises(ValueError):
            update_snapshots(session, Table("user_account", MetaData()), [])


class EquityDeltaTests(unittest.TestCase):
    def test_unchanged_metadata_is_not_in_patch_and_json_null_is_explicit(self):
        old = dict(metadata="issuer evidence" * 100, rows=[dict(price=1, rate=.005, error=None)])
        new = copy.deepcopy(old)
        new["rows"][0].update(price=2, rate=None)
        sets, removes = json_delta(old, new)
        self.assertEqual(sets, [('$."rows"[0]."price"', 2), ('$."rows"[0]."rate"', None)])
        self.assertEqual(removes, [])
        self.assertEqual(json_delta(old, old), ([], []))

    def test_deleted_keys_lists_and_escaped_names(self):
        old = {'data': {"delete": 1, 'a"b': 2}, "rows": [1, 2, 3], "evidence": "x"*2000}
        new = {'data': {'a"b': 3}, "rows": [3, 1], "evidence": "x"*2000}
        sets, removes = json_delta(old, new)
        self.assertIn(('$."rows"', [3, 1]), sets)
        self.assertIn(('$."data"."a\\"b"', 3), sets)
        self.assertEqual(removes, ['$."data"."delete"'])

    def test_dense_or_oversized_patches_fall_back_to_exact_full_value(self):
        self.assertIsNone(json_delta(dict(a=1), dict(a=2)))
        self.assertIsNone(json_delta(dict(x=[1]*20, evidence="z"*2000), dict(x=[2]*20, evidence="z"*2000), limit=5))

    def test_boolean_and_number_are_not_confused(self):
        sets, _ = json_delta(dict(flag=True, evidence="x"*2000), dict(flag=1, evidence="x"*2000))
        self.assertEqual(sets, [('$."flag"', 1)])

    def test_mysql_expression_is_bound_and_preserves_full_row_images(self):
        table = Table("equity_spot_snapshot", MetaData(), Column("id", Integer), Column("payload", JSON), Column("updated_at", DateTime))
        session = Mock()
        session.get_bind.return_value.dialect.name = "mysql"
        session.get_bind.return_value.dialect.is_mariadb = False
        old = dict(metadata="x"*2000, rate=.01, gone=1)
        new = dict(metadata="x"*2000, rate=None)
        equity_payload_update(session, table, old, new, datetime(2026,10,21))
        compiled = session.execute.call_args.args[0].compile(dialect=mysql.dialect())
        self.assertIn("json_set(json_remove(", str(compiled))
        self.assertIn("CAST(", str(compiled))
        self.assertIn("null", compiled.params.values())
        for key, value in compiled.params.items():
            if value == "null":
                self.assertNotIn(key, compiled._bind_processors)
        self.assertNotIn("x"*100, str(compiled.params))
        session.reset_mock()
        equity_payload_update(session, table, old, old, datetime(2026,10,21))
        self.assertNotIn("payload=", str(session.execute.call_args.args[0]))


class CompressionPolicyTests(unittest.TestCase):
    def test_syntax_read_only_default_and_safety_contract(self):
        script = pathlib.Path(__file__).resolve().parents[1] / "scripts/configure_mysql_log_compression.sh"
        subprocess.run(["bash", "-n", str(script)], check=True)
        source = script.read_text()
        self.assertIn('mode="${1:---check}"', source)
        self.assertIn('configure_mysql_log_retention.sh" --check', source)
        self.assertLess(source.index('configure_mysql_log_retention.sh" --check'), source.index("SET PERSIST"))
        self.assertIn("binlog_transaction_compression = ON", source)
        self.assertIn("binlog_transaction_compression_level_zstd = 3", source)
        for forbidden in ("PURGE", "rm -", "TRUNCATE", "RESET MASTER", "systemctl", "GRANT", "SET PERSIST binlog_row_image", "sql_log_bin ="):
            self.assertNotIn(forbidden, source)
