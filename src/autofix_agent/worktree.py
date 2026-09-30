from __future__ import annotations

import base64
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .console import stage
from .process import ProcessExecutor, safe_environment


@dataclass(frozen=True)
class Workspace:
    path: Path
    branch: str
    source_commit: str


class GitWorktreeManager:
    def __init__(self, config: Config, executor: ProcessExecutor):
        self.config = config
        self.executor = executor

    def resolve_source(self) -> str:
        reference = "refs/heads/{}^{{commit}}".format(self.config.base_branch)
        result = self.executor.run(
            ["git", "rev-parse", "--verify", reference],
            cwd=self.config.source_repo,
            timeout=30,
        )
        commit = result.stdout.strip().lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
            raise RuntimeError("base branch did not resolve to a full commit")
        return commit

    def create(self, incident_id: str, fingerprint: str, source_commit: str, run_id: str) -> Workspace:
        self.config.workspace_root.mkdir(parents=True, exist_ok=True)
        branch = "ai-fix/incident-{}-{}-{}".format(incident_id[:8], fingerprint[:8], run_id[:6])
        path = (self.config.workspace_root / run_id).resolve()
        if path.parent != self.config.workspace_root:
            raise RuntimeError("unsafe workspace path")
        if path.exists():
            raise RuntimeError("workspace already exists: {}".format(path))
        self.executor.run(
            ["git", "worktree", "add", "-b", branch, str(path), source_commit],
            cwd=self.config.source_repo,
            timeout=120,
        )
        return Workspace(path=path, branch=branch, source_commit=source_commit)

    def bootstrap(self, workspace: Workspace) -> None:
        if not self.config.bootstrap_dependencies:
            stage("GUARD", "Dependency bootstrap disabled by configuration")
            return
        stage("GUARD", "Bootstrapping isolated dependencies", workspace=workspace.path)
        key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        testing_environment = safe_environment({"APP_KEY": "base64:" + key})
        env_file = workspace.path / ".env.testing"
        try:
            env_file.write_text(
                "\n".join(
                    [
                        "APP_ENV=testing",
                        "APP_KEY=base64:{}".format(key),
                        "APP_DEBUG=false",
                        "DB_CONNECTION=sqlite",
                        "DB_DATABASE=:memory:",
                        "CACHE_STORE=array",
                        "SESSION_DRIVER=array",
                        "QUEUE_CONNECTION=sync",
                        "MAIL_MAILER=array",
                        "INCIDENT_RECORDING_ENABLED=false",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            if (workspace.path / "composer.lock").exists():
                self.executor.run(
                    ["composer", "install", "--no-interaction", "--prefer-dist", "--no-scripts", "--no-progress"],
                    cwd=workspace.path,
                    timeout=self.config.claude_timeout_seconds,
                    environment=testing_environment,
                )
                self.executor.run(
                    ["php", "artisan", "package:discover", "--ansi"],
                    cwd=workspace.path,
                    timeout=120,
                    environment=testing_environment,
                )
            if (workspace.path / "package-lock.json").exists():
                self.executor.run(
                    ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                    cwd=workspace.path,
                    timeout=self.config.claude_timeout_seconds,
                    environment=testing_environment,
                )
                # Feature tests render Blade layouts that require Vite's manifest.
                # Build a clean baseline before Claude receives write access; the
                # trusted verification pipeline builds again after the fix.
                self.executor.run(
                    ["npm", "run", "build"],
                    cwd=workspace.path,
                    timeout=self.config.claude_timeout_seconds,
                    environment=testing_environment,
                )
        finally:
            env_file.unlink(missing_ok=True)

    def cleanup(self, workspace: Optional[Workspace]) -> None:
        if workspace is None:
            return
        path = workspace.path.resolve()
        if path.parent != self.config.workspace_root or not path.exists():
            return
        stage("GIT", "Removing isolated worktree; branch is retained", branch=workspace.branch)
        try:
            self.executor.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=self.config.source_repo,
                timeout=120,
            )
        except Exception:
            # Only remove a path proven to be a direct child of the managed root.
            shutil.rmtree(str(path), ignore_errors=True)
            self.executor.run(["git", "worktree", "prune"], cwd=self.config.source_repo, timeout=30, check=False)
