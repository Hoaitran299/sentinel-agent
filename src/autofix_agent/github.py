from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .config import Config
from .i18n import t
from .process import ProcessExecutor, safe_environment


PULL_NUMBER = re.compile(r"/pull/(\d+)(?:[/?#]|$)")


@dataclass(frozen=True)
class PublishResult:
    pull_request_url: str
    created: bool


class GitHubPublisher:
    """Trusted publisher. This component is never callable by Claude."""

    def __init__(self, config: Config, executor: ProcessExecutor):
        self.config = config
        self.executor = executor

    def enabled(self) -> bool:
        return self.config.publish_pull_request

    def preflight(self) -> Dict[str, Any]:
        token = self._token()
        repository = self._request("GET", "/repos/{}".format(self.config.github_repository), token)
        reference = self._request(
            "GET",
            "/repos/{}/git/ref/heads/{}".format(
                self.config.github_repository,
                urllib.parse.quote(self.config.base_branch, safe=""),
            ),
            token,
        )
        return {
            "full_name": repository.get("full_name"),
            "default_branch": repository.get("default_branch"),
            "base_sha": reference.get("object", {}).get("sha"),
        }

    def publish(
        self,
        workspace: Path,
        branch: str,
        commit_sha: str,
        incident_id: str,
        analysis: dict,
    ) -> PublishResult:
        token = self._token()
        parent = self.executor.run(["git", "rev-parse", "HEAD^"], cwd=workspace, timeout=30).stdout.strip()
        remote = self.preflight().get("base_sha")
        if parent != remote:
            raise RuntimeError("verified commit is not based on the current remote {}".format(self.config.base_branch))
        self._push(workspace, branch, token)

        owner = self.config.github_repository.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "head": "{}:{}".format(owner, branch),
                "base": self.config.base_branch,
            }
        )
        existing = self._request(
            "GET",
            "/repos/{}/pulls?{}".format(self.config.github_repository, query),
            token,
        )
        if isinstance(existing, list) and existing:
            return PublishResult(str(existing[0]["html_url"]), created=False)

        language = self.config.report_language
        title = t(language, "pr_title", incident=incident_id[:8])
        summary = str(analysis.get("summary", t(language, "pr_summary_fallback")))[:1000]
        body = "\n".join(
            [
                "## {}".format(t(language, "pr_heading")),
                "",
                "- {}: `{}`".format(t(language, "incident"), incident_id),
                "- {}: `{}`".format(t(language, "verified_commit"), commit_sha),
                "- {}: `{}`".format(t(language, "pr_source_branch"), branch),
                "",
                "### {}".format(t(language, "pr_summary_heading")),
                "",
                summary,
                "",
                t(language, "pr_gate_note"),
                t(language, "pr_credential_note"),
            ]
        )
        created = self._request(
            "POST",
            "/repos/{}/pulls".format(self.config.github_repository),
            token,
            {
                "title": title,
                "head": branch,
                "base": self.config.base_branch,
                "body": body,
                "draft": self.config.github_draft,
            },
        )
        return PublishResult(str(created["html_url"]), created=True)

    def pull_request_state(self, pull_request_url: str) -> str:
        """Return `open`, `merged` or `closed` for a PR this publisher created."""
        match = PULL_NUMBER.search(pull_request_url)
        if match is None:
            raise ValueError("not a pull request URL")
        pull = self._request(
            "GET",
            "/repos/{}/pulls/{}".format(self.config.github_repository, match.group(1)),
            self._token(),
        )
        if pull.get("merged") or pull.get("merged_at"):
            return "merged"
        return "closed" if pull.get("state") == "closed" else "open"

    def fetch_base(self, source_repo: Path) -> str:
        token = self._token()
        private_ref = "refs/ai-fix/base/{}".format(self.config.base_branch)
        with self._git_credentials(token) as environment:
            self.executor.run(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    self.config.github_git_url,
                    "+refs/heads/{}:{}".format(self.config.base_branch, private_ref),
                ],
                cwd=source_repo,
                timeout=180,
                environment=environment,
            )
        commit = self.executor.run(
            ["git", "rev-parse", "--verify", "{}^{{commit}}".format(private_ref)],
            cwd=source_repo,
            timeout=30,
        ).stdout.strip().lower()
        return commit

    def _push(self, workspace: Path, branch: str, token: str) -> None:
        with self._git_credentials(token) as environment:
            self.executor.run(
                [
                    "git",
                    "push",
                    "--set-upstream",
                    self.config.github_git_url,
                    "HEAD:refs/heads/{}".format(branch),
                ],
                cwd=workspace,
                timeout=180,
                environment=environment,
            )

    @contextmanager
    def _git_credentials(self, token: str) -> Iterator[dict]:
        with tempfile.TemporaryDirectory(prefix="ai-fix-askpass-") as directory:
            helper = Path(directory) / "askpass.sh"
            helper.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                "  *) printf '%s\\n' \"$AI_FIX_GITHUB_TOKEN_VALUE\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            helper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            environment = safe_environment(
                {
                    "GIT_ASKPASS": str(helper),
                    "GIT_TERMINAL_PROMPT": "0",
                    "AI_FIX_GITHUB_TOKEN_VALUE": token,
                }
            )
            yield environment

    def _token(self) -> str:
        token = os.environ.get(self.config.github_token_env, "").strip()
        if not token:
            raise RuntimeError("{} is required when PR publishing is enabled".format(self.config.github_token_env))
        if any(character.isspace() for character in token):
            raise RuntimeError("GitHub token contains whitespace")
        if not token.startswith(("github_pat_", "ghp_")):
            raise RuntimeError(
                "{} is not a GitHub personal access token; use a complete fine-grained token starting with github_pat_".format(
                    self.config.github_token_env
                )
            )
        return token

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.config.github_api_url + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer {}".format(token),
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "demo-auto-fixbug-orchestrator/0.1",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("message", "GitHub API error")
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = "GitHub API error"
            if error.code == 401:
                raise RuntimeError(
                    "GitHub rejected the token (401); replace revoked, expired, malformed, or incomplete credentials"
                ) from error
            raise RuntimeError("GitHub API returned {}: {}".format(error.code, str(detail)[:300])) from error
