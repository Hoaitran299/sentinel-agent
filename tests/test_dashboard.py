import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.config import Config
from autofix_agent.console import scope, set_sink, stage
from autofix_agent.dashboard import build_server
from autofix_agent.database import DatabaseUrl
from autofix_agent.log_reader import LogEvent
from autofix_agent.storage import StateStore


def event() -> LogEvent:
    return LogEvent("2026-09-27 22:00:00", "local", "ERROR", "phone cannot be null", "phone cannot be null", None, "a" * 64)


def make_config(root: Path) -> Config:
    return Config(
        project_root=root, source_repo=root, log_path=root / "laravel.log", base_branch="master", mode="report_and_fix",
        state_db=root / "state.sqlite3", database=DatabaseUrl.sqlite(root / "state.sqlite3"),
        ui_host="127.0.0.1", ui_port=0, report_language="en", pr_sync_interval_seconds=60,
        telegram_enabled=False, telegram_chat_id="", telegram_token_env="AI_FIX_TELEGRAM_BOT_TOKEN",
        telegram_api_url="https://api.telegram.org", telegram_events=["started", "succeeded", "failed", "pr_merged", "pr_closed"], workspace_root=root / "worktrees", reports_dir=root / "reports",
        threshold=3, window_seconds=600, poll_interval_seconds=1, start_at_end=True, include_patterns=[],
        claude_binary="claude", claude_timeout_seconds=1, claude_max_budget_usd=1, bootstrap_dependencies=False,
        allowed_paths=["app/"], protected_paths=[], max_changed_files=1, max_diff_lines=1, require_new_test=True,
        publish_pull_request=False, github_repository="", github_api_url="", github_git_url="",
        github_token_env="AI_FIX_GITHUB_TOKEN", github_draft=True, verification_commands=[],
    )


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = make_config(root)
        self.store = StateStore(self.config.database)
        self.server = build_server(self.config)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        merged = {"Host": host or "127.0.0.1:{}".format(self.port)}
        merged.update(headers or {})
        connection.request(method, path, body="{}" if method == "POST" else None, headers=merged)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, response, body

    def failed_incident(self) -> str:
        decision = self.store.record_event(event(), "b" * 40, 1, 600)
        self.store.fail_incident(decision.incident_id, "verification failed")
        return decision.incident_id

    def test_serves_page_with_strict_csp_and_overview(self):
        status, response, body = self.request("GET", "/")
        self.assertEqual(200, status)
        self.assertIn(b"AI Fix Console", body)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))

        incident_id = self.failed_incident()
        status, _, body = self.request("GET", "/api/overview")
        overview = json.loads(body)
        self.assertEqual(200, status)
        self.assertEqual("sqlite", overview["database"])
        self.assertEqual({"failed": 1}, overview["counts"])
        self.assertEqual(incident_id, overview["incidents"][0]["id"])

    def test_rejects_foreign_host_header(self):
        status, _, _ = self.request("GET", "/api/overview", host="attacker.example:{}".format(self.port))
        self.assertEqual(403, status)

    def test_retry_requires_custom_header_and_same_origin(self):
        incident_id = self.failed_incident()
        path = "/api/incidents/{}/retry".format(incident_id)

        status, _, _ = self.request("POST", path, {"Content-Type": "application/json"})
        self.assertEqual(403, status)
        status, _, _ = self.request("POST", path, {"X-AI-Fix-Request": "1", "Origin": "https://attacker.example"})
        self.assertEqual(403, status)
        self.assertEqual("failed", self.store.incident(incident_id)["status"])

        status, _, body = self.request("POST", path, {"X-AI-Fix-Request": "1", "Origin": "http://127.0.0.1:{}".format(self.port)})
        self.assertEqual(202, status)
        self.assertTrue(json.loads(body)["queued"])
        self.assertEqual("queued", self.store.incident(incident_id)["status"])

        status, _, _ = self.request("POST", path, {"X-AI-Fix-Request": "1"})
        self.assertEqual(409, status)

    def test_release_unlocks_pending_fix_with_csrf_header(self):
        decision = self.store.record_event(event(), "b" * 40, 1, 600)
        run_id = self.store.claim(decision.incident_id)
        self.store.complete_incident(decision.incident_id, "ai-fix/x", "c" * 40, "r.md", "https://github.com/o/r/pull/7")
        path = "/api/incidents/{}/release".format(decision.incident_id)

        status, _, _ = self.request("POST", path)
        self.assertEqual(403, status)
        status, _, _ = self.request("POST", path, {"X-AI-Fix-Request": "1"})
        self.assertEqual(200, status)
        self.assertEqual("closed", self.store.incident(decision.incident_id)["status"])
        status, _, _ = self.request("POST", path, {"X-AI-Fix-Request": "1"})
        self.assertEqual(409, status)
        self.assertIsNotNone(run_id)

    def post_json(self, path, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        merged = {"Host": "127.0.0.1:{}".format(self.port), "Content-Type": "application/json"}
        merged.update(headers or {})
        connection.request("POST", path, body=json.dumps(payload), headers=merged)
        response = connection.getresponse()
        body = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, body

    def test_clear_requires_header_and_typed_confirmation_then_backs_up(self):
        incident_id = self.failed_incident()
        self.config.reports_dir.mkdir()
        run_report = self.config.reports_dir / "{}.md".format("c" * 32)
        run_report.write_text("# report", encoding="utf-8")
        foreign = self.config.reports_dir / "notes.md"
        foreign.write_text("keep me", encoding="utf-8")

        status, _ = self.post_json("/api/clear", {"confirm": "CLEAR"})
        self.assertEqual(403, status)
        status, _ = self.post_json("/api/clear", {"confirm": "clear"}, {"X-AI-Fix-Request": "1"})
        self.assertEqual(400, status)
        self.assertEqual("failed", self.store.incident(incident_id)["status"])

        status, body = self.post_json("/api/clear", {"confirm": "CLEAR", "delete_reports": True}, {"X-AI-Fix-Request": "1"})
        self.assertEqual(200, status)
        self.assertEqual(1, body["deleted"]["incidents"])
        self.assertEqual(1, body["reports_deleted"])
        self.assertEqual([], self.store.recent())
        self.assertFalse(run_report.exists())
        self.assertTrue(foreign.exists())
        backup = json.loads(Path(body["backup"]).read_text(encoding="utf-8"))
        self.assertEqual(incident_id, backup["incidents"][0]["id"])
        self.assertEqual(0o600, Path(body["backup"]).stat().st_mode & 0o777)

    def test_clear_conflict_while_running_keeps_data_and_no_backup(self):
        decision = self.store.record_event(event(), "b" * 40, 1, 600)
        self.store.claim(decision.incident_id)
        status, _ = self.post_json("/api/clear", {"confirm": "CLEAR"}, {"X-AI-Fix-Request": "1"})
        self.assertEqual(409, status)
        self.assertEqual("running", self.store.incident(decision.incident_id)["status"])
        backups = self.config.reports_dir.parent / "backups"
        self.assertEqual([], list(backups.glob("*.json")) if backups.exists() else [])

    def test_settings_round_trip_with_validation_and_csrf(self):
        status, _, body = self.request("GET", "/api/settings")
        self.assertEqual(200, status)
        self.assertEqual({"mode": "report_and_fix", "threshold": 3, "window_seconds": 600}, json.loads(body)["values"])

        status, _ = self.post_json("/api/settings", {"mode": "report_only"})
        self.assertEqual(403, status)
        status, body = self.post_json("/api/settings", {"threshold": 0}, {"X-AI-Fix-Request": "1"})
        self.assertEqual(400, status)
        status, body = self.post_json("/api/settings", {"mode": "report_only", "threshold": 5, "window_seconds": 300}, {"X-AI-Fix-Request": "1"})
        self.assertEqual(200, status)
        self.assertEqual({"mode": "report_only", "threshold": 5, "window_seconds": 300}, body["values"])

        overview = json.loads(self.request("GET", "/api/overview")[2])
        self.assertEqual("report_only", overview["runtime"]["mode"])
        self.assertIn("Settings updated from dashboard", [e["message"] for e in overview["events"]])

    def test_reported_incident_can_be_started_from_dashboard(self):
        decision = self.store.record_event(event(), "b" * 40, 1, 600, mode="report_only")
        self.assertEqual("reported", self.store.incident(decision.incident_id)["status"])
        status, _ = self.post_json("/api/incidents/{}/retry".format(decision.incident_id), {}, {"X-AI-Fix-Request": "1"})
        self.assertEqual(202, status)
        self.assertEqual("queued", self.store.incident(decision.incident_id)["status"])

    def test_report_is_read_only_from_reports_dir(self):
        self.config.reports_dir.mkdir()
        run_id = "c" * 32
        (self.config.reports_dir / "{}-failure.md".format(run_id)).write_text("# failure", encoding="utf-8")

        status, _, body = self.request("GET", "/api/runs/{}/report".format(run_id))
        self.assertEqual(200, status)
        self.assertEqual("# failure", json.loads(body)["markdown"])
        status, _, _ = self.request("GET", "/api/runs/..%2F..%2Fetc%2Fpasswd/report")
        self.assertEqual(404, status)

    def test_incident_detail_includes_timeline(self):
        incident_id = self.failed_incident()
        self.store.add_event("ERROR", "Agent run stopped safely", {"reason": "x"}, incident_id=incident_id)
        status, _, body = self.request("GET", "/api/incidents/{}".format(incident_id))
        detail = json.loads(body)
        self.assertEqual(200, status)
        self.assertEqual("Agent run stopped safely", detail["events"][0]["message"])
        self.assertEqual(1, len(detail["occurrences"]))

    def test_non_loopback_ui_host_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.config, ui_host="0.0.0.0").validate()

    def test_bind_host_override_keeps_loopback_host_check(self):
        with mock.patch.dict(os.environ, {"AI_FIX_UI_BIND_HOST": "0.0.0.0"}):
            server = build_server(replace(self.config, ui_port=0))
        try:
            self.assertEqual("0.0.0.0", server.server_address[0])
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/overview", headers={"Host": "evil.example:{}".format(port)})
            self.assertEqual(403, connection.getresponse().status)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()


class ConsoleSinkTest(unittest.TestCase):
    def test_stage_mirrors_scoped_events_and_survives_sink_errors(self):
        captured = []
        set_sink(lambda *args: captured.append(args))
        try:
            with redirect_stdout(io.StringIO()):
                with scope(incident_id="i" * 32):
                    with scope(run_id="r" * 32):
                        stage("VERIFY", "Running", command="php artisan test")
                stage("OBSERVE", "Watcher stopped")
                set_sink(lambda *args: 1 / 0)
                stage("OBSERVE", "still printed")
        finally:
            set_sink(None)

        self.assertEqual(("VERIFY", "Running", {"command": "php artisan test"}, "i" * 32, "r" * 32), captured[0])
        self.assertEqual((None, None), captured[1][3:])


if __name__ == "__main__":
    unittest.main()
