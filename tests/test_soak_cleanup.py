import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

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

    def test_mysql_integrity_checks_every_table_on_a_separate_guarded_connection(self):
        from sqlalchemy import MetaData, Table
        from sqlalchemy.dialects.mysql import dialect
        from sqlalchemy.engine import make_url
        from sqlalchemy.pool import NullPool

        session = Mock()
        database = "snapshot_soak_" + "1" * 32
        socket_dir = "/tmp/arbiscope-read-mysql.test"
        url = make_url(f"mysql+pymysql://root@localhost/{database}?unix_socket={socket_dir}/mysql.sock")
        # Flask-SQLAlchemy adds its default charset when it creates the engine.
        from flask import Flask
        from flask_sqlalchemy import SQLAlchemy
        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = url
        db = SQLAlchemy(app)
        with app.app_context():
            url = db.engine.url
            self.assertEqual(url.query["charset"], "utf8mb4")
            db.engine.dispose()
        session.get_bind.return_value.url = url
        metadata = MetaData()
        for name in ("event", "alert_pulse"):
            Table(name, metadata)
        connection = MagicMock()
        connection.dialect = dialect()
        tables = [("event", "BASE TABLE"), ("alert_pulse", "BASE TABLE")]
        identity = (database, socket_dir + "/data/", 1, 83024)
        def run(last_status="OK", actual=tables, received=identity):
            names = []
            def table_check(sql):
                name = sql.split("`")[1]
                names.append(name)
                return Mock(all=lambda: [(database+"."+name, "check", "status",
                                          last_status if name == "event" else "OK")])
            connection.execute.side_effect = [Mock(one=lambda: received), actual]
            connection.exec_driver_sql.side_effect = table_check
            with patch("sqlalchemy.create_engine") as create, patch("pathlib.Path.is_socket", return_value=True):
                audit = create.return_value
                audit.connect.return_value.__enter__.return_value = connection
                try:
                    result = check_integrity(session, metadata, "mysql")
                finally:
                    self.assertIs(create.call_args.kwargs["poolclass"], NullPool)
                    self.assertEqual(create.call_args.args[0], url)
                    self.assertEqual(create.call_args.kwargs["connect_args"]["read_timeout"], 120)
                    audit.dispose.assert_called_once()
                    audit.connect.return_value.__exit__.assert_called_once()
                    session.execute.assert_not_called()
            self.assertEqual(names, ["alert_pulse", "event"])
            return result
        self.assertEqual(set(run()), {"event", "alert_pulse"})
        session.rollback.assert_called_once()
        with self.assertRaisesRegex(AssertionError, "event"):
            run(last_status="damaged")
        with self.assertRaisesRegex(AssertionError, "table set"):
            run(actual=tables[:1])
        with self.assertRaisesRegex(ValueError, "identity changed"):
            run(received=(database, "/var/lib/mysql", 0, 1))
        for bad_url in (url.set(database="production"), url.set(host="remote"),
                        url.set(query={"unix_socket": "/run/mysqld/mysqld.sock"}),
                        url.update_query_dict({"charset": "latin1"}),
                        url.update_query_dict({"read_default_file": "/example.cnf"})):
            session.get_bind.return_value.url = bad_url
            with patch("sqlalchemy.create_engine") as create, self.assertRaisesRegex(ValueError, "private synthetic"):
                check_integrity(session, metadata, "mysql")
            create.assert_not_called()

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
