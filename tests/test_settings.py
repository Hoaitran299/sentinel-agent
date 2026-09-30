import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autofix_agent import settings
from autofix_agent.storage import StateStore
from test_dashboard import event, make_config


class RuntimeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.store = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    def test_defaults_come_from_config_and_saved_values_override(self):
        self.assertEqual(settings.RuntimeSettings("report_and_fix", 3, 600), settings.load(self.store, self.config))
        settings.save(self.store, {"mode": "report_only", "threshold": "5"})
        self.assertEqual(settings.RuntimeSettings("report_only", 5, 600), settings.load(self.store, self.config))

    def test_rejects_invalid_values(self):
        for bad in ({"mode": "yolo"}, {"threshold": 0}, {"threshold": "abc"}, {"window_seconds": 5}, {"threshold": True}, {"extra": 1}):
            with self.assertRaises(ValueError, msg=bad):
                settings.save(self.store, bad)

    def test_invalid_stored_row_falls_back_to_default_for_that_key_only(self):
        self.store.save_settings({"threshold": "not-a-number", "mode": "fix_only"})
        self.assertEqual(settings.RuntimeSettings("fix_only", 3, 600), settings.load(self.store, self.config))

    def test_mode_flags(self):
        self.assertTrue(settings.RuntimeSettings("fix_only", 1, 60).runs_agent)
        self.assertFalse(settings.RuntimeSettings("fix_only", 1, 60).sends_notifications)
        self.assertFalse(settings.RuntimeSettings("report_only", 1, 60).runs_agent)


class ModeBehaviourTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    def record(self, n, threshold=3, mode="report_and_fix"):
        return self.store.record_event(event(), "b" * 40, threshold, 600, observed_at=100 + n, mode=mode)

    def test_report_only_parks_incident_and_suppresses_repeats(self):
        decisions = [self.record(n, mode="report_only") for n in range(5)]

        self.assertEqual([False] * 5, [d.should_trigger for d in decisions])
        self.assertEqual([False, False, True, False, False], [d.reported for d in decisions])
        self.assertTrue(decisions[3].suppressed)
        incident = self.store.incident(decisions[2].incident_id)
        self.assertEqual(("reported", 2), (incident["status"], incident["suppressed_count"]))

        # A human starts the fix from the dashboard: reported -> queued.
        self.assertTrue(self.store.requeue_failed(incident["id"]))
        self.assertEqual([incident["id"]], self.store.queued())

    def test_reported_incident_can_be_closed(self):
        reported = [self.record(n, mode="report_only") for n in range(3)][-1]
        self.assertTrue(self.store.release(reported.incident_id))
        self.assertFalse(self.record(10, mode="report_only").suppressed)

    def test_threshold_change_applies_to_accumulating_incident(self):
        first = self.record(0, threshold=5)
        second = self.record(1, threshold=2)  # operator lowered the threshold in between
        self.assertTrue(second.should_trigger)
        self.assertEqual(2, self.store.incident(first.incident_id)["threshold"])

    def test_trigger_fires_only_on_the_transition(self):
        decisions = [self.record(n, threshold=2) for n in range(4)]
        self.assertEqual([False, True, False, False], [d.should_trigger for d in decisions])


if __name__ == "__main__":
    unittest.main()
