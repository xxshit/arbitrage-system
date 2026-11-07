"""Execute backup script with a synthetic dump; never contacts MySQL."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BinanceBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        commands = self.root / "bin"
        commands.mkdir()
        scripts = {
            "mariadb-dump": "#!/bin/sh\nprintf '%s\\n' '-- synthetic SQL only' 'CREATE TABLE sample (id INT);'\n",
            "flock": "#!/bin/sh\nexit 0\n",
            "cp": "#!/bin/sh\nif [ \"$1\" = '--reflink=auto' ]; then shift; fi\nexec /bin/cp \"$@\"\n",
            "sha256sum": "#!/usr/bin/env python3\nimport hashlib,sys\nfor p in sys.argv[1:]:\n print(hashlib.sha256(open(p,'rb').read()).hexdigest()+'  '+p)\n",
        }
        for name, code in scripts.items():
            path = commands / name
            path.write_text(code)
            path.chmod(0o700)
        self.chat = self.root / "chat.key"
        self.key = self.root / "binance.key"
        self.chat.write_bytes(b"synthetic-chat-key")
        self.key.write_bytes(b"synthetic-binance-key")
        self.env = dict(os.environ, PATH=str(commands)+":/usr/bin:/bin", BACKUP_ROOT=str(self.root / "mysql"),
                        CHAT_ENCRYPTION_KEY_FILE=str(self.chat), BINANCE_KEYRING_FILE=str(self.key))

    def tearDown(self):
        self.temp.cleanup()

    def run_backup(self):
        return subprocess.run(["/bin/bash", str(ROOT / "scripts/backup_mysql.sh")], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_synthetic_dump_is_paired_with_exact_private_restore_key(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = hashlib.sha256(self.key.read_bytes()).hexdigest()
        saved = self.root / "secrets" / ("binance-"+expected+".key")
        self.assertEqual(saved.read_bytes(), self.key.read_bytes())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        for scope in ("six-hour", "daily"):
            dump, = (self.root / "mysql" / scope).glob("*.sql.gz")
            self.assertEqual(Path(str(dump)+".binance-key.sha256").read_text().strip(), expected)
        self.assertNotIn(self.key.read_text(), result.stdout+result.stderr)

    def test_symlink_key_blocks_backup_before_dump(self):
        self.key.unlink()
        self.key.symlink_to(self.chat)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list((self.root / "mysql").rglob("*.sql.gz")))


if __name__ == "__main__":
    unittest.main()
