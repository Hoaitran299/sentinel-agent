import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.github import GitHubPublisher
from autofix_agent.process import CommandResult


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def run(self, command, cwd, timeout, **kwargs):
        self.calls.append({"command": list(command), "cwd": cwd, **kwargs})
        stdout = "b" * 40 if list(command)[:2] == ["git", "rev-parse"] else ""
        return CommandResult(list(command), 0, stdout, "")


class StubPublisher(GitHubPublisher):
    def __init__(self, config, executor):
        super().__init__(config, executor)
        self.requests = []
        self.pushed = False

    def _push(self, workspace, branch, token):
        self.pushed = True

    def _request(self, method, path, token, payload=None):
        self.requests.append((method, path, payload))
        if path.startswith("/repos/example/project/git/ref/"):
            return {"object": {"sha": "b" * 40}}
        if path == "/repos/example/project":
            return {"full_name": "example/project", "default_branch": "master"}
        if method == "GET" and "/pulls?" in path:
            return []
        return {"html_url": "https://github.com/example/project/pull/1"}


def config():
    return SimpleNamespace(
        publish_pull_request=True,
        github_token_env="AI_FIX_GITHUB_TOKEN",
        github_repository="example/project",
        github_api_url="https://api.github.com",
        github_git_url="https://github.com/example/project.git",
        github_draft=True,
        base_branch="master",
        report_language="en",
    )


class GitHubPublisherTest(unittest.TestCase):
    def test_creates_pr_only_after_push_and_reuses_no_secret_in_payload(self):
        executor = FakeExecutor()
        publisher = StubPublisher(config(), executor)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_FIX_GITHUB_TOKEN": "github_pat_test-token"}, clear=False
        ):
            result = publisher.publish(
                workspace=Path(directory),
                branch="ai-fix/incident-1234",
                commit_sha="c" * 40,
                incident_id="12345678",
                analysis={"summary": "Validate phone"},
            )

        self.assertTrue(publisher.pushed)
        self.assertTrue(result.created)
        self.assertEqual("https://github.com/example/project/pull/1", result.pull_request_url)
        self.assertNotIn("github_pat_test-token", repr(publisher.requests))

    def test_push_command_uses_clean_url_and_token_only_in_publisher_environment(self):
        executor = FakeExecutor()
        publisher = GitHubPublisher(config(), executor)
        with tempfile.TemporaryDirectory() as directory:
            publisher._push(Path(directory), "ai-fix/example", "github_pat_test-token")

        call = executor.calls[0]
        self.assertNotIn("github_pat_test-token", " ".join(call["command"]))
        self.assertEqual("https://github.com/example/project.git", call["command"][3])
        self.assertEqual("github_pat_test-token", call["environment"]["AI_FIX_GITHUB_TOKEN_VALUE"])
        self.assertNotIn("GITHUB_TOKEN", call["environment"])

    def test_fetches_remote_master_into_a_private_ref_without_changing_checkout(self):
        executor = FakeExecutor()
        publisher = GitHubPublisher(config(), executor)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_FIX_GITHUB_TOKEN": "github_pat_test-token"}, clear=False
        ):
            commit = publisher.fetch_base(Path(directory))

        self.assertEqual("b" * 40, commit)
        fetch = executor.calls[0]
        self.assertEqual(["git", "fetch", "--no-tags"], fetch["command"][:3])
        self.assertIn("refs/ai-fix/base/master", fetch["command"][-1])
        self.assertNotIn("github_pat_test-token", " ".join(fetch["command"]))


if __name__ == "__main__":
    unittest.main()
