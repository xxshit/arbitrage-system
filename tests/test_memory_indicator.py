import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from background_runtime import cgroup_memory_limits, memory_status


class MemoryIndicatorTests(unittest.TestCase):
    def test_limits_are_read_from_service_files_not_a_hardcoded_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.max").write_text("1610612736\n")
            (root / "memory.high").write_text("1342177280\n")
            self.assertEqual(cgroup_memory_limits(root), dict(
                memory_limit_bytes=1610612736, memory_high_bytes=1342177280))
            (root / "memory.max").write_text("805306368\n")
            self.assertEqual(cgroup_memory_limits(root)["memory_limit_bytes"], 805306368)

    def test_unlimited_missing_zero_and_invalid_never_become_finite_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(cgroup_memory_limits(root), dict(memory_limit_bytes=None, memory_high_bytes=None))
            for invalid in ("max", "", "-1", "0", "invalid"):
                (root / "memory.max").write_text(invalid)
                self.assertIsNone(cgroup_memory_limits(root)["memory_limit_bytes"])

    def test_unreadable_limit_does_not_hide_the_other_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.max").mkdir()
            (root / "memory.high").write_text("256")
            self.assertEqual(cgroup_memory_limits(root), dict(memory_limit_bytes=None, memory_high_bytes=256))

    def test_memory_status_uses_its_own_cgroup_and_keeps_new_fields_if_later_stats_missing(self):
        contents = {
            "/proc/self/statm": "100 25 10",
            "/proc/self/cgroup": "0::/system.slice/website.service\n",
            "/sys/fs/cgroup/system.slice/website.service/memory.current": "123456",
            "/sys/fs/cgroup/system.slice/website.service/memory.max": "500000",
            "/sys/fs/cgroup/system.slice/website.service/memory.high": "400000",
        }
        def read(path, *args, **kwargs):
            if str(path) not in contents:
                raise FileNotFoundError
            return contents[str(path)]
        with patch.object(Path, "read_text", read), patch("background_runtime.time.time", return_value=1770000000):
            result = memory_status()
        self.assertEqual(result["cgroup_bytes"], 123456)
        self.assertEqual(result["memory_limit_bytes"], 500000)
        self.assertEqual(result["memory_high_bytes"], 400000)
        self.assertEqual(result["memory_sampled_at_epoch"], 1770000000)
        self.assertIn("rss_bytes", result)
        self.assertNotIn("system.slice", str(result))

    def test_non_linux_runtime_still_works(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(set(memory_status()), {"pid"})


if __name__ == "__main__":
    unittest.main()
