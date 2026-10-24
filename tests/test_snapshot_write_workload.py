import unittest

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session

from scripts.snapshot_write_workload import SnapshotWriteWorkload, synthetic_tables


class SnapshotWriteWorkloadTests(unittest.TestCase):
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
