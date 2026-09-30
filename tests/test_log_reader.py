import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.log_reader import parse_entry, sanitize


class LogReaderTest(unittest.TestCase):
    def test_parses_laravel_error_and_application_frame(self):
        event = parse_entry(
            [
                "[2026-09-27 22:00:00] local.ERROR: SQLSTATE[23000]: Column 'phone' cannot be null {\"userId\":1}\n",
                "#9 /srv/app/app/Http/Controllers/ProfileController.php(39): save()\n",
            ]
        )

        self.assertIsNotNone(event)
        self.assertEqual("app/Http/Controllers/ProfileController.php:39", event.top_application_frame)
        self.assertNotIn("23000", event.message)

    def test_sanitizes_credentials_and_email(self):
        cleaned = sanitize("password=hello token:abc person@example.com +84 912 345 678 /Users/wake/project")

        self.assertNotIn("hello", cleaned)
        self.assertNotIn("abc", cleaned)
        self.assertNotIn("person@example.com", cleaned)
        self.assertNotIn("912 345 678", cleaned)
        self.assertNotIn("/Users/wake", cleaned)

    def test_normalized_message_does_not_retain_email(self):
        event = parse_entry(
            ["[2026-09-27 22:00:00] local.ERROR: failed for person@example.com token=abc\n"]
        )

        self.assertNotIn("person@example.com", event.message)
        self.assertNotIn("abc", event.message)


if __name__ == "__main__":
    unittest.main()
