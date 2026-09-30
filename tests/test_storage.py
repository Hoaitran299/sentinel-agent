import os
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent.database import Database, DatabaseUrl
from autofix_agent.log_reader import LogEvent
from autofix_agent.storage import IMPORT_TABLES, StateStore


MYSQL_URL = os.environ.get("AI_FIX_TEST_DATABASE_URL", "")


def event() -> LogEvent:
    return LogEvent(
        timestamp="2026-09-27 22:00:00",
        environment="local",
        level="ERROR",
        message="phone cannot be null",
        sanitized_excerpt="phone cannot be null",
        top_application_frame="app/Http/Controllers/ProfileController.php:39",
        fingerprint="a" * 64,
    )


def reset_mysql(url: DatabaseUrl) -> None:
    database = Database(url)
    try:
        database.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in IMPORT_TABLES:
            database.execute("DROP TABLE IF EXISTS {}".format(table))
        database.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        database.close()


class StateStoreContract:
    """Behaviour shared by every supported state database."""

    def open_store(self) -> StateStore:
        raise NotImplementedError

    def setUp(self):
        self.store = self.open_store()

    def tearDown(self):
        self.store.close()

    def test_triggers_only_on_third_occurrence(self):
        first = self.store.record_event(event(), "b" * 40, 3, 600, observed_at=100)
        second = self.store.record_event(event(), "b" * 40, 3, 600, observed_at=101)
        third = self.store.record_event(event(), "b" * 40, 3, 600, observed_at=102)
        fourth = self.store.record_event(event(), "b" * 40, 3, 600, observed_at=103)

        self.assertFalse(first.should_trigger)
        self.assertFalse(second.should_trigger)
        self.assertTrue(third.should_trigger)
        self.assertFalse(fourth.should_trigger)
        self.assertEqual(third.incident_id, fourth.incident_id)
        self.assertEqual(4, fourth.occurrence_count)
        self.assertEqual(4, len(self.store.occurrences(third.incident_id)))

    def test_expired_window_creates_new_incident(self):
        first = self.store.record_event(event(), "b" * 40, 3, 10, observed_at=100)
        second = self.store.record_event(event(), "b" * 40, 3, 10, observed_at=111)

        self.assertNotEqual(first.incident_id, second.incident_id)
        self.assertEqual(1, second.occurrence_count)

    def test_failed_incident_can_be_requeued_for_an_explicit_retry(self):
        decision = self.store.record_event(event(), "b" * 40, 1, 600, observed_at=100)
        self.store.fail_incident(decision.incident_id, "verification failed")

        self.assertTrue(self.store.requeue_failed(decision.incident_id))
        self.assertEqual("queued", self.store.incident(decision.incident_id)["status"])
        self.assertEqual([decision.incident_id], self.store.queued())
        self.assertFalse(self.store.requeue_failed(decision.incident_id))

    def test_claim_is_single_use_and_run_state_round_trips(self):
        decision = self.store.record_event(event(), "b" * 40, 1, 600, observed_at=100)
        run_id = self.store.claim(decision.incident_id)

        self.assertIsNotNone(run_id)
        self.assertIsNone(self.store.claim(decision.incident_id))
        self.store.update_run(run_id, "verifying", verification=[{"command": ["php"], "returncode": 0}])
        runs = self.store.runs(decision.incident_id)
        self.assertEqual("verifying", runs[0]["status"])
        self.assertEqual(0, runs[0]["verification"][0]["returncode"])

    def test_cursor_upsert_and_events_timeline(self):
        path = Path("/tmp/laravel.log")
        self.store.save_cursor(path, "11", 100)
        self.store.save_cursor(path, "11", 250)
        self.assertEqual(("11", 250), self.store.cursor(path))
        self.assertIsNotNone(self.store.watcher_heartbeat())

        decision = self.store.record_event(event(), "b" * 40, 3, 600, observed_at=100)
        self.store.add_event("OBSERVE", "Matching error observed", {"count": "1/3"}, incident_id=decision.incident_id)
        self.store.add_event("OBSERVE", "Watcher stopped", {})
        timeline = self.store.events(incident_id=decision.incident_id)

        self.assertEqual(1, len(timeline))
        self.assertEqual({"count": "1/3"}, timeline[0]["context"])
        self.assertEqual(2, len(self.store.events()))

    def fixed_incident(self, source_commit: str = "b" * 40) -> str:
        decision = self.store.record_event(event(), source_commit, 1, 600, observed_at=100)
        self.store.claim(decision.incident_id)
        self.store.complete_incident(decision.incident_id, "ai-fix/x", "c" * 40, "r.md", "https://github.com/o/r/pull/7")
        return decision.incident_id

    def test_repeats_after_a_fix_are_attached_instead_of_starting_a_new_run(self):
        incident_id = self.fixed_incident()

        # even after master moves, the unmerged/undeployed fix still covers this error
        repeats = [self.store.record_event(event(), "d" * 40, 1, 600, observed_at=200 + n) for n in range(5)]

        self.assertTrue(all(r.suppressed and not r.should_trigger for r in repeats))
        self.assertEqual({incident_id}, {r.incident_id for r in repeats})
        self.assertEqual("https://github.com/o/r/pull/7", repeats[-1].pull_request_url)
        incident = self.store.incident(incident_id)
        self.assertEqual(5, incident["suppressed_count"])
        self.assertEqual(6, incident["occurrence_count"])
        self.assertEqual("waiting_for_review", incident["status"])
        self.assertEqual(1, len(self.store.recent()))

    def test_merged_and_dismissed_fixes_keep_suppressing_until_released(self):
        incident_id = self.fixed_incident()
        self.assertEqual([incident_id], [row["id"] for row in self.store.open_pull_requests()])
        self.assertTrue(self.store.set_review_outcome(incident_id, "merged"))
        self.assertFalse(self.store.set_review_outcome(incident_id, "dismissed"))
        self.assertEqual([], self.store.open_pull_requests())
        self.assertTrue(self.store.record_event(event(), "b" * 40, 1, 600, observed_at=300).suppressed)

        self.assertTrue(self.store.release(incident_id))
        self.assertFalse(self.store.release(incident_id))
        fresh = self.store.record_event(event(), "b" * 40, 1, 600, observed_at=400)
        self.assertFalse(fresh.suppressed)
        self.assertTrue(fresh.should_trigger)
        self.assertNotEqual(incident_id, fresh.incident_id)

    def test_failed_incident_does_not_suppress_new_attempts(self):
        decision = self.store.record_event(event(), "b" * 40, 1, 600, observed_at=100)
        self.store.fail_incident(decision.incident_id, "verification failed")
        self.assertFalse(self.store.record_event(event(), "b" * 40, 1, 600, observed_at=200).suppressed)

    def test_clear_deletes_history_but_keeps_cursor(self):
        path = Path("/tmp/laravel.log")
        self.store.save_cursor(path, "11", 250)
        self.fixed_incident()
        self.store.add_event("OBSERVE", "x", {})

        snapshot = self.store.snapshot()
        deleted = self.store.clear()

        self.assertEqual(1, len(snapshot["incidents"]))
        self.assertEqual({"incidents": 1, "runs": 1, "occurrences": 1, "run_events": 1}, deleted)
        self.assertEqual([], self.store.recent())
        self.assertEqual(("11", 250), self.store.cursor(path))

    def test_clear_refuses_while_an_agent_run_is_in_progress(self):
        decision = self.store.record_event(event(), "b" * 40, 1, 600, observed_at=100)
        self.store.claim(decision.incident_id)
        with self.assertRaises(RuntimeError):
            self.store.clear()
        self.assertEqual("running", self.store.incident(decision.incident_id)["status"])

    def test_resolve_prefix_rejects_non_hex_input(self):
        decision = self.store.record_event(event(), "b" * 40, 3, 600, observed_at=100)
        self.assertEqual(decision.incident_id, self.store.resolve_prefix(decision.incident_id[:8]))
        with self.assertRaises(ValueError):
            self.store.resolve_prefix("%")


