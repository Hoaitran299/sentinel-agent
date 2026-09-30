import os
import sys
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.notifier import TelegramNotifier
from autofix_agent.pipeline import AgentPipeline
from autofix_agent.storage import StateStore
from test_dashboard import event, make_config
from test_github import FakeExecutor, StubPublisher, config as github_config


class PullRequestStateTest(unittest.TestCase):
    def state_for(self, pull):
        publisher = StubPublisher(github_config(), FakeExecutor())
        publisher._request = lambda method, path, token, payload=None: (publisher.requests.append(path) or pull)
        with patch.dict(os.environ, {"AI_FIX_GITHUB_TOKEN": "github_pat_test-token"}):
            state = publisher.pull_request_state("https://github.com/example/project/pull/42")
        self.assertEqual(["/repos/example/project/pulls/42"], publisher.requests)
        return state

    def test_maps_github_pull_to_open_merged_or_closed(self):
        self.assertEqual("open", self.state_for({"state": "open", "merged": False}))
        self.assertEqual("merged", self.state_for({"state": "closed", "merged": True}))
        self.assertEqual("closed", self.state_for({"state": "closed", "merged": False}))

    def test_rejects_non_pull_request_urls(self):
        publisher = StubPublisher(github_config(), FakeExecutor())
        with self.assertRaises(ValueError):
            publisher.pull_request_state("https://github.com/example/project/issues/42")


class FakePublisher:
    def __init__(self, states):
        self.states = states

    def enabled(self):
        return True

    def pull_request_state(self, url):
        state = self.states[url]
        if isinstance(state, Exception):
            raise state
        return state


class SyncPullRequestsTest(unittest.TestCase):
    def test_sync_moves_merged_and_closed_prs_and_survives_api_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            self.addCleanup(store.close)
            ids = {}
            for number, fingerprint in ((1, "a"), (2, "b"), (3, "c"), (4, "d")):
                decision = store.record_event(replace(event(), fingerprint=fingerprint * 64), "f" * 40, 1, 600)
                store.claim(decision.incident_id)
                store.complete_incident(decision.incident_id, "ai-fix/{}".format(number), "c" * 40, "r.md", "https://github.com/o/r/pull/{}".format(number))
                ids[number] = decision.incident_id

            pipeline = AgentPipeline.__new__(AgentPipeline)
            pipeline.config = make_config(Path(directory))
            pipeline.store = store
            sent = []
            pipeline.notifier = TelegramNotifier(
                replace(pipeline.config, telegram_enabled=True, telegram_chat_id="-100123"),
                transport=lambda method, payload: sent.append(payload["text"]) or {},
            )
            pipeline.publisher = FakePublisher({
                "https://github.com/o/r/pull/1": "open",
                "https://github.com/o/r/pull/2": "merged",
                "https://github.com/o/r/pull/3": "closed",
                "https://github.com/o/r/pull/4": RuntimeError("GitHub API returned 502"),
            })
            with redirect_stdout(StringIO()):
                pipeline.sync_pull_requests()

            status = {number: store.incident(incident_id)["status"] for number, incident_id in ids.items()}
            self.assertEqual({1: "waiting_for_review", 2: "merged", 3: "dismissed", 4: "waiting_for_review"}, status)
            self.assertIsNotNone(store.incident(ids[2])["resolved_at"])
            self.assertEqual(2, len(sent))
            self.assertIn("merged", sent[0])
            self.assertIn("closed without merge", sent[1])


if __name__ == "__main__":
    unittest.main()
