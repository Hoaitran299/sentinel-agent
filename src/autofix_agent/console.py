from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, Optional


COLORS = {
    "OBSERVE": "\033[36m",
    "TRIGGER": "\033[33m",
    "ANALYZE": "\033[35m",
    "GUARD": "\033[34m",
    "FIX": "\033[35m",
    "VERIFY": "\033[36m",
    "GIT": "\033[32m",
    "DONE": "\033[32m",
    "ERROR": "\033[31m",
}
RESET = "\033[0m"

EventSink = Callable[[str, str, Dict[str, Any], Optional[str], Optional[str]], None]

_sink: Optional[EventSink] = None
_scope: Dict[str, Optional[str]] = {"incident_id": None, "run_id": None}


def set_sink(sink: Optional[EventSink]) -> None:
    """Mirror every stage line into persistent storage (used by the dashboard timeline)."""
    global _sink
    _sink = sink


@contextmanager
def scope(incident_id: Optional[str] = None, run_id: Optional[str] = None) -> Iterator[None]:
    previous = dict(_scope)
    _scope.update(incident_id=incident_id or previous["incident_id"], run_id=run_id or previous["run_id"])
    try:
        yield
    finally:
        _scope.update(previous)


def stage(name: str, message: str, **context: Any) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    color = COLORS.get(name, "")
    suffix = " ".join("{}={}".format(key, value) for key, value in context.items())
    line = "{} | {}".format(message, suffix) if suffix else message
    print("{}[{}] [{:<7}] {}{}".format(color, timestamp, name, line, RESET), flush=True)
    if _sink is not None:
        try:
            _sink(name, message, context, _scope["incident_id"], _scope["run_id"])
        except Exception as error:  # the timeline must never break an agent run
            print("[{}] [WARN   ] Could not persist stage event | {}".format(timestamp, error.__class__.__name__), flush=True)
