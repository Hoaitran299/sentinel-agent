import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.config import Config, parse_events
from autofix_agent.notifier import TelegramNotifier
from autofix_agent.pipeline import AgentPipeline
from autofix_agent.process import safe_environment
from autofix_agent.storage import StateStore
from test_dashboard import event, make_config


TOKEN = "123456789:" + "A" * 35
INCIDENT = {
    "id": "7dfba14a" + "0" * 24,
    "message": "Column 'phone' cannot be null <script>alert(1)</script>",
    "occurrence_count": 3,
    "threshold": 3,
    "pull_request_url": None,
}


class Recorder:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if self.error:
            raise self.error
        return {"username": "aifix_bot"} if method == "getMe" else {}


class TelegramNotifierTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(make_config(Path(self.temp.name)), telegram_enabled=True, telegram_chat_id="-1001234567890")

    def notifier(self, transport, **overrides):
        return TelegramNotifier(replace(self.config, **overrides), transport=transport)

    def test_success_message_is_html_escaped_and_links_the_pr(self):
        sent = Recorder()
        self.notifier(sent).notify(
            "succeeded", INCIDENT, summary="Add phone validation", branch="ai-fix/x",
            commit="d6987267fc11225b", pull_request_url="https://github.com/o/r/pull/1", report="run.md",
        )
        method, payload = sent.calls[0]
        text = payload["text"]
        self.assertEqual(("sendMessage", "-1001234567890", "HTML"), (method, payload["chat_id"], payload["parse_mode"]))
        self.assertIn("✅ <b>Fix verified and ready for review</b>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn('<a href="https://github.com/o/r/pull/1">', text)
        self.assertIn("<code>d6987267fc11</code>", text)

    def test_uses_report_language(self):
        sent = Recorder()
        self.notifier(sent, report_language="vi").notify("started", INCIDENT)
        self.assertIn("AI agent bắt đầu sửa", sent.calls[0][1]["text"])
        sent = Recorder()
        self.notifier(sent, report_language="ja").notify("failed", INCIDENT, reason="boom")
        self.assertIn("理由: boom", sent.calls[0][1]["text"])

    def test_disabled_or_filtered_events_send_nothing(self):
        sent = Recorder()
        self.notifier(sent, telegram_enabled=False).notify("failed", INCIDENT)
        self.notifier(sent, telegram_events=["failed"]).notify("started", INCIDENT)
        self.assertEqual([], sent.calls)

    def test_fix_only_mode_mutes_and_report_only_sends_reported(self):
        from autofix_agent.settings import RuntimeSettings

        sent = Recorder()
        pipeline = AgentPipeline.__new__(AgentPipeline)
        pipeline.notifier = self.notifier(sent, telegram_events=["reported", "started"])
        pipeline.apply_settings(RuntimeSettings("fix_only", 3, 600))
        pipeline.notifier.notify("started", INCIDENT)
        self.assertEqual([], sent.calls)

        pipeline.apply_settings(RuntimeSettings("report_only", 3, 600))
        pipeline.notifier.notify("reported", INCIDENT)
        self.assertIn("📣", sent.calls[0][1]["text"])
        self.assertIn("Run AI fix", sent.calls[0][1]["text"])

    def test_transport_errors_never_escape_notify(self):
        with redirect_stdout(io.StringIO()) as output:
            self.notifier(Recorder(error=RuntimeError("network down"))).notify("failed", INCIDENT)
        self.assertIn("Telegram notification failed", output.getvalue())

    def test_http_errors_do_not_leak_the_bot_token(self):
        notifier = TelegramNotifier(self.config)
        error = urllib.error.HTTPError(
            "https://api.telegram.org/bot{}/sendMessage".format(TOKEN), 401, "Unauthorized", {},
            io.BytesIO(json.dumps({"description": "Unauthorized " + TOKEN}).encode()),
        )
        with patch.dict(os.environ, {"AI_FIX_TELEGRAM_BOT_TOKEN": TOKEN}), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                notifier.preflight()
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn("api.telegram.org", str(caught.exception))

    def test_malformed_token_is_rejected_before_any_request(self):
        with patch.dict(os.environ, {"AI_FIX_TELEGRAM_BOT_TOKEN": "not-a-token"}), patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(RuntimeError):
                TelegramNotifier(self.config).preflight()
        urlopen.assert_not_called()

    def test_token_is_not_forwarded_to_claude_or_verification(self):
        with patch.dict(os.environ, {"AI_FIX_TELEGRAM_BOT_TOKEN": TOKEN, "AI_FIX_TELEGRAM_CHAT_ID": "-100"}):
            environment = safe_environment()
        self.assertNotIn(TOKEN, json.dumps(environment))

    def test_only_the_first_repeat_after_a_fix_is_announced(self):
        store = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(store.close)
        decision = store.record_event(event(), "b" * 40, 1, 600)
        store.claim(decision.incident_id)
        store.complete_incident(decision.incident_id, "ai-fix/x", "c" * 40, "r.md", "https://github.com/o/r/pull/1")

        sent = Recorder()
        pipeline = AgentPipeline.__new__(AgentPipeline)
        pipeline.store = store
        pipeline.notifier = self.notifier(sent, telegram_events=["repeated"])
        for _ in range(3):
            repeat = store.record_event(event(), "b" * 40, 1, 600)
            pipeline.notify_repeat(repeat.incident_id, repeat.suppressed_count)

        self.assertEqual(1, len(sent.calls))
        self.assertIn("🔁", sent.calls[0][1]["text"])


class TelegramConfigTest(unittest.TestCase):
    def load(self, env):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "source").mkdir()
            path = root / "config" / "demo.json"
            path.write_text(json.dumps({"source_repo": "source", "log_path": "source/x.log", "allowed_paths": ["app/"]}), encoding="utf-8")
            base = {"AI_FIX_PUBLISH_PR": "false", "AI_FIX_DATABASE_URL": "", "AI_FIX_TELEGRAM_EVENTS": ""}
            base.update(env)
            with patch.dict(os.environ, base):
                return Config.load(path)

    def test_requires_valid_chat_id_when_enabled(self):
        self.assertFalse(self.load({"AI_FIX_TELEGRAM_ENABLED": "false"}).telegram_enabled)
        config = self.load({"AI_FIX_TELEGRAM_ENABLED": "true", "AI_FIX_TELEGRAM_CHAT_ID": "-1001234567890"})
        self.assertEqual(["reported", "started", "succeeded", "failed", "pr_merged", "pr_closed"], config.telegram_events)
        with self.assertRaises(ValueError):
            self.load({"AI_FIX_TELEGRAM_ENABLED": "true", "AI_FIX_TELEGRAM_CHAT_ID": "my group"})

    def test_parses_event_list(self):
        self.assertEqual(["failed", "repeated"], parse_events(" failed, REPEATED "))
        with self.assertRaises(ValueError):
            parse_events("failed,exploded")


if __name__ == "__main__":
    unittest.main()
