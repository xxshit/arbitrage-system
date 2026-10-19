import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "funding-rotation-test")
import app as module


class FundingHistoryRotationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for target, name, value in ((module.app, "instance_path", str(self.folder)),
                                    (module, "DUAL_FUNDING_SYNC_CURSOR", None)):
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.targets = [(exchange, f"COIN{index:02}/USDT") for exchange in ("Bybit", "OKX") for index in range(14)]

    def test_venues_are_interleaved_in_each_bounded_batch(self):
        batch = module.dual_funding_history_batch(self.targets)
        self.assertEqual(len(batch), 12)
        self.assertEqual([target[0] for target in batch], ["Bybit", "OKX"] * 6)
        self.assertEqual(len(module.dual_funding_history_batch(self.targets[:2])), 2)
        self.assertEqual(module.dual_funding_history_batch([]), [])

    def test_restart_resumes_next_batch_and_covers_all_targets(self):
        seen = set()
        for _ in range(3):
            module.DUAL_FUNDING_SYNC_CURSOR = None  # emulate a deployment/restart each time
            batch = module.dual_funding_history_batch(self.targets)
            if len(seen) < 24:
                self.assertFalse(seen.intersection(batch))
            seen.update(batch)
            module.save_dual_funding_history_cursor(batch[-1])
        self.assertEqual(seen, set(self.targets))

    def test_removed_cursor_target_and_changed_directory_still_advance(self):
        batch = module.dual_funding_history_batch(self.targets)
        module.save_dual_funding_history_cursor(batch[-1])
        module.DUAL_FUNDING_SYNC_CURSOR = None
        targets = [target for target in self.targets if target != batch[-1]]
        self.assertEqual(module.dual_funding_history_batch(targets)[0], ("Bybit", "COIN06/USDT"))
        module.save_dual_funding_history_cursor(("OKX", "ZZZ/USDT"))
        self.assertEqual(module.dual_funding_history_batch(targets)[0], ("Bybit", "COIN00/USDT"))

    def test_malformed_or_missing_progress_is_safe(self):
        path = self.folder / "dual-funding-history-cursor.json"
        for content in ('{', 'null', '{}', '123', '[null, null]', '["Bybit"]', '["Bybit", 12]', '"' + 'x' * 5000):
            path.write_text(content)
            self.assertEqual(module.dual_funding_history_batch(self.targets)[0], ("Bybit", "COIN00/USDT"))

    def test_failed_disk_write_keeps_in_process_rotation(self):
        batch = module.dual_funding_history_batch(self.targets)
        with patch.object(Path, "write_text", side_effect=OSError("disk unavailable")), patch.object(module.app.logger, "warning") as warning:
            module.save_dual_funding_history_cursor(batch[-1])
        warning.assert_called_once()
        self.assertEqual(module.dual_funding_history_batch(self.targets)[0], ("Bybit", "COIN06/USDT"))


if __name__ == "__main__":
    unittest.main()
