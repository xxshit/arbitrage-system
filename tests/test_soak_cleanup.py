import threading
import unittest
from unittest.mock import Mock, patch

from scripts.soak_public_reads import background_task, check_integrity, synthetic_database_url
from scripts.soak_snapshot_writes import isolated_mysql_url


class SoakCleanupTests(unittest.TestCase):
    def test_default_database_is_explicit_synthetic_sqlite(self):
        self.assertEqual(synthetic_database_url('/tmp/synthetic-only'), 'sqlite:////tmp/synthetic-only/synthetic.sqlite3')

    def test_mysql_uses_instance_guard_before_returning_database_url(self):
        with patch('scripts.soak_snapshot_writes.isolated_mysql_url', return_value='guarded') as guard:
            self.assertEqual(synthetic_database_url('/tmp/unused','/tmp/arbiscope-read-mysql.TEST/mysql.sock'),'guarded')
        guard.assert_called_once_with('/tmp/arbiscope-read-mysql.TEST/mysql.sock')

    def test_mysql_rejects_production_or_unguarded_socket_before_connecting(self):
        with patch('scripts.soak_snapshot_writes.create_engine') as engine:
            for path in ('/run/mysqld/mysqld.sock','/tmp/mysql.sock','/tmp/arbiscope-read-mysql.TEST/not-mysql.sock'):
                with self.assertRaises(AssertionError): isolated_mysql_url(path)
        engine.assert_not_called()

    def test_mysql_integrity_rejects_bad_table_status(self):
        session, metadata = Mock(), Mock()
        metadata.sorted_tables = [Mock(name='table')]
        metadata.sorted_tables[0].name = 'synthetic_table'
        session.execute.return_value.all.return_value = [('db.synthetic_table','check','status','OK')]
        check_integrity(session,metadata,'mysql')
        self.assertEqual(str(session.execute.call_args.args[0]), 'CHECK TABLE `synthetic_table`')
        session.execute.return_value.all.return_value = [('db.synthetic_table','check','error','damaged')]
        with self.assertRaises(AssertionError): check_integrity(session,metadata,'mysql')

    def test_normal_exit_stops_and_joins_synthetic_worker(self):
        stop = threading.Event()
        with background_task(stop.wait, stop, name="test-soak-history") as worker:
            self.assertTrue(worker.is_alive())
            self.assertTrue(worker.daemon)
        self.assertTrue(stop.is_set())
        self.assertFalse(worker.is_alive())

    def test_timeout_keeps_original_failure_and_stops_worker(self):
        stop = threading.Event()
        with self.assertRaisesRegex(TimeoutError, "synthetic request deadline"):
            with background_task(stop.wait, stop, name="test-soak-timeout") as worker:
                raise TimeoutError("synthetic request deadline")
        self.assertTrue(stop.is_set())
        self.assertFalse(worker.is_alive())

    def test_stuck_worker_cannot_be_reported_as_success(self):
        stop = threading.Event()
        worker = Mock()
        worker.is_alive.return_value = True
        worker.join.side_effect = lambda timeout: self.assertTrue(stop.is_set())
        with patch("scripts.soak_public_reads.threading.Thread", return_value=worker) as factory:
            with self.assertRaisesRegex(RuntimeError, "synthetic_history_worker_did_not_stop"):
                with background_task(lambda: None, stop, name="bounded-test", join_timeout=.01):
                    pass
        self.assertTrue(factory.call_args.kwargs["daemon"])
        worker.join.assert_called_once_with(.01)


if __name__ == "__main__":
    unittest.main()
