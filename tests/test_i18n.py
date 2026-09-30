import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.claude import ClaudeCodeRunner
from autofix_agent.config import Config
from autofix_agent.i18n import MESSAGES, normalize_language, t
from autofix_agent.pipeline import AgentPipeline
from test_dashboard import make_config
from test_github import FakeExecutor, StubPublisher, config as github_config


class LanguageTest(unittest.TestCase):
    def test_normalizes_common_spellings_and_rejects_unknown(self):
        self.assertEqual("vi", normalize_language("VI"))
        self.assertEqual("ja", normalize_language("ja_JP"))
        self.assertEqual("en", normalize_language(" en-US "))
        with self.assertRaises(ValueError):
            normalize_language("fr")

    def test_every_language_defines_every_message(self):
        for language, messages in MESSAGES.items():
            self.assertEqual(set(MESSAGES["en"]), set(messages), language)

    def test_config_reads_language_from_env_with_json_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "source").mkdir()
            path = root / "config" / "demo.json"
            path.write_text(
                json.dumps({"source_repo": "source", "log_path": "source/laravel.log", "allowed_paths": ["app/"], "report_language": "ja"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AI_FIX_PUBLISH_PR": "false", "AI_FIX_DATABASE_URL": ""}, clear=False):
                os.environ.pop("AI_FIX_REPORT_LANGUAGE", None)
                self.assertEqual("ja", Config.load(path).report_language)
                with patch.dict(os.environ, {"AI_FIX_REPORT_LANGUAGE": "vi"}):
                    self.assertEqual("vi", Config.load(path).report_language)
                with patch.dict(os.environ, {"AI_FIX_REPORT_LANGUAGE": "de"}):
                    with self.assertRaises(ValueError):
                        Config.load(path)


class LocalizedOutputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))

    def test_failure_report_uses_configured_language(self):
        pipeline = AgentPipeline.__new__(AgentPipeline)
        pipeline.config = replace(self.config, report_language="vi")
        incident = {"id": "a" * 32, "source_commit": "b" * 40}

        report = pipeline._write_failure_report(incident, "c" * 32, None, RuntimeError("boom"))

        text = report.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Báo cáo AI fix thất bại"))
        self.assertIn("- Lần chạy: `{}`".format("c" * 32), text)

    def test_pull_request_body_uses_configured_language(self):
        settings = github_config()
        settings.report_language = "ja"
        publisher = StubPublisher(settings, FakeExecutor())
        with patch.dict(os.environ, {"AI_FIX_GITHUB_TOKEN": "github_pat_test-token"}):
            publisher.publish(
                workspace=Path(self.temp.name),
                branch="ai-fix/incident-1234",
                commit_sha="b" * 40,
                incident_id="1234abcd" * 4,
                analysis={"summary": "電話番号の検証が不足しています"},
            )
        payload = [p for method, path, p in publisher.requests if method == "POST"][0]
        self.assertEqual(t("ja", "pr_title", incident="1234abcd"), payload["title"])
        self.assertIn("## 自動修正レポート", payload["body"])

    def test_claude_prompts_request_the_report_language(self):
        runner = ClaudeCodeRunner(replace(self.config, report_language="ja"), FakeExecutor())
        incident = {"id": "a" * 32, "fingerprint": "f" * 64, "source_commit": "b" * 40, "message": "m",
                    "log_excerpt": "x", "top_application_frame": None, "occurrence_count": 3}
        with patch.object(runner, "_run", return_value={}) as run:
            runner.analyze(Path(self.temp.name), incident)
            runner.fix(Path(self.temp.name), incident, {"summary": "s"}, ["app/"])
        for call in run.call_args_list:
            self.assertIn("Japanese (日本語)", call.kwargs["prompt"])
            # the instruction lives in the trusted part, before untrusted incident data
            prompt = call.kwargs["prompt"]
            self.assertLess(prompt.index("Japanese"), prompt.index("INCIDENT_DATA"))


if __name__ == "__main__":
    unittest.main()