class SqliteStateStoreTest(StateStoreContract, unittest.TestCase):
    def open_store(self) -> StateStore:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        return StateStore(Path(self.temp.name) / "state.sqlite3")

    def test_migrates_legacy_sqlite_file_without_new_columns(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        database = Database(DatabaseUrl.sqlite(legacy))
        database.execute("CREATE TABLE incidents (id TEXT PRIMARY KEY, fingerprint TEXT, source_commit TEXT, status TEXT, occurrence_count INTEGER, threshold INTEGER, window_started_at REAL, last_seen_at REAL, log_excerpt TEXT, message TEXT, top_application_frame TEXT, branch_name TEXT, commit_sha TEXT, report_path TEXT, failure_reason TEXT)")
        database.close()

        store = StateStore(legacy)
        self.addCleanup(store.close)
        self.assertIn("pull_request_url", store.db.columns("incidents"))


@unittest.skipUnless(MYSQL_URL, "set AI_FIX_TEST_DATABASE_URL=mysql://... to run MySQL tests")
class MysqlStateStoreTest(StateStoreContract, unittest.TestCase):
    def open_store(self) -> StateStore:
        url = DatabaseUrl.parse(MYSQL_URL)
        reset_mysql(url)
        return StateStore(url)

    def test_imports_existing_sqlite_state(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        legacy = StateStore(Path(temp.name) / "state.sqlite3")
        decision = legacy.record_event(event(), "b" * 40, 1, 600, observed_at=100)
        run_id = legacy.claim(decision.incident_id)
        legacy.complete_incident(decision.incident_id, "ai-fix/x", "c" * 40, "var/reports/x.md", "https://example.test/pr/1")

        copied = self.store.import_from(legacy)
        again = self.store.import_from(legacy)  # idempotent
        legacy.close()

        self.assertEqual(1, copied["incidents"])
        self.assertEqual(1, again["incidents"])
        imported = self.store.incident(decision.incident_id)
        self.assertEqual("waiting_for_review", imported["status"])
        self.assertEqual(run_id, self.store.runs(decision.incident_id)[0]["id"])
        self.assertEqual(1, len(self.store.occurrences(decision.incident_id)))


class DatabaseUrlTest(unittest.TestCase):
    def test_parses_mysql_url_with_encoded_password(self):
        url = DatabaseUrl.parse("mysql://aifix:p%40ss%3Aword@127.0.0.1:3307/ai_fix")
        self.assertEqual(("mysql", "aifix", "p@ss:word", 3307, "ai_fix"), (url.dialect, url.user, url.password, url.port, url.database))
        self.assertNotIn("p@ss", url.describe())

    def test_rejects_unknown_scheme_and_missing_database(self):
        with self.assertRaises(ValueError):
            DatabaseUrl.parse("postgres://x@y/z")
        with self.assertRaises(ValueError):
            DatabaseUrl.parse("mysql://x@127.0.0.1/")


if __name__ == "__main__":
    unittest.main()
