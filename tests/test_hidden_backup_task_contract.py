import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "scripts" / "install_local_backup_task.ps1").read_text(encoding="utf-8")
RUNNER = (ROOT / "scripts" / "run_cloud_backup_hidden.vbs").read_text(encoding="utf-8")


class HiddenBackupTaskContractTests(unittest.TestCase):
    def test_scheduled_task_uses_windowless_wscript_runner(self):
        self.assertIn("System32\\wscript.exe", INSTALLER)
        self.assertIn("//B //Nologo", INSTALLER)
        self.assertIn("run_cloud_backup_hidden.vbs", INSTALLER)
        self.assertNotIn('-Execute "powershell.exe"', INSTALLER)

    def test_runner_is_hidden_waits_and_records_output(self):
        self.assertIn("shell.Run(command, 0, True)", RUNNER)
        self.assertIn("cloud-backup-task.log", RUNNER)
        self.assertIn("2>&1", RUNNER)
        self.assertIn("WScript.Quit exitCode", RUNNER)


if __name__ == "__main__":
    unittest.main()
