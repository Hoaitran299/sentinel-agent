import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.config import Config
from autofix_agent.policy import PolicyViolation, WorkspacePolicy
from autofix_agent.process import ProcessExecutor


class WorkspacePolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        (self.repo / "app/Http/Requests").mkdir(parents=True)
        (self.repo / "tests/Feature/Profile").mkdir(parents=True)
        (self.repo / "app/Http/Requests/ProfileUpdateRequest.php").write_text("<?php\n", encoding="utf-8")
        (self.repo / "tests/Feature/Profile/ProfileTest.php").write_text("<?php\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "base"],
            cwd=self.repo,
            check=True,
        )
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "source_repo": str(self.repo),
                    "log_path": str(self.root / "laravel.log"),
                    "workspace_root": str(self.root / "worktrees"),
                    "allowed_paths": ["app/Http/Requests/ProfileUpdateRequest.php", "tests/Feature/Profile/"],
                    "protected_paths": ["composer.json", ".github/"],
                    "max_changed_files": 4,
                    "max_diff_lines": 100,
                    "require_new_test": True,
                }
            ),
            encoding="utf-8",
        )
        self.config = Config.load(config_path)
        self.policy = WorkspacePolicy(self.config, ProcessExecutor())

    def tearDown(self):
        self.temp.cleanup()

    def test_allows_source_change_and_new_test(self):
        baseline = self.policy.capture(self.repo)
        (self.repo / "app/Http/Requests/ProfileUpdateRequest.php").write_text("<?php\n// fixed\n", encoding="utf-8")
        (self.repo / "tests/Feature/Profile/MissingPhoneTest.php").write_text("<?php\n", encoding="utf-8")

        changed = self.policy.enforce(self.repo, baseline)

        self.assertEqual(
            ["app/Http/Requests/ProfileUpdateRequest.php", "tests/Feature/Profile/MissingPhoneTest.php"],
            changed,
        )

    def test_rejects_modifying_existing_test(self):
        baseline = self.policy.capture(self.repo)
        (self.repo / "tests/Feature/Profile/ProfileTest.php").write_text("<?php\n// weakened\n", encoding="utf-8")

        with self.assertRaisesRegex(PolicyViolation, "existing test"):
            self.policy.enforce(self.repo, baseline)

    def test_rejects_a_fix_without_a_new_regression_test(self):
        baseline = self.policy.capture(self.repo)
        (self.repo / "app/Http/Requests/ProfileUpdateRequest.php").write_text("<?php\n// fixed\n", encoding="utf-8")

        with self.assertRaisesRegex(PolicyViolation, "new regression test"):
            self.policy.enforce(self.repo, baseline)


if __name__ == "__main__":
    unittest.main()
