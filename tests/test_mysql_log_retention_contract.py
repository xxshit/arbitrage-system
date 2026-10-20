import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/configure_mysql_log_retention.sh"


class RetentionSafetyTests(unittest.TestCase):
    def test_shell_syntax_and_read_only_default(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
        source = SCRIPT.read_text()
        self.assertIn('mode="${1:---check}"', source)
        self.assertIn('if [[ "$mode" == --apply ]]', source)

    def test_backup_and_replication_checks_precede_persistent_policy(self):
        source = SCRIPT.read_text()
        apply_at = source.index("SET PERSIST")
        for check in ("28800", "sha256sum -c", "gzip -t", "cmp -s", "SHOW REPLICAS", "replication_connection_configuration", '[[ -z "$replicas" ]]'):
            self.assertLess(source.index(check), apply_at)
        self.assertIn("binlog_expire_logs_seconds = 172800", source)
        self.assertIn("binlog_expire_logs_auto_purge = ON", source)

    def test_never_deletes_files_or_business_rows_or_restarts_mysql(self):
        source = SCRIPT.read_text()
        for forbidden in ("rm -", "TRUNCATE", "DELETE FROM", "RESET MASTER", "systemctl restart", "binlog_format =", "sql_log_bin ="):
            self.assertNotIn(forbidden, source)
