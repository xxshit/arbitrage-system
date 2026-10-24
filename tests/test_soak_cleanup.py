import threading
import unittest
from unittest.mock import Mock, patch

from scripts.soak_public_reads import background_task


class SoakCleanupTests(unittest.TestCase):
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
