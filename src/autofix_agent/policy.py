from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Set

from .config import Config
from .process import ProcessExecutor


class PolicyViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class PolicyBaseline:
    tracked_tests: Dict[str, str]


class WorkspacePolicy:
    def __init__(self, config: Config, executor: ProcessExecutor):
        self.config = config
        self.executor = executor

    def capture(self, workspace: Path) -> PolicyBaseline:
        result = self.executor.run(["git", "ls-files", "tests"], cwd=workspace, timeout=30)
        tracked = {}
        for relative in filter(None, result.stdout.splitlines()):
            tracked[relative] = self._hash(workspace / relative)
        return PolicyBaseline(tracked_tests=tracked)

    def validate_analysis(self, analysis: dict) -> None:
        proposed = analysis.get("proposed_files")
        if not isinstance(proposed, list) or not proposed:
            raise PolicyViolation("analysis must propose at least one file")
        for path in proposed:
            if not isinstance(path, str) or not self._allowed(path):
                raise PolicyViolation("analysis proposed a forbidden path: {}".format(path))

    def enforce(self, workspace: Path, baseline: PolicyBaseline) -> List[str]:
        changed = self.changed_files(workspace)
        if not changed:
            raise PolicyViolation("agent did not change any file")
        if len(changed) > self.config.max_changed_files:
            raise PolicyViolation("changed file limit exceeded")
        for relative in changed:
            if self._protected(relative):
                raise PolicyViolation("protected path changed: {}".format(relative))
            if not self._allowed(relative):
                raise PolicyViolation("path outside writable scope: {}".format(relative))
            if relative in baseline.tracked_tests and self._hash(workspace / relative) != baseline.tracked_tests[relative]:
                raise PolicyViolation("existing test was modified: {}".format(relative))
        if self.config.require_new_test and not any(
            relative.startswith("tests/") and relative not in baseline.tracked_tests for relative in changed
        ):
            raise PolicyViolation("a new regression test is required")
        if self._diff_lines(workspace, changed) > self.config.max_diff_lines:
            raise PolicyViolation("diff line limit exceeded")
        return changed

    def changed_files(self, workspace: Path) -> List[str]:
        result = self.executor.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=workspace,
            timeout=30,
        )
        changed: Set[str] = set()
        entries = result.stdout.split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            status = entry[:2]
            path = entry[3:]
            if "R" in status or "C" in status:
                raise PolicyViolation("renames and copies are not allowed")
            changed.add(path)
        return sorted(changed)

    def _allowed(self, path: str) -> bool:
        return self._matches(path, self.config.allowed_paths)

    def _protected(self, path: str) -> bool:
        return self._matches(path, self.config.protected_paths)

    @staticmethod
    def _matches(path: str, rules: Iterable[str]) -> bool:
        if path.startswith("/") or ".." in Path(path).parts:
            return False
        return any(path == rule or (rule.endswith("/") and path.startswith(rule)) for rule in rules)

    def _diff_lines(self, workspace: Path, changed: Iterable[str]) -> int:
        total = 0
        tracked = self.executor.run(["git", "diff", "--numstat"], cwd=workspace, timeout=30).stdout
        for row in tracked.splitlines():
            parts = row.split("\t", 2)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                total += int(parts[0]) + int(parts[1])
        for relative in changed:
            result = self.executor.run(["git", "ls-files", "--error-unmatch", relative], cwd=workspace, timeout=30, check=False)
            if result.returncode != 0:
                total += len((workspace / relative).read_text(encoding="utf-8", errors="replace").splitlines())
        return total

    @staticmethod
    def _hash(path: Path) -> str:
        if not path.is_file():
            return "missing"
        return hashlib.sha256(path.read_bytes()).hexdigest()
