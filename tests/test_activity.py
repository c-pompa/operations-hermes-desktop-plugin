import importlib.util
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

NOW = time.time()
HOUR = NOW - NOW % 3600 + 1800  # mid-hour, so +-10 min stays in the same hour bucket
if HOUR > NOW:
    HOUR -= 3600


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.metrics, self.kanban = root / "metrics.db", root / "kanban.db"
        m = sqlite3.connect(self.metrics)
        m.executescript("""
CREATE TABLE records (ts REAL, host TEXT, event TEXT, kind TEXT, platform TEXT, model TEXT, error TEXT,
  label TEXT, device TEXT);
CREATE TABLE conformance_results (ts REAL, host TEXT, rule TEXT, status TEXT, value TEXT, verified INTEGER);
""")
        for dt in (-600, 0, 600):
            m.execute("INSERT INTO records VALUES (?, 'mini', 'router_request', 'error', NULL, 'qwen', 'http_598',"
                      " NULL, 'MBP')", (HOUR + dt - 3600 * 3,))
        m.execute("INSERT INTO records VALUES (?, 'mini', 'router_request', 'error', NULL, 'qwen', 'http_598',"
                  " NULL, 'MBP')", (HOUR - 3600 * 5,))
        for ts, platform, label in [(NOW - 90000, "discord", "connected"), (NOW - 7000, "discord", "connected"),
                                    (NOW - 6000, "discord", "fatal"), (NOW - 5000, "discord", "fatal"),
                                    (NOW - 4000, "discord", "connected"), (NOW - 3000, "api", "connected"),
                                    (NOW - 2000, "feishu", "stale")]:
            m.execute("INSERT INTO records (ts, kind, platform, label) VALUES (?, 'gateway_status', ?, ?)",
                      (ts, platform, label))
        for ts, host, rule, status, verified in [(NOW - 9000, "mbp", "jobs", "pass", 1), (NOW - 8000, "mbp", "jobs", "pass", 1),
                                                 (NOW - 7000, "mbp", "jobs", "fail", 0), (NOW - 6000, "mini", "keys", "na", 1),
                                                 (NOW - 5000, "mini", "stamp", "warn", 1)]:
            m.execute("INSERT INTO conformance_results VALUES (?, ?, ?, ?, '5 of 7', ?)", (ts, host, rule, status, verified))
        m.commit()
        m.close()
        k = sqlite3.connect(self.kanban)
        k.executescript("""
CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, created_at INTEGER);
CREATE TABLE task_events (task_id TEXT, kind TEXT, created_at INTEGER);
INSERT INTO tasks VALUES ('t1', 'Ship it', 'coder', 'done', 0);
""")
        for kind in ("heartbeat", "spawned", "claimed", "completed"):
            k.execute("INSERT INTO task_events VALUES ('t1', ?, ?)", (kind, int(NOW) - 100))
        k.commit()
        k.close()
        self.saved = api._METRICS_DB, api._KANBAN_DB, api._FIXES
        api._METRICS_DB, api._KANBAN_DB, api._FIXES = self.metrics, self.kanban, root / "fixes.json"

    def tearDown(self):
        api._METRICS_DB, api._KANBAN_DB, api._FIXES = self.saved
        self.tmp.cleanup()

    def by_category(self, hours=24):
        out = api.activity_route(hours)
        self.assertEqual(out["errors"], {})
        groups = {}
        for g in out["items"]:
            groups.setdefault(g["category"], []).append(g)
        return out, groups

    def test_errors_group_per_hour_with_raw_lines(self):
        _, g = self.by_category()
        errs = [(e["count"], len(e["raw"])) for e in g["incident"] if e["kind"] == "error"]
        self.assertEqual(errs, [(3, 3), (1, 1)])
        self.assertEqual(g["incident"][-1]["raw"][0][1], "router_request · qwen · http_598")

    def test_gateway_and_host_checks_report_changes_only(self):
        _, g = self.by_category()
        self.assertEqual([e["text"] for e in g["incident"] if e["kind"] == "gateway"],
                         ["discord went connected", "discord went fatal"])
        self.assertEqual([(e["where"], e["text"], e["raw"][0][1]) for e in g["conformance"]],
                         [("mini", "stamp is warn", "5 of 7"), ("mbp", "jobs pass -> fail", "5 of 7 (unverified sender)")])

    def test_fix_requests_are_logged(self):
        api._FIXES.write_text(json.dumps({
            "mbp": {"ts": NOW - 100, "via": "hand", "card": None, "rules": ["jobs"]},
            "mini": {"ts": NOW - 50, "via": "ops", "card": "t_9", "rules": ["stamp", "keys"]},
            "old": {"ts": NOW - 90000, "via": "hand", "card": None, "rules": ["jobs"]}}))
        _, g = self.by_category()
        fixes = [(e["where"], e["text"], [r[1] for r in e["raw"]]) for e in g["conformance"] if e["kind"] == "fix requested"]
        self.assertEqual(fixes, [("mini", "Fix asked for on mini, as card t_9 for ops", ["stamp, keys"]),
                                 ("mbp", "Fix asked for on mbp, to run by hand", ["jobs"])])

    def test_task_noise_is_left_out(self):
        _, g = self.by_category()
        self.assertEqual(sorted(e["kind"] for e in g["task"]), ["task claimed", "task completed"])

    def test_counts_and_newest_first(self):
        out, _ = self.by_category()
        self.assertEqual(out["counts"], {"incident": 4, "conformance": 2, "task": 2})
        self.assertEqual([g["ts"] for g in out["items"]], sorted((g["ts"] for g in out["items"]), reverse=True))

    def test_older_metrics_without_host_checks_and_missing_kanban(self):
        m = sqlite3.connect(self.metrics)
        m.execute("DROP TABLE conformance_results")
        m.commit()
        m.close()
        api._KANBAN_DB = Path(self.tmp.name) / "absent.db"
        out = api.activity_route(24)
        self.assertEqual(set(out["errors"]), {"kanban"})
        self.assertEqual(set(out["counts"]), {"incident"})
        self.assertFalse(api._KANBAN_DB.exists())

    def test_read_only(self):
        before = self.metrics.read_bytes(), self.kanban.read_bytes()
        api.activity_route(168)
        self.assertEqual((self.metrics.read_bytes(), self.kanban.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
