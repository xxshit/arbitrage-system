import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "deploy_from_macos.sh").read_text(encoding="utf-8")


class MacOSDeploymentContractTests(unittest.TestCase):
    def test_requires_clean_master_matching_origin(self):
        self.assertIn("git status --porcelain=v1", SCRIPT)
        self.assertIn("git branch --show-current", SCRIPT)
        self.assertIn("git rev-parse origin/master", SCRIPT)

    def test_runs_tests_before_upload_and_deploy(self):
        tests_at = SCRIPT.index("unittest discover -s tests")
        upload_at = SCRIPT.index('scp "${SCP_OPTIONS[@]}" app.py')
        deploy_at = SCRIPT.index("sudo /usr/local/sbin/arbitrage-deploy-staged")
        self.assertLess(tests_at, upload_at)
        self.assertLess(upload_at, deploy_at)

    def test_uses_dedicated_identity_and_fixed_server_commands(self):
        self.assertIn("arbitrage_deploy_mac", SCRIPT)
        self.assertIn("BatchMode=yes", SCRIPT)
        self.assertIn("IdentitiesOnly=yes", SCRIPT)
        self.assertIn("StrictHostKeyChecking=yes", SCRIPT)
        self.assertIn("sudo /usr/local/sbin/arbitrage-verify-runtime", SCRIPT)
        self.assertNotIn("root@", SCRIPT)


if __name__ == "__main__":
    unittest.main()
