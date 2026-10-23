import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor_core as core  # noqa: E402

NOW = datetime(2026, 10, 23, 12, 17, tzinfo=timezone.utc)
DAILY = {"name": "Sales alerts", "repo": "me/sales", "workflow": "daily.yml", "max_age_hours": 26}
ACTIVE = {"state": "active"}


def run(hours_ago, conclusion="success", status="completed"):
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"status": status, "conclusion": conclusion, "updated_at": t}


class EvaluateTests(unittest.TestCase):
    def test_recent_success_is_ok(self):
        r = core.evaluate(DAILY, ACTIVE, [run(6), run(30)], NOW)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["last_success_ago"], "6h ago")

    def test_last_run_failed(self):
        r = core.evaluate(DAILY, ACTIVE, [run(2, "failure"), run(26)], NOW)
        self.assertEqual(r["status"], "down")
        self.assertTrue(r["reasons"][0].startswith("last run failed"))

    def test_silent_stop_is_caught(self):
        # every run was green, but nothing has run for two days
        r = core.evaluate(DAILY, ACTIVE, [run(50), run(74)], NOW)
        self.assertEqual(r["status"], "down")
        self.assertIn("no successful run for", r["reasons"][0])

    def test_disabled_by_github_inactivity(self):
        r = core.evaluate(DAILY, {"state": "disabled_inactivity"}, [run(6)], NOW)
        self.assertEqual(r["status"], "down")
        self.assertTrue(r["disabled"])
        self.assertIn("60 days", r["reasons"][0])

    def test_cancelled_and_running_runs_are_ignored(self):
        runs = [run(0.1, None, "in_progress"), run(1, "cancelled"), run(5)]
        self.assertEqual(core.evaluate(DAILY, ACTIVE, runs, NOW)["status"], "ok")

    def test_missing_workflow(self):
        r = core.evaluate(DAILY, None, [], NOW)
        self.assertIn("workflow file not found", r["reasons"])

    def test_stale_published_data_even_when_runs_are_green(self):
        m = dict(DAILY, data_url="https://x/data.json", data_max_age_hours=26)
        r = core.evaluate(m, ACTIVE, [run(6)], NOW, "2026-10-21 06:15 UTC")
        self.assertEqual(r["status"], "down")
        self.assertIn("published data is", r["reasons"][0])

    def test_fresh_published_data(self):
        m = dict(DAILY, data_url="https://x/data.json")
        r = core.evaluate(m, ACTIVE, [run(6)], NOW, "2026-10-23 06:15 UTC")
        self.assertEqual(r["status"], "ok")

    def test_success_rate(self):
        r = core.evaluate(DAILY, ACTIVE, [run(1), run(2, "failure"), run(3), run(4)], NOW)
        self.assertEqual(r["success_rate"], 0.75)


class AlertTests(unittest.TestCase):
    def result(self, status):
        return {"name": "Sales alerts", "repo": "me/sales", "workflow": "daily.yml", "status": status,
                "reasons": ["last run failed (2h ago)"] if status == "down" else [], "last_success_ago": "1h ago"}

    def test_no_alert_while_healthy(self):
        msgs, _ = core.alerts_for([self.result("ok")], {"Sales alerts": {"status": "ok"}}, NOW)
        self.assertEqual(msgs, [])

    def test_alert_once_when_it_breaks(self):
        msgs, state = core.alerts_for([self.result("down")], {"Sales alerts": {"status": "ok"}}, NOW)
        self.assertEqual(len(msgs), 1)
        self.assertIn("DOWN", msgs[0])
        # an hour later, still down: no repeat
        later = NOW + timedelta(hours=1)
        msgs2, _ = core.alerts_for([self.result("down")], state, later)
        self.assertEqual(msgs2, [])

    def test_daily_reminder_while_still_down(self):
        _, state = core.alerts_for([self.result("down")], {}, NOW)
        msgs, _ = core.alerts_for([self.result("down")], state, NOW + timedelta(hours=25))
        self.assertIn("still down", msgs[0])

    def test_recovery_message(self):
        _, state = core.alerts_for([self.result("down")], {}, NOW)
        msgs, state = core.alerts_for([self.result("ok")], state, NOW + timedelta(hours=3))
        self.assertIn("recovered after 3h", msgs[0])
        self.assertEqual(state["Sales alerts"]["status"], "ok")

    def test_first_run_healthy_is_silent(self):
        msgs, _ = core.alerts_for([self.result("ok")], {}, NOW)
        self.assertEqual(msgs, [])


class DigestAndHistoryTests(unittest.TestCase):
    def test_digest_all_healthy(self):
        rs = [{"name": "A", "status": "ok", "last_success_ago": "2h ago", "success_rate": 1.0, "runs_counted": 20},
              {"name": "B", "status": "ok", "last_success_ago": "9 min ago", "success_rate": None, "runs_counted": 0}]
        text = core.digest_text(rs)
        self.assertTrue(text.startswith(":white_check_mark: All 2 automations healthy"))
        self.assertIn("100% of last 20 runs", text)

    def test_history_keeps_48_hours_and_one_entry_per_hour(self):
        history = {}
        for h in range(60):
            history = core.add_history(history, [{"name": "A", "status": "ok"}], NOW - timedelta(hours=60 - h))
        history = core.add_history(history, [{"name": "A", "status": "down"}], NOW)
        history = core.add_history(history, [{"name": "A", "status": "ok"}], NOW + timedelta(minutes=5))
        self.assertLessEqual(len(history["A"]), core.HISTORY_HOURS)
        self.assertEqual(history["A"][-1]["status"], "ok")
        self.assertEqual(len({e["hour"] for e in history["A"]}), len(history["A"]))


if __name__ == "__main__":
    unittest.main()
