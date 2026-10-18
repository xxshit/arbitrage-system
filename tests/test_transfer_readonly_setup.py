import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_transfer_readonly.py"
spec = importlib.util.spec_from_file_location("readonly_setup", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TransferReadonlySetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "transfer-readonly.json"
        self.uid, self.gid = os.getuid(), os.getgid()
        self.credentials = {"api_key": "SYNTHETIC_KEY_NOT_REAL", "api_secret": "SYNTHETIC_SECRET_NOT_REAL"}

    def save(self, exchange="Binance", values=None, **options):
        return module.save_credentials(self.path, exchange, values or self.credentials, self.uid, self.gid, **options)

    def test_private_atomic_file_and_no_temp_plaintext_left(self):
        self.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o750)
        self.assertEqual(module.load_config(self.path, self.uid)["exchanges"]["Binance"], self.credentials)
        self.assertEqual(list(self.path.parent.glob(".transfer-readonly-*")), [])

    def test_other_exchanges_are_preserved_and_overwrite_is_explicit(self):
        self.save()
        self.save("Bybit")
        before = self.path.read_bytes()
        with self.assertRaises(module.ConfigurationError):
            self.save()
        self.assertEqual(self.path.read_bytes(), before)
        self.save(values={**self.credentials, "api_secret": "SYNTHETIC_REPLACEMENT"}, replace=True)
        self.assertEqual(set(module.load_config(self.path, self.uid)["exchanges"]), {"Binance", "Bybit"})

    def test_passphrase_is_required_only_for_okx(self):
        with self.assertRaises(module.ConfigurationError):
            self.save("OKX")
        self.save("OKX", {**self.credentials, "passphrase": "SYNTHETIC_PASSPHRASE"})
        self.assertEqual(set(module.load_config(self.path, self.uid)["exchanges"]), {"OKX"})

    def test_unknown_fields_providers_controls_and_blank_inputs_are_rejected(self):
        cases = [("Other", self.credentials), ("Binance", {**self.credentials, "trade": True}), ("Binance", {**self.credentials, "api_secret": "bad\nsecret"}), ("Binance", {**self.credentials, "api_key": ""})]
        for exchange, values in cases:
            with self.subTest(exchange=exchange), self.assertRaises(module.ConfigurationError):
                self.save(exchange, values)
        self.assertFalse(self.path.exists())

    def test_symlink_directory_file_and_lock_are_rejected(self):
        target = Path(self.directory.name) / "target"
        target.mkdir(mode=0o750)
        self.path.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(module.ConfigurationError):
            self.save()
        self.path.parent.unlink()
        self.path.parent.mkdir(mode=0o750)
        secret = target / "original"
        secret.write_text("DO_NOT_CHANGE")
        self.path.symlink_to(secret)
        with self.assertRaises(OSError):
            self.save()
        self.path.unlink()
        (self.path.parent / ".transfer-readonly.lock").unlink()
        (self.path.parent / ".transfer-readonly.lock").symlink_to(secret)
        with self.assertRaises(OSError):
            self.save()
        self.assertEqual(secret.read_text(), "DO_NOT_CHANGE")

    def test_insecure_existing_permissions_and_corruption_are_not_overwritten(self):
        self.save()
        self.path.chmod(0o644)
        with self.assertRaises(module.ConfigurationError):
            self.save("Bybit")
        self.path.chmod(0o640)
        self.path.write_text("not-json")
        with self.assertRaises(module.ConfigurationError):
            self.save("Bybit")
        self.assertEqual(self.path.read_text(), "not-json")

    def test_replace_failure_preserves_prior_file_and_cleans_own_temp(self):
        self.save()
        before = self.path.read_bytes()
        with patch.object(module.os, "replace", side_effect=OSError("synthetic failure")), self.assertRaises(OSError):
            self.save("Bybit")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".transfer-readonly-*")), [])

    def test_cli_rejects_noninteractive_input_and_never_accepts_argv_secrets(self):
        with patch.object(module.sys, "argv", ["configure", "Binance"]), patch.object(module.os, "geteuid", return_value=0), patch.object(module.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": self.gid})()), patch.object(module, "CONFIG_PATH", self.path), patch.object(module.sys.stdin, "isatty", return_value=False), patch.object(module, "hidden_input") as hidden, patch("builtins.print"):
            self.assertEqual(module.main(), 1)
            hidden.assert_not_called()
        self.assertNotIn("add_argument(\"--secret", SCRIPT.read_text())

    def test_hidden_input_cannot_fall_back_to_echoing(self):
        def warn(*_args):
            module.warnings.warn("no tty", module.getpass.GetPassWarning)
        with patch.object(module.getpass, "getpass", side_effect=warn), self.assertRaises(module.getpass.GetPassWarning):
            module.hidden_input("Hidden:")

    def test_bad_cli_arguments_are_not_echoed_back_as_potential_secrets(self):
        output = io.StringIO()
        with patch.object(module.sys, "argv", ["configure", "Binance", "SYNTHETIC_SECRET_NEVER_ECHO"]), patch.object(module.sys, "stderr", output), self.assertRaises(SystemExit):
            module.main()
        self.assertNotIn("SYNTHETIC_SECRET_NEVER_ECHO", output.getvalue())
        self.assertIn("隐藏输入", output.getvalue())

    def test_unexpected_group_and_directory_permissions_fail_closed(self):
        self.save()
        with self.assertRaises(module.ConfigurationError):
            module.load_config(self.path, self.uid, self.gid + 12345)
        self.path.parent.chmod(0o770)
        before = self.path.read_bytes()
        with self.assertRaises(module.ConfigurationError):
            self.save("Bybit")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
