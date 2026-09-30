from __future__ import annotations

import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .config import Config
from .console import stage
from . import settings as runtime_settings
from .storage import StateStore


STATIC_ROOT = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
HEX_ID = re.compile(r"^[0-9a-f]{32}$")
REPORT_FILE = re.compile(r"^[0-9a-f]{32}(?:-failure)?\.md$")
CLEAR_CONFIRMATION = "CLEAR"
INCIDENT_ROUTE = re.compile(r"^/api/incidents/([0-9a-f]{32})(?:/(retry|release))?$")
REPORT_ROUTE = re.compile(r"^/api/runs/([0-9a-f]{32})/report$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class DashboardApi:
    """Read-mostly view over the state store. The only write is re-queueing a failed incident;
    the watcher process (which owns Claude/GitHub capabilities) executes it."""

    def __init__(self, config: Config):
        self.config = config
        try:
            from .log_sources import build_source

            self.log_source = build_source(config).label
        except Exception as error:
            self.log_source = "invalid log source ({})".format(error.__class__.__name__)

    def _store(self) -> StateStore:
        return StateStore(self.config.database)

    def overview(self) -> Dict[str, Any]:
        store = self._store()
        try:
            heartbeat = store.watcher_heartbeat()
            runtime = runtime_settings.load(store, self.config)
            return {
                "runtime": runtime.as_dict(),
                "now": time.time(),
                "database": store.dialect,
                "watcher": self._watcher_state(heartbeat),
                "settings": {
                    "threshold": self.config.threshold,
                    "window_seconds": self.config.window_seconds,
                    "base_branch": self.config.base_branch,
                    "log_source": self.log_source,
                    "publish_pull_request": self.config.publish_pull_request,
                    "github_repository": self.config.github_repository,
                    "telegram_enabled": self.config.telegram_enabled,
                },
                "counts": store.status_counts(),
                "incidents": store.recent(limit=100),
                "events": store.events(limit=60),
            }
        finally:
            store.close()

    def incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        store = self._store()
        try:
            try:
                incident = store.incident(incident_id)
            except KeyError:
                return None
            return {
                "incident": incident,
                "occurrences": store.occurrences(incident_id),
                "runs": [self._with_reports(run) for run in store.runs(incident_id)],
                "events": store.events(incident_id=incident_id, limit=500),
            }
        finally:
            store.close()

    def retry(self, incident_id: str) -> Tuple[HTTPStatus, Dict[str, Any]]:
        store = self._store()
        try:
            if not store.requeue_failed(incident_id):
                return HTTPStatus.CONFLICT, {"error": "Only failed incidents can be retried."}
            store.add_event("TRIGGER", "Retry requested from dashboard", {}, incident_id=incident_id)
            watcher = self._watcher_state(store.watcher_heartbeat())
            return HTTPStatus.ACCEPTED, {"queued": True, "watcher": watcher}
        finally:
            store.close()

    def get_settings(self) -> Dict[str, Any]:
        store = self._store()
        try:
            return {
                "values": runtime_settings.load(store, self.config).as_dict(),
                "defaults": runtime_settings.defaults(self.config).as_dict(),
                "limits": {"threshold": runtime_settings.THRESHOLD_RANGE, "window_seconds": runtime_settings.WINDOW_RANGE},
            }
        finally:
            store.close()

    def update_settings(self, values: Dict[str, Any]) -> Tuple[HTTPStatus, Dict[str, Any]]:
        store = self._store()
        try:
            before = runtime_settings.load(store, self.config).as_dict()
            try:
                changed = runtime_settings.save(store, values)
            except ValueError as error:
                return HTTPStatus.BAD_REQUEST, {"error": str(error)}
            after = runtime_settings.load(store, self.config).as_dict()
            diff = {key: "{} -> {}".format(before[key], after[key]) for key in changed if before[key] != after[key]}
            if diff:
                store.add_event("OBSERVE", "Settings updated from dashboard", diff)
            return HTTPStatus.OK, {"values": after}
        finally:
            store.close()

    def release(self, incident_id: str) -> Tuple[HTTPStatus, Dict[str, Any]]:
        store = self._store()
        try:
            if not store.release(incident_id):
                return HTTPStatus.CONFLICT, {"error": "Only incidents with a pending or closed fix can be released."}
            store.add_event("TRIGGER", "Released from dashboard; the next occurrences may start a new auto-fix", {}, incident_id=incident_id)
            return HTTPStatus.OK, {"released": True}
        finally:
            store.close()

    def clear(self, delete_reports: bool) -> Tuple[HTTPStatus, Dict[str, Any]]:
        store = self._store()
        try:
            backup = self._backup(store.snapshot())
            try:
                deleted = store.clear()
            except RuntimeError as error:
                backup.unlink()
                return HTTPStatus.CONFLICT, {"error": str(error)}
            removed_reports = self._delete_reports() if delete_reports else 0
            store.add_event(
                "DONE",
                "State cleared from dashboard",
                {"incidents": deleted["incidents"], "reports_deleted": removed_reports, "backup": backup.name},
            )
            return HTTPStatus.OK, {"deleted": deleted, "reports_deleted": removed_reports, "backup": str(backup)}
        finally:
            store.close()

    def _backup(self, snapshot: Dict[str, Any]) -> Path:
        directory = self.config.reports_dir.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        path = directory / "state-{}-{:03d}.json".format(time.strftime("%Y%m%d-%H%M%S", time.localtime(now)), int(now * 1000) % 1000)
        path.write_text(json.dumps(snapshot, default=str, ensure_ascii=False, indent=1), encoding="utf-8")
        path.chmod(0o600)
        return path

    def _delete_reports(self) -> int:
        # Only files the orchestrator itself writes (<run-id>.md / <run-id>-failure.md).
        if not self.config.reports_dir.is_dir():
            return 0
        removed = 0
        for path in self.config.reports_dir.iterdir():
            if path.is_file() and REPORT_FILE.match(path.name):
                path.unlink()
                removed += 1
        return removed

    def report(self, run_id: str) -> Optional[str]:
        reports = self.config.reports_dir.resolve()
        for name in ("{}.md".format(run_id), "{}-failure.md".format(run_id)):
            candidate = (reports / name).resolve()
            if candidate.parent == reports and candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        return None

    def _with_reports(self, run: Dict[str, Any]) -> Dict[str, Any]:
        reports = self.config.reports_dir
        run["has_report"] = any(
            (reports / name).is_file() for name in ("{}.md".format(run["id"]), "{}-failure.md".format(run["id"]))
        )
        return run

    def _watcher_state(self, heartbeat: Optional[float]) -> Dict[str, Any]:
        # The watcher saves its log cursor on every poll, so the cursor timestamp is a heartbeat.
        # A long agent run blocks polling, which is reported as "busy" rather than "offline".
        age = None if heartbeat is None else max(0.0, time.time() - heartbeat)
        tolerance = max(5.0, self.config.poll_interval_seconds * 5)
        return {"last_seen": heartbeat, "age_seconds": age, "online": age is not None and age <= tolerance}


class DashboardHandler(BaseHTTPRequestHandler):
    api: DashboardApi
    port: int
    server_version = "AiFixDashboard/1.0"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
        return

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        name, _, port = host.rpartition(":") if not host.endswith("]") else (host, "", "")
        return name in LOOPBACK_HOSTS and port == str(self.port)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self._host_allowed():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Host not allowed"})
        path = urlparse(self.path).path
        try:
            if path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                return self._send(HTTPStatus.OK, (STATIC_ROOT / name).read_bytes(), content_type)
            if path == "/api/overview":
                return self._json(HTTPStatus.OK, self.api.overview())
            if path == "/api/settings":
                return self._json(HTTPStatus.OK, self.api.get_settings())
            match = INCIDENT_ROUTE.match(path)
            if match and not match.group(2):
                detail = self.api.incident(match.group(1))
                if detail is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "Incident not found"})
                return self._json(HTTPStatus.OK, detail)
            match = REPORT_ROUTE.match(path)
            if match:
                report = self.api.report(match.group(1))
                if report is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "Report not found"})
                return self._json(HTTPStatus.OK, {"markdown": report})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except Exception as error:
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": error.__class__.__name__})

    def do_POST(self) -> None:
        if not self._host_allowed():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Host not allowed"})
        # A custom header cannot be sent cross-site without a CORS preflight, which this
        # server never approves, so browsers cannot forge the retry request (CSRF).
        if self.headers.get("X-AI-Fix-Request") != "1":
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Missing request header"})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected"})
        path = urlparse(self.path).path
        if path in {"/api/clear", "/api/settings"}:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 4096)) or b"{}") if length else {}
            except (ValueError, json.JSONDecodeError):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
            if not isinstance(body, dict):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
        if path == "/api/settings":
            try:
                status, payload = self.api.update_settings(body)
                return self._json(status, payload)
            except Exception as error:
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": error.__class__.__name__})
        if path == "/api/clear":
            if body.get("confirm") != CLEAR_CONFIRMATION:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "Type CLEAR to confirm"})
            try:
                status, payload = self.api.clear(bool(body.get("delete_reports")))
                return self._json(status, payload)
            except Exception as error:
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": error.__class__.__name__})
        match = INCIDENT_ROUTE.match(path)
        if not match or not match.group(2):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        try:
            action = self.api.retry if match.group(2) == "retry" else self.api.release
            status, payload = action(match.group(1))
            return self._json(status, payload)
        except Exception as error:
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": error.__class__.__name__})


def build_server(config: Config, port: Optional[int] = None) -> ThreadingHTTPServer:
    selected = config.ui_port if port is None else port
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"api": DashboardApi(config), "port": selected})
    # Inside a container the server must bind 0.0.0.0 so a published port can reach it;
    # the Host header check still only accepts loopback names.
    bind_host = os.environ.get("AI_FIX_UI_BIND_HOST", "").strip() or config.ui_host
    server = ThreadingHTTPServer((bind_host, selected), handler)
    handler.port = server.server_address[1]  # port 0 binds a random port in tests
    server.daemon_threads = True
    return server


def serve(config: Config, port: Optional[int] = None) -> int:
    StateStore(config.database).close()  # fail fast and run migrations before serving
    server = build_server(config, port)
    bound_port = server.server_address[1]
    stage("DONE", "Dashboard ready", url="http://localhost:{}/".format(bound_port), database=config.database.describe())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
