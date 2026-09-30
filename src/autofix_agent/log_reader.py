from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from typing import Iterable, Optional


HEADER = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+(?P<environment>[^.\s]+)\.(?P<level>[A-Z]+):\s+(?P<body>.*)$"
)
APP_FRAME = re.compile(r"(?P<path>app/[A-Za-z0-9_./-]+\.php)\((?P<line>\d+)\)")
SECRET = re.compile(
    r"(?i)(password|passwd|token|secret|authorization|cookie|api[_-]?key)([\"']?\s*[:=]\s*[\"']?)([^\s,}\"]+)"
)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()-]{7,}\d)(?!\w)")
HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s]+")
SQL_VALUE = re.compile(r"(?i)(bindings?|values?)\s*[:=]\s*\[[^\]]*\]")


@dataclass(frozen=True)
class LogEvent:
    timestamp: str
    environment: str
    level: str
    message: str
    sanitized_excerpt: str
    top_application_frame: Optional[str]
    fingerprint: str


def sanitize(value: str, limit: int = 6000) -> str:
    value = SECRET.sub(lambda match: "{}{}[REDACTED]".format(match.group(1), match.group(2)), value)
    value = EMAIL.sub("[REDACTED_EMAIL]", value)
    value = PHONE.sub("[REDACTED_PHONE]", value)
    value = HOME_PATH.sub("/home/[REDACTED_USER]", value)
    value = SQL_VALUE.sub(lambda match: "{}=[REDACTED]".format(match.group(1)), value)
    return value[:limit]


def normalize_message(value: str) -> str:
    value = value.split(' {"', 1)[0]
    value = re.sub(r"(?i)SQLSTATE\[[A-Z0-9]+\]", "SQLSTATE", value)
    value = re.sub(r"\b\d+\b", "?", value)
    value = re.sub(r"['\"][^'\"]{16,}['\"]", "?", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value[:1000]


def parse_entry(lines: Iterable[str]) -> Optional[LogEvent]:
    rows = list(lines)
    if not rows:
        return None
    header = HEADER.match(rows[0].rstrip("\n"))
    if header is None:
        return None
    level = header.group("level")
    if level not in {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}:
        return None
    raw = "".join(rows)
    frame_match = APP_FRAME.search(raw)
    frame = None
    if frame_match:
        application_path = frame_match.group("path")
        while application_path.startswith("app/app/"):
            application_path = application_path[4:]
        frame = "{}:{}".format(application_path, frame_match.group("line"))
    normalized = normalize_message(sanitize(header.group("body")))
    digest = hashlib.sha256("{}|{}".format(normalized, frame or "no-app-frame").encode("utf-8")).hexdigest()
    return LogEvent(
        timestamp=header.group("timestamp"),
        environment=header.group("environment"),
        level=level,
        message=normalized,
        sanitized_excerpt=sanitize(raw),
        top_application_frame=frame,
        fingerprint=digest,
    )


def matches_any(event: LogEvent, patterns: Iterable[re.Pattern[str]]) -> bool:
    compiled = list(patterns)
    return not compiled or any(pattern.search(event.sanitized_excerpt) for pattern in compiled)
