import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.process import CommandError, ProcessExecutor, safe_environment


class SafeEnvironmentTest(unittest.TestCase):
    def test_does_not_forward_application_or_publisher_credentials(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "telegram-secret",
                "TELEGRAM_CHAT_ID": "chat-secret",
                "GITHUB_TOKEN": "github-secret",
                "DB_PASSWORD": "database-secret",
                "AI_FIX_DATABASE_URL": "mysql://aifix:state-secret@127.0.0.1/ai_fix",
                "AI_FIX_LOG_S3_SECRET_ACCESS_KEY": "s3-secret",
                "AI_FIX_LOG_HTTP_TOKEN": "log-token",
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
            },
            clear=False,
        ):
            environment = safe_environment()

        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)
        self.assertNotIn("TELEGRAM_CHAT_ID", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("DB_PASSWORD", environment)
        self.assertNotIn("AI_FIX_DATABASE_URL", environment)
        self.assertNotIn("AI_FIX_LOG_S3_SECRET_ACCESS_KEY", environment)
        self.assertNotIn("AI_FIX_LOG_HTTP_TOKEN", environment)
        self.assertNotIn("SSH_AUTH_SOCK", environment)

    def test_command_error_keeps_the_useful_failure_tail(self):
        executor = ProcessExecutor()

        with self.assertRaises(CommandError) as caught:
            executor.run(
                [sys.executable, "-c", "print('ASSERTION: phone is required'); print('Duration: 1s'); raise SystemExit(1)"],
                cwd=Path.cwd(),
                timeout=10,
            )

        self.assertIn("ASSERTION: phone is required", str(caught.exception))
        self.assertIsNotNone(caught.exception.result)

    def test_command_error_prioritizes_framework_root_cause_over_stack_tail(self):
        executor = ProcessExecutor()
        script = (
            "print('Next Illuminate\\\\View\\\\ViewException: Vite manifest not found')\n"
            "[print('#%s stack frame' % index) for index in range(80)]\n"
            "raise SystemExit(1)"
        )

        with self.assertRaises(CommandError) as caught:
            executor.run([sys.executable, "-c", script], cwd=Path.cwd(), timeout=10)

        self.assertIn("Vite manifest not found", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
