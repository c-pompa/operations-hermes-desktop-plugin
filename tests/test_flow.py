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
ROWS = [
    # platform, profile, model, device, provider, duration_s, error, age_s
    ("desktop", "assistant", "glm", None, "nous", 2.0, None, 60),
    ("desktop", "assistant", "glm", None, "nous", 4.0, None, 120),
    ("cron", "research", "qwen", "host-a", "custom", 10.0, "timeout", 3700),
    ("cron", None, "qwen", "host-a", "custom", None, "", 3800),
    ("desktop", "assistant", "glm", None, "nous", 1.0, None, 90000),  # outside 24h
]


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        m = sqlite3.connect(self.db)
        m.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, platform TEXT, profile TEXT,"
                  " model TEXT, device TEXT, provider TEXT, duration_s REAL, error TEXT)")
        for p, prof, model, dev, prov, dur, err, age in ROWS:
            m.execute("INSERT INTO records (ts, kind, platform, profile, model, device, provider, duration_s, error)"
                      " VALUES (?, 'api', ?, ?, ?, ?, ?, ?, ?)", (NOW - age, p, prof, model, dev, prov, dur, err))
        m.execute("INSERT INTO records (ts, kind, platform) VALUES (?, 'host_stats', 'x')", (NOW - 5,))
        m.commit()
        m.close()
        self.saved = api._METRICS_DB
        api._METRICS_DB = self.db

    def tearDown(self):
        api._METRICS_DB = self.saved
        self.tmp.cleanup()

    def test_nodes_paths_and_served_by(self):
        out = api.flow(24)
        self.assertEqual((out["errors"], out["total"]), ({}, 4))
        n = {x["id"]: x for x in out["nodes"]}
        self.assertEqual([x["col"] for x in out["nodes"]], ["entry"] * 2 + ["profile"] * 3 + ["model"] * 2 + ["served"] * 2)
        self.assertEqual((n["entry:desktop"]["requests"], n["entry:desktop"]["p95_s"]), (2, 4.0))
        self.assertEqual((n["served:host-a"]["errors"], n["served:host-a"]["pool_device"]), (1, True))
        self.assertEqual((n["served:nous"]["requests"], n["served:nous"]["pool_device"]), (2, False))
        self.assertIn("profile:no profile", n)
        self.assertEqual(out["paths"], [
            {"ids": ["entry:desktop", "profile:assistant", "model:glm", "served:nous"], "requests": 2},
            {"ids": ["entry:cron", "profile:research", "model:qwen", "served:host-a"], "requests": 1},
            {"ids": ["entry:cron", "profile:no profile", "model:qwen", "served:host-a"], "requests": 1}])

    def test_start_limits_flow_to_that_window(self):
        # the two cron calls, about an hour ago; nothing after the window's end
        out = api.flow(1, start=NOW - 3900)
        self.assertEqual((out["total"], out["start"]), (2, NOW - 3900))
        self.assertEqual({x["id"] for x in out["nodes"] if x["col"] == "entry"}, {"entry:cron"})
        self.assertEqual(sum(sum(h["by"].values()) for h in out["hourly"]), 2)

    def test_failed_client_calls_count_router_errors_do_not(self):
        m = sqlite3.connect(self.db)
        m.execute("INSERT INTO records (ts, kind, platform, profile, model, provider, error)"
                  " VALUES (?, 'error', 'cron', 'research', 'qwen', 'custom', 'HTTP 598')", (NOW - 30,))
        m.execute("INSERT INTO records (ts, kind, model, error) VALUES (?, 'error', 'qwen', 'HTTP 400')", (NOW - 30,))
        m.commit()
        m.close()
        out = api.flow(24)
        n = {x["id"]: x for x in out["nodes"]}
        self.assertEqual(out["total"], 5)
        self.assertEqual((n["entry:cron"]["requests"], n["entry:cron"]["errors"]), (3, 2))
        self.assertEqual((n["served:custom"]["requests"], n["served:custom"]["errors"]), (1, 1))
        self.assertNotIn("entry:unknown", n)

    def test_errors_grouped_by_what_failed_and_where(self):
        m = sqlite3.connect(self.db)
        m.execute("ALTER TABLE records ADD COLUMN host TEXT")
        m.execute("ALTER TABLE records ADD COLUMN event TEXT")
        m.execute("ALTER TABLE records ADD COLUMN session_id TEXT")
        for age, sid in ((40, "s1"), (30, "s2"), (20, "s2")):
            m.execute("INSERT INTO records (ts, kind, host, platform, profile, model, provider, event, error, session_id)"
                      " VALUES (?, 'error', 'mbp', 'subagent', 'default', 'glm', 'custom', 'api_request_error', 'bad model', ?)",
                      (NOW - age, sid))
        m.execute("INSERT INTO records (ts, kind, host, model, event, error) VALUES (?, 'error', 'mbp', 'glm', 'router_request', 'http_400')",
                  (NOW - 10,))
        m.execute("INSERT INTO records (ts, kind, host, event, error) VALUES (?, 'error', 'mbp', 'router_request', 'old')", (NOW - 7200,))
        m.commit()
        m.close()
        out = api.errors_route(1, start=NOW - 3600)
        self.assertEqual([(g["count"], g["platform"], g["served"], g["error"], g["sessions"]) for g in out["groups"]],
                         [(3, "subagent", "custom", "bad model", ["s2", "s1"]), (1, None, None, "http_400", [])])
        g = out["groups"][0]
        self.assertEqual((g["first"], g["last"], g["host"], g["profile"], g["model"]), (NOW - 40, NOW - 20, "mbp", "default", "glm"))

    def test_hourly_by_served(self):
        hourly = api.flow(24)["hourly"]
        self.assertEqual(hourly[0]["ts"] % 3600, 0)
        self.assertEqual(sum(h["by"].get("served:nous", 0) for h in hourly), 2)
        self.assertEqual(sum(sum(h["by"].values()) for h in hourly), 4)

    def test_hourly_by_path_adds_up_to_each_path(self):
        out = api.flow(24)
        for i, p in enumerate(out["paths"]):
            self.assertEqual(sum(h["paths"].get(i, 0) for h in out["hourly"]), p["requests"])

    def test_hourly_failed_by_path(self):
        out = api.flow(24)
        # only the cron call that timed out failed; an empty error string is not a failure
        self.assertEqual([sum(h["failed"].get(i, 0) for h in out["hourly"]) for i in range(len(out["paths"]))], [0, 1, 0])

    def test_older_database_without_profile_or_provider(self):
        m = sqlite3.connect(self.db)
        m.execute("ALTER TABLE records DROP COLUMN profile")
        m.execute("ALTER TABLE records DROP COLUMN provider")
        m.commit()
        m.close()
        ids = {x["id"] for x in api.flow(24)["nodes"]}
        self.assertEqual(ids & {"profile:no profile", "served:unknown", "served:host-a"},
                         {"profile:no profile", "served:unknown", "served:host-a"})

    def test_missing_database_reads_empty(self):
        api._METRICS_DB = Path(self.tmp.name) / "absent.db"
        out = api.flow(6)
        self.assertIn("not found", out["errors"]["metrics"])
        self.assertEqual((out["total"], out["nodes"], out["paths"]), (0, [], []))
        self.assertEqual(len(out["hourly"]), 7)
        self.assertFalse(api._METRICS_DB.exists())

    def test_read_only(self):
        before = self.db.read_bytes()
        api.flow(168)
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
