import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PULL_SCRIPT = (ROOT / "scripts" / "pull_cloud_backup_macos.sh").read_text(encoding="utf-8")
EXPORT_SCRIPT = (ROOT / "scripts" / "refresh_backup_export.sh").read_text(encoding="utf-8")
BACKUP_SCRIPT = (ROOT / "scripts" / "backup_mysql.sh").read_text(encoding="utf-8")


class MacOSBackupDownloadContractTests(unittest.TestCase):
    def test_download_channel_is_sftp_only_and_uses_dedicated_identity(self):
        self.assertIn("arbitrage_backup_mac", PULL_SCRIPT)
        self.assertIn("arbbackup", PULL_SCRIPT)
        self.assertIn("BatchMode=yes", PULL_SCRIPT)
        self.assertIn("IdentitiesOnly=yes", PULL_SCRIPT)
        self.assertIn("StrictHostKeyChecking=yes", PULL_SCRIPT)
        self.assertNotIn("ssh ", PULL_SCRIPT)
        self.assertNotIn("scp ", PULL_SCRIPT)

    def test_download_verifies_database_without_exporting_chat_key(self):
        self.assertIn("shasum -a 256", PULL_SCRIPT)
        self.assertIn("gzip -t", PULL_SCRIPT)
        self.assertNotIn("chat-encryption.key", PULL_SCRIPT)
        self.assertNotIn("cloud-secrets", PULL_SCRIPT)

    def test_export_uses_immutable_release_and_validates_sources(self):
        self.assertIn("gzip -t", EXPORT_SCRIPT)
        self.assertIn("ACTUAL_BACKUP_HASH", EXPORT_SCRIPT)
        self.assertNotIn("chat-encryption.key", EXPORT_SCRIPT)
        self.assertIn("releases", EXPORT_SCRIPT)
        self.assertIn("LATEST", EXPORT_SCRIPT)
        self.assertNotIn("mariadb-dump", EXPORT_SCRIPT)

    def test_successful_cloud_backup_refreshes_read_only_export(self):
        backup_complete_at = BACKUP_SCRIPT.index("Backup completed:")
        export_at = BACKUP_SCRIPT.index("arbitrage-refresh-backup-export")
        self.assertLess(backup_complete_at, export_at)


if __name__ == "__main__":
    unittest.main()
