from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Pattern
import re
from urllib.parse import urlparse

from .database import DatabaseUrl
from .i18n import DEFAULT_LANGUAGE, normalize_language

CHAT_FORMAT = re.compile(r"^(?:-?\d{3,}|@[A-Za-z][A-Za-z0-9_]{4,})$")


@dataclass(frozen=True)
class Config:
    project_root: Path
    source_repo: Path
    log_path: Path
    base_branch: str
    state_db: Path
    database: DatabaseUrl
    ui_host: str
    ui_port: int
    report_language: str
    pr_sync_interval_seconds: float
    telegram_enabled: bool
    telegram_chat_id: str
    telegram_token_env: str
    telegram_api_url: str
    telegram_events: List[str]
    workspace_root: Path
    reports_dir: Path
    mode: str
    threshold: int
    window_seconds: int
    poll_interval_seconds: float
    start_at_end: bool
    include_patterns: List[Pattern[str]]
    claude_binary: str
    claude_timeout_seconds: int
    claude_max_budget_usd: float
    bootstrap_dependencies: bool
    allowed_paths: List[str]
    protected_paths: List[str]
    max_changed_files: int
    max_diff_lines: int
    require_new_test: bool
    publish_pull_request: bool
    github_repository: str
    github_api_url: str
    github_git_url: str
    github_token_env: str
    github_draft: bool
    verification_commands: List[List[str]]

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = path.expanduser().resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        project_root = path.parent.parent
        load_env_file(project_root / ".env")

        def resolve(value: str) -> Path:
            candidate = Path(value).expanduser()
            return (candidate if candidate.is_absolute() else project_root / candidate).resolve()

        state_db = resolve(data.get("state_db", "var/state.sqlite3"))
        database_url = os.environ.get(str(data.get("database_url_env", "AI_FIX_DATABASE_URL")), "").strip()
        database = DatabaseUrl.parse(database_url) if database_url else DatabaseUrl.sqlite(state_db)

        config = cls(
            project_root=project_root,
            source_repo=resolve(data["source_repo"]),
            log_path=resolve(data["log_path"]),
            base_branch=str(data.get("base_branch", "master")),
            state_db=state_db,
            database=database,
            ui_host=str(data.get("ui_host", "127.0.0.1")),
            ui_port=int(data.get("ui_port", 8787)),
            pr_sync_interval_seconds=float(data.get("pr_sync_interval_seconds", 60)),
            telegram_enabled=env_bool("AI_FIX_TELEGRAM_ENABLED", bool(data.get("telegram_enabled", False))),
            telegram_chat_id=os.environ.get("AI_FIX_TELEGRAM_CHAT_ID", str(data.get("telegram_chat_id", ""))).strip(),
            telegram_token_env=str(data.get("telegram_token_env", "AI_FIX_TELEGRAM_BOT_TOKEN")),
            telegram_api_url=str(data.get("telegram_api_url", "https://api.telegram.org")).rstrip("/"),
            telegram_events=parse_events(os.environ.get("AI_FIX_TELEGRAM_EVENTS") or data.get("telegram_events")),
            report_language=normalize_language(
                os.environ.get("AI_FIX_REPORT_LANGUAGE") or str(data.get("report_language", DEFAULT_LANGUAGE))
            ),
            workspace_root=resolve(data.get("workspace_root", "var/worktrees")),
            reports_dir=resolve(data.get("reports_dir", "var/reports")),
            mode=str(data.get("mode", "report_and_fix")),
            threshold=int(data.get("threshold", 3)),
            window_seconds=int(data.get("window_seconds", 600)),
            poll_interval_seconds=float(data.get("poll_interval_seconds", 1)),
            start_at_end=bool(data.get("start_at_end", True)),
            include_patterns=[re.compile(item, re.IGNORECASE) for item in data.get("include_patterns", [])],
            claude_binary=str(data.get("claude_binary", "claude")),
            claude_timeout_seconds=int(data.get("claude_timeout_seconds", 900)),
            claude_max_budget_usd=float(data.get("claude_max_budget_usd", 3)),
            bootstrap_dependencies=bool(data.get("bootstrap_dependencies", True)),
            allowed_paths=[str(item) for item in data.get("allowed_paths", [])],
            protected_paths=[str(item) for item in data.get("protected_paths", [])],
            max_changed_files=int(data.get("max_changed_files", 8)),
            max_diff_lines=int(data.get("max_diff_lines", 500)),
            require_new_test=bool(data.get("require_new_test", True)),
            publish_pull_request=env_bool("AI_FIX_PUBLISH_PR", bool(data.get("publish_pull_request", False))),
            github_repository=os.environ.get("AI_FIX_GITHUB_REPOSITORY", str(data.get("github_repository", ""))),
            github_api_url=os.environ.get("AI_FIX_GITHUB_API_URL", str(data.get("github_api_url", "https://api.github.com"))).rstrip("/"),
            github_git_url=os.environ.get("AI_FIX_GITHUB_GIT_URL", str(data.get("github_git_url", ""))),
            github_token_env=str(data.get("github_token_env", "AI_FIX_GITHUB_TOKEN")),
            github_draft=env_bool("AI_FIX_GITHUB_DRAFT", bool(data.get("github_draft", True))),
            verification_commands=[[str(part) for part in command] for command in data.get("verification_commands", [])],
        )
        config.validate()
        return config

    def validate(self) -> None:
        from .storage import MODES

        if self.mode not in MODES:
            raise ValueError("mode must be one of: {}".format(", ".join(MODES)))
        if self.threshold < 1:
            raise ValueError("threshold must be at least 1")
        if self.window_seconds < 1:
            raise ValueError("window_seconds must be at least 1")
        if not self.source_repo.is_dir():
            raise ValueError("source_repo does not exist: {}".format(self.source_repo))
        if self.workspace_root == self.source_repo or self.source_repo in self.workspace_root.parents:
            raise ValueError("workspace_root must not be inside source_repo")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", self.base_branch):
            raise ValueError("base_branch contains unsafe characters")
        if not self.allowed_paths:
            raise ValueError("allowed_paths must not be empty")
        if self.publish_pull_request:
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repository) is None:
                raise ValueError("github_repository must use owner/repository format")
            for name, value in (("github_api_url", self.github_api_url), ("github_git_url", self.github_git_url)):
                parsed = urlparse(value)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("{} must be a credential-free HTTPS URL".format(name))
        if self.ui_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("ui_host must be a loopback address; the dashboard has no login")
        if self.telegram_enabled:
            if CHAT_FORMAT.match(self.telegram_chat_id) is None:
                raise ValueError("AI_FIX_TELEGRAM_CHAT_ID must be a numeric chat id (e.g. -1001234567890) or @channelname")
            parsed = urlparse(self.telegram_api_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("telegram_api_url must be a credential-free HTTPS URL")
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", self.telegram_token_env) is None:
            raise ValueError("telegram_token_env must name an environment variable")
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", self.github_token_env) is None:
            raise ValueError("github_token_env must name an environment variable")


def parse_events(value: object) -> List[str]:
    from .notifier import DEFAULT_EVENTS, EVENTS

    if value is None or value == "":
        return list(DEFAULT_EVENTS)
    items = value if isinstance(value, list) else str(value).split(",")
    events = [str(item).strip().lower() for item in items if str(item).strip()]
    unknown = sorted(set(events) - set(EVENTS))
    if unknown:
        raise ValueError("unknown Telegram events {}; allowed: {}".format(unknown, ", ".join(EVENTS)))
    return events


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("{} must be a boolean".format(name))


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid .env line {}".format(number))
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", key) is None:
            raise ValueError("invalid .env key on line {}".format(number))
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
