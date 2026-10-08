import importlib.util
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

METRICS_SCHEMA = """
CREATE TABLE records (id INTEGER PRIMARY KEY, ts REAL, host TEXT, event TEXT, kind TEXT, platform TEXT,
  model TEXT, duration_s REAL, error TEXT, label TEXT, device TEXT, cpu_load REAL, mem_used_gb REAL,
  mem_total_gb REAL);
CREATE TABLE health_findings (id INTEGER PRIMARY KEY, ts REAL, window_hours REAL, rule TEXT, sev TEXT,
  text TEXT, action TEXT, beta INTEGER DEFAULT 0);
"""
KANBAN_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, created_at INTEGER,
  consecutive_failures INTEGER DEFAULT 0, last_failure_error TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT,
  created_at INTEGER);
"""


def rec(conn, **kw):
    cols = ",".join(kw)
    conn.execute(f"INSERT INTO records ({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))


class OverviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.metrics, self.kanban = root / "metrics.db", root / "kanban.db"
        m = sqlite3.connect(self.metrics)
        m.executescript(METRICS_SCHEMA)
        for i, d in enumerate([1.0, 2.0, 3.0, 40.0]):
            rec(m, ts=NOW - 60 * (i + 1), kind="api", duration_s=d, host="mini")
        rec(m, ts=NOW - 30, kind="error", event="router_request", host="mini", device="host-a",
            error="http_598: router timeout")
        rec(m, ts=NOW - 10, kind="host_stats", host="mini", cpu_load=1.5, mem_used_gb=10, mem_total_gb=32)
        rec(m, ts=NOW - 900, kind="host_stats", host="mbp", cpu_load=2.0, mem_used_gb=30, mem_total_gb=64)
        rec(m, ts=NOW - 20, kind="gateway_status", platform="ai:discord", label="fatal", error="intents off")
        rec(m, ts=NOW - 20, kind="gateway_status", platform="ai:api", label="connected")
        rec(m, ts=NOW - 5, kind="pool_status", device="host-b", model="embed")
        rec(m, ts=NOW - 5, kind="pool_status", device="host-b", model="embed:2")
        rec(m, ts=NOW - 5, kind="pool_status", device="host-c", model="coder")
        rec(m, ts=NOW - 400, kind="error", event="router_request", host="mini", device="host-a",
            error="http_598: router timeout")
        rec(m, ts=NOW - 50000, kind="pool_status", device="0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f", model="old")
        m.execute("INSERT INTO health_findings (ts, window_hours, rule, sev, text, action)"
                  " VALUES (?, 6, 'R1', 'crit', 'summarizer not loaded', 'load it')", (NOW - 100,))
        m.execute("INSERT INTO health_findings (ts, window_hours, rule, sev, text, action)"
                  " VALUES (?, 6, 'R9', 'crit', 'older snapshot', '')", (NOW - 5000,))
        m.commit()
        m.close()
        k = sqlite3.connect(self.kanban)
        k.executescript(KANBAN_SCHEMA)
        k.execute("INSERT INTO tasks VALUES ('t1', 'Ship it', 'backend-eng', 'done', ?, 0, NULL)", (int(NOW) - 600,))
        k.execute("INSERT INTO tasks VALUES ('t2', 'Flaky job', 'lm-harness', 'ready', ?, 3, 'OOM')", (int(NOW) - 600,))
        k.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t1', 'completed', ?)", (int(NOW) - 120,))
        k.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t1', 'heartbeat', ?)", (int(NOW) - 60,))
        k.commit()
        k.close()
        aliases = root / "device-aliases.json"
        aliases.write_text('{"mini": "host-b", "mbp": "host-a"}')
        self.saved = (api._METRICS_DB, api._KANBAN_DB, api._DEVICE_ALIASES)
        api._METRICS_DB, api._KANBAN_DB, api._DEVICE_ALIASES = self.metrics, self.kanban, aliases

    def tearDown(self):
        api._METRICS_DB, api._KANBAN_DB, api._DEVICE_ALIASES = self.saved
        self.tmp.cleanup()

    def test_vitals(self):
        v = api.overview(24)["vitals"]
        self.assertEqual(v["requests"], 4)
        self.assertEqual(v["errors"], 2)
        self.assertEqual(v["p95_s"], 40.0)
        self.assertEqual((v["hosts_reporting"], v["hosts_total"]), (1, 2))
        self.assertEqual(v["open_tasks"], 1)

    def test_attention_ranks_crit_first_and_uses_latest_findings(self):
        items = api.overview(24)["attention"]
        texts = [i["text"] for i in items]
        self.assertEqual({i["sev"] for i in items[:2]}, {"crit"})
        self.assertIn("summarizer not loaded", texts)
        self.assertNotIn("older snapshot", texts)
        self.assertIn("ai:discord is fatal", texts)
        self.assertFalse(any("ai:api" in t for t in texts))
        self.assertTrue(any(t.startswith("mbp has sent no host stats") for t in texts))
        self.assertIn("Flaky job failed 3 times in a row (lm-harness)", texts)
        self.assertEqual(api.attention_route(), {"items": items, "errors": {}})
        self.assertIn("host:mbp", [i["key"] for i in items])
        self.assertEqual(len({i["key"] for i in items}), len(items))

    def test_hosts_and_current_pool_only(self):
        h = api.overview(24)["hosts"]
        self.assertEqual([(x["host"], x["stale"]) for x in h["hosts"]], [("mbp", True), ("mini", False)])
        self.assertEqual(h["pool"], [{"device": "host-b", "models": [{"name": "embed", "count": 2}]},
                                     {"device": "host-c", "models": [{"name": "coder", "count": 1}]}])
        self.assertEqual(h["pool_only"], ["host-c"])

    def test_pool_only_without_aliases_lists_every_device(self):
        api._DEVICE_ALIASES = Path(self.tmp.name) / "absent.json"
        self.assertEqual(api.overview(24)["hosts"]["pool_only"], ["host-b", "host-c"])

    def test_activity_groups_repeats_and_merges_task_events(self):
        items = api.overview(24)["activity"]
        self.assertEqual([(a["kind"], a["count"]) for a in items], [("error", 2), ("task completed", 1)])
        self.assertEqual((items[0]["ts"], items[0]["first_ts"]), (NOW - 30, NOW - 400))

    def test_today_buckets_cover_since_midnight(self):
        hours = api.overview(24)["today"]
        self.assertEqual(hours[0]["ts"], api._local_midnight(NOW))
        self.assertEqual(sum(h["requests"] for h in hours) <= 4, True)

    def test_a_failing_section_leaves_the_others(self):
        m = sqlite3.connect(self.metrics)
        m.execute("DROP TABLE health_findings")
        m.commit()
        m.close()
        out = api.overview(24)
        self.assertIsNone(out["attention"])
        self.assertIn("attention", out["errors"])
        self.assertEqual(out["vitals"]["requests"], 4)

    def test_missing_kanban_keeps_metrics(self):
        api._KANBAN_DB = Path(self.tmp.name) / "absent.db"
        out = api.overview(24)
        self.assertIn("absent.db not found", out["errors"]["kanban"])
        self.assertEqual((out["vitals"]["requests"], out["vitals"]["open_tasks"]), (4, 0))
        self.assertIn("ai:discord is fatal", [i["text"] for i in out["attention"]])
        self.assertEqual([a["kind"] for a in out["activity"]], ["error"])
        self.assertFalse(api._KANBAN_DB.exists())

    def test_missing_metrics_keeps_kanban(self):
        api._METRICS_DB = Path(self.tmp.name) / "absent.db"
        out = api.overview(24)
        self.assertIn("metrics", out["errors"])
        self.assertEqual((out["vitals"]["requests"], out["vitals"]["open_tasks"]), (0, 1))
        self.assertEqual(out["hosts"], {"hosts": [], "pool": [], "pool_only": []})
        self.assertEqual([i["source"] for i in out["attention"]], ["kanban"])
        self.assertFalse(api._METRICS_DB.exists())

    def test_fresh_install_without_either_database(self):
        api._METRICS_DB = api._KANBAN_DB = Path(self.tmp.name) / "absent.db"
        out = api.overview(24)
        self.assertEqual(set(out["errors"]), {"metrics", "kanban"})
        self.assertEqual((out["attention"], out["activity"]), ([], []))
        self.assertEqual(len(out["today"]), len(api.overview(24)["today"]))
        self.assertIn("not found", api.changes(24)["errors"]["models"])

    def test_read_only(self):
        before = self.metrics.read_bytes(), self.kanban.read_bytes()
        api.overview(24)
        self.assertEqual((self.metrics.read_bytes(), self.kanban.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
