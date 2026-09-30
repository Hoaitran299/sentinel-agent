from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .console import stage
from .i18n import t


EVENTS = ("reported", "started", "succeeded", "failed", "pr_merged", "pr_closed", "repeated")
DEFAULT_EVENTS = ("reported", "started", "succeeded", "failed", "pr_merged", "pr_closed")
TOKEN_FORMAT = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
MAX_MESSAGE = 3500  # Telegram limit is 4096 characters; keep room for markup.

Transport = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    """Trusted notifier owned by the watcher. The bot token never reaches Claude, worktrees,
    argv, reports or the database, and is redacted from every error message."""

    def __init__(self, config: Any, transport: Optional[Transport] = None):
        self.config = config
        self._transport = transport
        self.muted = False  # set by runtime settings (fix_only mode)

    def enabled(self) -> bool:
        return bool(self.config.telegram_enabled)

    def preflight(self) -> str:
        bot = self._call("getMe", {})
        return "@{}".format(bot.get("username", "?"))

    def send_test(self) -> None:
        self._send([self._title("🧪", "tg_test"), self._line("tg_repository", self.config.github_repository or "-")])

    def notify(self, event: str, incident: Dict[str, Any], **details: Any) -> None:
        """Send one notification; failures are logged and never interrupt the pipeline."""
        if not self.enabled() or self.muted or event not in self.config.telegram_events:
            return
        try:
            self._send(self._build(event, incident, details))
        except Exception as error:
            stage("ERROR", "Telegram notification failed", notification=event, reason=self._redact(str(error)))

    # -- message building -------------------------------------------------------------

    def _build(self, event: str, incident: Dict[str, Any], details: Dict[str, Any]) -> List[str]:
        icon, key = {
            "reported": ("📣", "tg_reported"),
            "started": ("🚨", "tg_started"),
            "succeeded": ("✅", "tg_succeeded"),
            "failed": ("❌", "tg_failed"),
            "pr_merged": ("🎉", "tg_pr_merged"),
            "pr_closed": ("🚫", "tg_pr_closed"),
            "repeated": ("🔁", "tg_repeated"),
        }[event]
        lines = [
            self._title(icon, key),
            self._line("incident", str(incident.get("id", ""))[:8], code=True),
            self._line("tg_error", str(incident.get("message", ""))[:300]),
            self._line("occurrences", "{}/{}".format(incident.get("occurrence_count", "?"), incident.get("threshold", "?"))),
        ]
        for label, value, code in (
            ("tg_summary", details.get("summary"), False),
            ("tg_reason", details.get("reason"), False),
            ("branch", details.get("branch"), True),
            ("verified_commit", str(details.get("commit") or "")[:12], True),
            ("tg_report", details.get("report"), True),
        ):
            if value:
                lines.append(self._line(label, str(value)[:600], code=code))
        pull_request = details.get("pull_request_url") or incident.get("pull_request_url")
        if pull_request:
            lines.append('{}: <a href="{}">{}</a>'.format(
                html.escape(t(self._language(), "pull_request")),
                html.escape(str(pull_request), quote=True),
                html.escape(str(pull_request), quote=False),
            ))
        if event == "repeated":
            lines.append(html.escape(t(self._language(), "tg_repeated_hint")))
        if event == "reported":
            lines.append(html.escape(t(self._language(), "tg_reported_hint")))
        return lines

    def _language(self) -> str:
        return getattr(self.config, "report_language", "en")

    def _title(self, icon: str, key: str) -> str:
        return "{} <b>{}</b>".format(icon, html.escape(t(self._language(), key)))

    def _line(self, key: str, value: str, code: bool = False) -> str:
        escaped = html.escape(value, quote=False)
        return "{}: {}".format(html.escape(t(self._language(), key)), "<code>{}</code>".format(escaped) if code else escaped)

    # -- transport ----------------------------------------------------------------------

    def _send(self, lines: List[str]) -> None:
        text = "\n".join(lines)
        if len(text) > MAX_MESSAGE:
            text = text[:MAX_MESSAGE] + "…"
        self._call(
            "sendMessage",
            {
                "chat_id": self.config.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def _token(self) -> str:
        token = os.environ.get(self.config.telegram_token_env, "").strip()
        if not TOKEN_FORMAT.match(token):
            raise TelegramError("{} is missing or malformed".format(self.config.telegram_token_env))
        return token

    def _call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._transport is not None:
            return self._transport(method, payload)
        url = "{}/bot{}/{}".format(self.config.telegram_api_url, self._token(), method)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "demo-auto-fixbug-orchestrator/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("description", "")
            except (ValueError, UnicodeDecodeError):
                detail = ""
            # Never include the URL: it contains the bot token.
            raise TelegramError("Telegram API returned {}: {}".format(error.code, self._redact(detail)[:200])) from None
        except urllib.error.URLError as error:
            raise TelegramError("Telegram API unreachable: {}".format(self._redact(str(error.reason))[:200])) from None
        if not body.get("ok"):
            raise TelegramError("Telegram API error: {}".format(self._redact(str(body.get("description")))[:200]))
        return body.get("result") or {}

    def _redact(self, value: str) -> str:
        token = os.environ.get(self.config.telegram_token_env, "")
        return value.replace(token, "[REDACTED]") if token else value
