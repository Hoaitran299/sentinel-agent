import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.database import DatabaseUrl
from autofix_agent.dbsetup import AdminLogin, ensure_env_url, generated_target, init_database, user_hosts
from autofix_agent.storage import StateStore


MYSQL_URL = os.environ.get("AI_FIX_TEST_DATABASE_URL", "")


class EnvFileTest(unittest.TestCase):
    def test_appends_url_once_and_keeps_existing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("AI_FIX_PUBLISH_PR=false\nAI_FIX_GITHUB_TOKEN=secret", encoding="utf-8")

            self.assertTrue(ensure_env_url(env, "mysql://a:b@127.0.0.1:3306/x"))
            self.assertFalse(ensure_env_url(env, "mysql://other:c@127.0.0.1:3306/y"))

            text = env.read_text(encoding="utf-8")
            self.assertIn("AI_FIX_GITHUB_TOKEN=secret\n", text)
            self.assertEqual(1, text.count("AI_FIX_DATABASE_URL="))
            self.assertIn("AI_FIX_DATABASE_URL=mysql://a:b@127.0.0.1:3306/x", text)
            self.assertEqual(0o600, env.stat().st_mode & 0o777)

    def test_fills_an_empty_placeholder_line(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("AI_FIX_DATABASE_URL=\nAI_FIX_PUBLISH_PR=false\n", encoding="utf-8")
            ensure_env_url(env, "mysql://a:b@127.0.0.1:3306/x")
            self.assertEqual(
                "AI_FIX_DATABASE_URL=mysql://a:b@127.0.0.1:3306/x\nAI_FIX_PUBLISH_PR=false\n",
                env.read_text(encoding="utf-8"),
            )


class TargetTest(unittest.TestCase):
    def test_generated_url_parses_and_has_a_strong_password(self):
        target = DatabaseUrl.parse(generated_target())
        self.assertEqual(("aifix", "ai_fix_orchestrator", "127.0.0.1", 3306), (target.user, target.database, target.host, target.port))
        self.assertGreaterEqual(len(target.password), 30)
        self.assertNotEqual(target.password, DatabaseUrl.parse(generated_target()).password)

    def test_local_servers_get_both_loopback_hosts(self):
        with patch.dict(os.environ, {"AI_FIX_DB_USER_HOST": ""}):
            self.assertEqual(["127.0.0.1", "localhost"], user_hosts(DatabaseUrl.parse("mysql://u:p@127.0.0.1/d")))
            self.assertEqual(["%"], user_hosts(DatabaseUrl.parse("mysql://u:p@db.internal/d")))

    def test_rejects_unsafe_identifiers(self):
        admin = AdminLogin("127.0.0.1", 3306, "root", "")
        with self.assertRaises(ValueError):
            init_database(admin, DatabaseUrl.parse("mysql://u:p@127.0.0.1/bad-name"))
        with self.assertRaises(ValueError):
            init_database(admin, DatabaseUrl.parse("mysql://u%60x:p@127.0.0.1/ok"))


@unittest.skipUnless(MYSQL_URL, "set AI_FIX_TEST_DATABASE_URL=mysql://root:... to run MySQL tests")
class InitDatabaseMysqlTest(unittest.TestCase):
    def test_creates_user_that_can_migrate_and_is_idempotent(self):
        root = DatabaseUrl.parse(MYSQL_URL)
        admin = AdminLogin(root.host, root.port, root.user, root.password)
        url = generated_target(host=root.host, port=root.port, database="ai_fix_dbinit_test", user="aifix_test")
        target = DatabaseUrl.parse(url)

        # Docker publishes the port, so connections arrive from the bridge network: allow any host.
        first = init_database(admin, target, hosts=["%"])
        second = init_database(admin, target, hosts=["%"])

        self.assertEqual(first, second)
        store = StateStore(target)
        try:
            self.assertEqual([], store.recent())
        finally:
            store.close()
