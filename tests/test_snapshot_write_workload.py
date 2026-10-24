import unittest

from sqlalchemy import MetaData, create_engine, update
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.orm import Session

from scripts.snapshot_write_workload import SnapshotWriteWorkload, exact_snapshot_select, synthetic_tables


class SnapshotWriteWorkloadTests(unittest.TestCase):
    def test_mysql_exact_verification_widens_only_float_results(self):
        quotes, _ = synthetic_tables(MetaData())
        query = exact_snapshot_select(quotes[0], "mysql")
        dialect = mysql.dialect()
        dialect.server_version_info = (8, 0, 46)
        sql = str(query.compile(dialect=dialect))
        self.assertEqual(sql.count(" AS DOUBLE) AS field"), 20)
        self.assertNotIn("CAST(latest_market_snapshot.id", sql)
        self.assertNotIn("CAST(latest_market_snapshot.captured_at", sql)
        self.assertEqual(list(query.selected_columns.keys()), list(quotes[0].c.keys()))
        self.assertNotIn("CAST", str(exact_snapshot_select(quotes[0], "sqlite").compile(dialect=sqlite.dialect())))

    def test_large_fractional_values_remain_exact_and_corruption_still_fails(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        quotes, equity = synthetic_tables(metadata)
        metadata.create_all(engine)
        with Session(engine) as session:
            workload = SnapshotWriteWorkload(session, quotes, equity)
            session.commit()
            workload.cycles = 1439
            workload.step(session)
            session.commit()
            self.assertEqual(workload.verify(session)["cycles"], 1440)
            value = workload.expected[quotes[0].name][1]["field0"]
            self.assertIsNotNone(value)
            self.assertGreater(value, 100000)
            session.execute(update(quotes[0]).where(quotes[0].c.id == 1).values(field0=value + .25))
            with self.assertRaisesRegex(AssertionError, "synthetic quote values/counts differ"):
                workload.verify(session)
            session.rollback()
            self.assertTrue(workload.verify(session)["exact_values"])

    def test_varying_shapes_are_lossless_and_compilation_count_stays_bounded(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        quotes, equity = synthetic_tables(metadata)
        metadata.create_all(engine)
        with Session(engine) as session:
            workload = SnapshotWriteWorkload(session, quotes, equity)
            session.commit()
            keys = set(engine._compiled_cache)
            for _ in range(50):
                workload.step(session)
                session.commit()
                self.assertEqual(set(engine._compiled_cache), keys)
            result = workload.verify(session)
            self.assertEqual(result["cycles"], 50)
            self.assertGreater(result["row_updates"], 2500)
            self.assertTrue(result["exact_values"])

    def test_never_reinitializes_an_existing_dataset(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        quotes, equity = synthetic_tables(metadata)
        metadata.create_all(engine)
        with Session(engine) as session:
            first = SnapshotWriteWorkload(session, quotes, equity)
            session.commit()
            with self.assertRaisesRegex(AssertionError, "empty synthetic"):
                SnapshotWriteWorkload(session, quotes, equity)
            session.rollback()
            self.assertEqual(first.verify(session)["quote_rows"], 200)
