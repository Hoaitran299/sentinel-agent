from __future__ import annotations

import os
import base64
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class CommandResult:
    command: List[str]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, message: str, result: Optional[CommandResult] = None):
        super().__init__(message)
        self.result = result


def safe_environment(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    allowed = ["HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER"]
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "APP_ENV": "testing",
            "APP_KEY": "base64:" + base64.b64encode(bytes(32)).decode("ascii"),
            "APP_DEBUG": "false",
            "DB_CONNECTION": "sqlite",
            "DB_DATABASE": ":memory:",
            "CACHE_STORE": "array",
            "SESSION_DRIVER": "array",
            "QUEUE_CONNECTION": "sync",
            "MAIL_MAILER": "array",
            "INCIDENT_RECORDING_ENABLED": "false",
        }
    )
    if extra:
        environment.update(extra)
    return environment


class ProcessExecutor:
    def __init__(self, max_output_bytes: int = 65536):
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        command: Iterable[str],
        cwd: Path,
        timeout: int,
        stdin: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        check: bool = True,
    ) -> CommandResult:
        arguments = [str(item) for item in command]
        try:
            completed = subprocess.run(
                arguments,
                cwd=str(cwd),
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=environment or safe_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandError("command timed out after {}s: {}".format(timeout, arguments[0])) from error
        except FileNotFoundError as error:
            raise CommandError("command not found: {}".format(arguments[0])) from error

        result = CommandResult(
            command=arguments,
            returncode=completed.returncode,
            stdout=self._limit(completed.stdout),
            stderr=self._limit(completed.stderr),
        )
        if check and result.returncode != 0:
            combined = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
            combined = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", combined)
            lines = [line for line in combined.splitlines() if line.strip()]
            indicators = (
                "FAIL",
                "Error",
                "Exception",
                "Expected",
                "Failed asserting",
                "Vite manifest",
                "Tests:",
            )
            highlights = [line for line in lines if any(indicator in line for indicator in indicators)]
            selected = highlights[-12:] + lines[-8:]
            summary = " | ".join(dict.fromkeys(selected)) if selected else "no output"
            label = " ".join(arguments[:3])
            raise CommandError(
                "{} failed ({}): {}".format(label, result.returncode, summary[-6000:]),
                result=result,
            )
        return result

    def _limit(self, value: str) -> str:
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= self.max_output_bytes:
            return value
        return encoded[-self.max_output_bytes :].decode("utf-8", errors="replace")
