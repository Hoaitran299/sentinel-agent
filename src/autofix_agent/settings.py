from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

from .storage import MODES, StateStore


THRESHOLD_RANGE = (1, 1000)
WINDOW_RANGE = (10, 7 * 24 * 3600)


@dataclass(frozen=True)
class RuntimeSettings:
    """Operator-controlled behaviour, stored in the `settings` table and re-read by the
    watcher on every poll, so dashboard changes apply without a restart.

    mode:
      report_and_fix  notify (Telegram) and run the AI fix automatically (default)
      fix_only        run the AI fix automatically without Telegram notifications
      report_only     notify only; the incident waits as `reported` until a human starts the fix
    """

    mode: str
    threshold: int
    window_seconds: int

    @property
    def runs_agent(self) -> bool:
        return self.mode != "report_only"

    @property
    def sends_notifications(self) -> bool:
        return self.mode != "fix_only"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def defaults(config: Any) -> RuntimeSettings:
    return RuntimeSettings(
        mode=getattr(config, "mode", "report_and_fix"),
        threshold=int(config.threshold),
        window_seconds=int(config.window_seconds),
    )


def validate(values: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a partial update coming from the dashboard/CLI."""
    clean: Dict[str, Any] = {}
    unknown = set(values) - {"mode", "threshold", "window_seconds"}
    if unknown:
        raise ValueError("unknown settings: {}".format(", ".join(sorted(unknown))))
    if "mode" in values:
        mode = str(values["mode"]).strip()
        if mode not in MODES:
            raise ValueError("mode must be one of: {}".format(", ".join(MODES)))
        clean["mode"] = mode
    for name, (low, high) in (("threshold", THRESHOLD_RANGE), ("window_seconds", WINDOW_RANGE)):
        if name in values:
            try:
                number = int(values[name])
            except (TypeError, ValueError):
                raise ValueError("{} must be an integer".format(name)) from None
            if isinstance(values[name], bool) or not low <= number <= high:
                raise ValueError("{} must be between {} and {}".format(name, low, high))
            clean[name] = number
    return clean


def load(store: StateStore, config: Any) -> RuntimeSettings:
    base = defaults(config).as_dict()
    stored = store.settings()
    for key in base:
        if key not in stored:
            continue
        try:
            base.update(validate({key: stored[key]}))
        except ValueError:
            # A hand-edited invalid row must not stop the watcher; keep the config default.
            pass
    return RuntimeSettings(**base)


def save(store: StateStore, values: Dict[str, Any]) -> Dict[str, Any]:
    clean = validate(values)
    if clean:
        store.save_settings(clean)
    return clean
