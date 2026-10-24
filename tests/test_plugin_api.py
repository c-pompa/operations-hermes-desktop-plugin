import importlib.util
import io
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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
        api._gitlab_cache.clear()  # a GET answer cached by an earlier test must not answer this one
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
        k.execute("INSERT INTO tasks VALUES ('t1', 'Ship it', 'alpha-eng', 'done', ?, 0, NULL)", (int(NOW) - 600,))
        k.execute("INSERT INTO tasks VALUES ('t2', 'Flaky job', 'delta-harness', 'ready', ?, 3, 'OOM')", (int(NOW) - 600,))
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
        self.assertIn("Flaky job failed 3 times in a row (delta-harness)", texts)
        out = api.attention_route()
        self.assertEqual({k: out[k] for k in ("items", "errors")}, {"items": items, "errors": {}})
        # no own store here, so the payload says it served the fleet metrics.db
        self.assertEqual(out["metrics_source"], "metricsdash")
        self.assertIn("host:mbp", [i["key"] for i in items])
        self.assertEqual(len({i["key"] for i in items}), len(items))

    def test_a_health_finding_dates_from_the_first_check_of_its_run(self):
        m = sqlite3.connect(self.metrics)
        for ts in (NOW - 9000, NOW - 3000, NOW - 1000):
            m.execute("INSERT INTO health_findings (ts, window_hours, rule, sev, text, action)"
                      " VALUES (?, 6, 'R1', 'crit', 'summarizer not loaded', 'load it')", (ts,))
        m.commit()
        m.close()
        r1 = next(i for i in api.overview(24)["attention"] if i["key"] == "health:R1")
        # the check at NOW - 5000 saw only R9, so the run that is still open began at NOW - 3000
        self.assertEqual(r1["ts"], NOW - 3000)

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

    def test_a_finding_about_a_router_role_names_it(self):
        m = sqlite3.connect(self.metrics)
        m.execute("INSERT INTO health_findings (ts, window_hours, rule, sev, text, action) VALUES (?, 6, 'R2', 'crit',"
                  " 'compression role hermes/compressor has no loaded model (candidates: a)', '')", (NOW - 100,))
        m.commit()
        m.close()
        items = {i["key"]: i for i in api.overview(24)["attention"]}
        self.assertEqual(items["health:R2"]["role"], "hermes/compressor")
        self.assertNotIn("role", items["health:R1"])


ROUTER_STATUS = {
    "roles": {"hermes/compressor": {"min_context": 64000, "candidates_detail": [
        {"id": "small", "live": False, "fit": None}, {"id": "big", "live": False, "fit": None}]}},
    "loaded": {"other": 80000, "tiny": 8192}}
LMSTUDIO = {"models": [{"key": "small", "type": "llm", "loaded_instances": []},
                       {"key": "big", "type": "llm", "loaded_instances": []},
                       # the same model on a second LM Link device, loaded there
                       {"key": "big", "type": "llm", "loaded_instances": [{"id": "big"}]},
                       {"key": "other", "type": "llm", "loaded_instances": [{"id": "other"}]}]}


class ModelActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "writer.yaml").write_text("model: {default: hermes/main}\nmcp_servers:\n  roles:\n    command: python3\n    args: [/x/rolectl-mcp.py]\n"
                                          "    env: {ROLECTL_MCP_ALLOW_WRITE: '1'}\n")
        (root / "reader.yaml").write_text("mcp_servers:\n  roles: {command: /x/rolectl-mcp.py}\n")
        self.calls, self.lmstudio_up, self.router_up = [], True, True

        def fake_urlopen(req, timeout):
            url = req if isinstance(req, str) else req.full_url
            body = None if isinstance(req, str) or req.data is None else json.loads(req.data)
            self.calls.append((url, body))
            if url.endswith("/router/status"):
                if not self.router_up:
                    raise urllib.error.URLError("connection refused")
                return io.BytesIO(json.dumps(ROUTER_STATUS).encode())
            if not self.lmstudio_up:
                raise urllib.error.URLError("connection refused")
            return io.BytesIO(json.dumps(LMSTUDIO).encode())
        for p in (mock.patch.object(api.urllib.request, "urlopen", fake_urlopen),
                  mock.patch.object(api, "_ROLECTL_MCP", root / "rolectl-mcp.py"),
                  mock.patch.object(api, "_profile_config", lambda p: root / f"{p}.yaml"),
                  mock.patch.object(api.kanban_db, "list_profiles_on_disk", lambda: ["default", "reader", "writer"]),
                  mock.patch.dict(api.os.environ, {"MODEL_ROUTER_TOKEN": "t"})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_each_way_is_on_when_it_can_work(self):
        st = api.model_role("hermes/compressor")
        # big is loaded on another device already; tiny has too little context for the role
        self.assertEqual(st["lmstudio"], {"on": True, "models": ["small"], "note": ""})
        self.assertEqual(st["repoint"]["models"], ["other"])
        self.assertEqual(st["agent"]["profiles"], ["writer"])
        self.assertEqual(st["agent"]["models"], {"writer": "hermes/main"})

    def test_off_ways_say_why(self):
        self.lmstudio_up = False
        api._profile_config = lambda p: Path(self.tmp.name) / "reader.yaml"
        st = api.model_role("hermes/compressor")
        self.assertFalse(st["lmstudio"]["on"])
        self.assertIn("lms server start", st["lmstudio"]["note"])
        self.assertFalse(st["agent"]["on"])
        self.assertIn("ROLECTL_MCP_ALLOW_WRITE", st["agent"]["note"])
        with self.assertRaises(api.HTTPException) as cm:
            api.model_role("no/such-role")
        self.assertEqual(cm.exception.status_code, 404)

    def test_agents_says_whether_the_plugin_can_do_more(self):
        out = api.model_agents()
        self.assertEqual((out["router"], out["profiles"], out["rolectl_mcp"]), (True, {"writer": "hermes/main"}, ""))
        (Path(self.tmp.name) / "rolectl-mcp.py").write_text("")
        self.router_up = False
        out = api.model_agents()
        self.assertFalse(out["router"])
        self.assertTrue(out["rolectl_mcp"].endswith("rolectl-mcp.py"))

    def test_nothing_writes_to_lm_studio_or_the_router(self):
        api.model_role("hermes/compressor")
        api.model_agents()
        self.assertTrue(all(body is None for _, body in self.calls))
        self.assertFalse(any("/models/" in r.path and "GET" not in r.methods for r in api.router.routes))


if __name__ == "__main__":
    unittest.main()
