"""Self-sufficiency (docs/self-sufficiency.md): Operations' own metrics store and collector.

Nothing here touches a real install: the own store, the collector's offsets, the heartbeat file,
every source and the session DB are pointed into a temp directory, and the fleet metrics.db is a
fixture the collector must never write to.

Proven to fail against the pre-change plugin (origin/dev): none of the collector, the own store,
the retention setting, the storage report or the heartbeat fallback existed there.
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "operations"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


api = load("operations_api", PLUGIN / "dashboard" / "plugin_api.py")
hook = load("operations_plugin", PLUGIN / "__init__.py")

NOW = time.time()
# Log lines carry their own timestamps, and the collector prunes by event time: fixtures must be
# inside the retention window or the row is dropped the moment it arrives.
LOG_TS = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 120)) + ",123"
ISO_TS = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(NOW - 120))

FLEET_SCHEMA = """
CREATE TABLE records (id INTEGER PRIMARY KEY, ts REAL, host TEXT, event TEXT, kind TEXT, platform TEXT,
  model TEXT, duration_s REAL, error TEXT, label TEXT, device TEXT, cpu_load REAL, mem_used_gb REAL,
  mem_total_gb REAL);
CREATE TABLE health_findings (id INTEGER PRIMARY KEY, ts REAL, window_hours REAL, rule TEXT, sev TEXT,
  text TEXT, action TEXT);
CREATE TABLE conformance_results (id INTEGER PRIMARY KEY, ts REAL, host TEXT, rule TEXT, standard TEXT,
  standard_rev TEXT, status TEXT, value TEXT, evidence TEXT, verified INTEGER);
"""


def add_rec(conn, **kw):
    cols = ", ".join(kw)
    conn.execute(f"INSERT INTO records ({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))
    conn.commit()


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def make_fleet_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(FLEET_SCHEMA)
    add_rec(conn, ts=NOW - 60, kind="api", duration_s=9.0, host="fleet-host")
    conn.close()
    return path


def append_text(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


class SelfSufficiencyTest(unittest.TestCase):
    def setUp(self):
        api._gitlab_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.own = self.root / "metrics" / "operations.db"
        self.fleet = self.root / "metrics" / "metrics.db"
        self.state = self.root / "plugin-data" / "operations" / "collector-state.json"
        self.heartbeat = self.root / "plugin-data" / "operations" / "heartbeat.json"
        self.requests = self.root / "metrics" / "requests.jsonl"
        self.moa = self.root / "metrics" / "moa-refs.jsonl"
        self.agent_log = self.root / "logs" / "agent.log"
        self.router = self.root / "logs" / "model-router-requests.jsonl"
        self.clientgate = self.root / "logs" / "clientgate-usage.jsonl"
        self.settings = {"metrics_retention_days": 30}
        self.patches = [
            mock.patch.object(api, "_OPERATIONS_DB", self.own),
            mock.patch.object(api, "_COLLECT_STATE", self.state),
            mock.patch.object(api, "_HEARTBEAT", self.heartbeat),
            mock.patch.object(api, "_REQUESTS_JSONL", self.requests),
            mock.patch.object(api, "_MOA_REFS_JSONL", self.moa),
            mock.patch.object(api, "_AGENT_LOG", self.agent_log),
            mock.patch.object(api, "_ROUTER_JSONL", self.router),
            mock.patch.object(api, "_CLIENTGATE_JSONL", self.clientgate),
            mock.patch.object(api, "_SESSION_DB", self.root / "state.db"),
            mock.patch.object(api, "_METRICS_DB", self.fleet),
            mock.patch.object(api, "_KANBAN_DB", self.root / "kanban.db"),
            mock.patch.object(api, "_LOCAL_STANDARD", self.root / "conformance" / "conformance.yaml"),
            mock.patch.object(api, "_LMS_BIN", str(self.root / "no-such-lms")),
            mock.patch.object(api, "HOST_LABEL", "fixture-host"),
            mock.patch.object(api, "_METRICS_ROOT", self.root),
            mock.patch.object(api, "_COLLECT_ENABLED", True),
            mock.patch.object(api, "_LAST_ROUTE_COLLECT", 0.0),
            mock.patch.object(api, "_DASH_PROBE", {"at": 0.0, "value": None, "fails": 0, "since": None,
                                                   "last_error": None, "recovered_at": None}),
            mock.patch.object(api, "_settings", lambda: dict(self.settings)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def own_conn(self):
        return api.ensure_store()

    # --- 1. the own store, its schema and its version ------------------------------------

    def test_own_store_lives_at_the_metrics_root_with_a_version_key(self):
        self.assertEqual(api.operations_db(), self.own)
        conn = self.own_conn()
        try:
            self.assertEqual(api.store_schema_version(conn), api.STORE_VERSION)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
                             str(api.STORE_VERSION))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"records", "conformance_results", "health_findings"} <= tables)
            self.assertEqual(api.store_journal_mode(conn), "wal")
            # every column the routes read exists in the own schema
            have = {r[1] for r in conn.execute("PRAGMA table_info(records)")}
            self.assertTrue({"ts", "host", "kind", "session_id", "parent_session_id", "profile", "provider",
                             "input_tokens", "output_tokens", "api_request_id", "turn_id", "device",
                             "cpu_load", "mem_used_gb", "mem_total_gb"} <= have)
        finally:
            conn.close()

    def test_benchmark_and_harness_data_are_not_collected(self):
        # the fleet DB's bulk (benchmark_samples/harness_samples) has no place in this store
        conn = self.own_conn()
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("benchmark_samples", tables)
            self.assertNotIn("harness_samples", tables)
            self.assertNotIn("pool_status_samples", tables)
        finally:
            conn.close()
        for name in ("benchmark_sample", "harness_sample"):
            row = api.normalize_record({"event": name, "ts": NOW}, "fixture-host")
            self.assertNotEqual(row["kind"], "host_stats")

    # --- 2/3. which store a read serves, and the source it names --------------------------

    def test_own_store_is_the_default_source(self):
        conn = self.own_conn()
        add_rec(conn, ts=NOW - 60, kind="api", duration_s=1.0, host="fixture-host")
        conn.close()
        make_fleet_db(self.fleet)  # the fallback is present and must lose
        out = api.overview(24)
        self.assertEqual(out["metrics_source"], "operations")
        self.assertEqual(out["vitals"]["requests"], 1)
        self.assertEqual(out["vitals"]["p95_s"], 1.0)
        self.assertNotIn("metrics", out["errors"])

    def test_the_fleet_db_is_a_fallback_only_when_the_own_store_is_empty(self):
        make_fleet_db(self.fleet)
        self.assertFalse(self.own.exists())
        out = api.overview(24)
        self.assertEqual(out["metrics_source"], "metricsdash")
        self.assertEqual(out["vitals"]["requests"], 1)
        self.assertEqual(out["vitals"]["p95_s"], 9.0)
        # the fleet DB is only ever opened read-only
        self.assertRegex(str(api.metrics_source()[0]), r"metrics\.db$")

    def test_an_empty_own_store_does_not_hide_the_fallback(self):
        self.own_conn().close()  # a store that exists but holds nothing yet
        make_fleet_db(self.fleet)
        self.assertEqual(api.overview(24)["metrics_source"], "metricsdash")

    def test_no_store_at_all_reports_none_not_empty(self):
        out = api.overview(24)
        self.assertEqual(out["metrics_source"], "none")
        self.assertIn("not found", out["errors"]["metrics"])
        self.assertEqual(out["vitals"]["requests"], 0)

    # --- 4. the collector -----------------------------------------------------------------

    def test_collector_tails_the_fixture_sources_into_the_own_store(self):
        # the rotating logs seed at EOF the first time they are seen, so they exist (empty) before
        # the first pass, exactly as they do on a running host
        for f in (self.requests, self.moa, self.agent_log, self.router, self.clientgate):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("")
        api.collect_once(kind="route")  # seeds the three seed-at-EOF files at offset 0
        write_jsonl(self.requests, [
            {"event": "post_api_request", "ts": NOW - 30, "model": "m", "duration_s": 2.0,
             "usage": {"input_tokens": 10, "output_tokens": 4}, "session_id": "s1"},
            {"event": "api_request_error", "ts": NOW - 20, "model": "m", "error_type": "http_402",
             "error_message": "no credit", "session_id": "s1"},
        ])
        write_jsonl(self.moa, [{"ts": NOW - 10, "session_id": "s2", "preset": "p",
                                "references": [{"label": "r1", "model": "a", "provider": "x"}],
                                "aggregator": {"label": "agg", "model": "b", "provider": "x"}}])
        write_jsonl(self.router, [{"ts": ISO_TS, "role": "hermes/main",
                                   "model": "m", "status": 200, "duration_s": 1.5,
                                   "usage": {"prompt_tokens": 3, "completion_tokens": 5}}])
        write_jsonl(self.clientgate, [{"ts": ISO_TS, "client": "alpha-eng",
                                       "model": "m", "status": 500, "duration_ms": 40,
                                       "prompt_tokens": 1, "completion_tokens": 1}])
        append_text(self.agent_log, [
            f"{LOG_TS} agent.conversation_loop: Turn ended: reason=guardrail_halt"
            " model=hermes/main api_calls=21/150 budget=21/150 tool_turns=77 session=s1",
            f"{LOG_TS} agent.tool_executor: Tool read_file returned error (0.00s):"
            " {\"error\": \"Blocked read_file\", \"guardrail\": {\"action\": \"block\", \"code\": \"sensitive_file\"}}",
            f"{LOG_TS} agent.other: an ordinary line that is not a guardrail event",
        ])
        out = api.collect_once(kind="route")
        self.assertGreaterEqual(out["records"], 7)
        self.assertEqual(out["errors"], {})
        conn = api._ro(self.own)
        try:
            kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM records GROUP BY kind"))
        finally:
            conn.close()
        for kind in ("api", "error", "moa_ref", "moa_agg", "router_request", "guardrail_halt", "guardrail_block"):
            self.assertIn(kind, kinds, kinds)
        # the clientgate failure (HTTP 500) lands in the error lane
        self.assertEqual(kinds["error"], 2)
        rows = api._ro(self.own)
        try:
            halted = rows.execute("SELECT label, error FROM records WHERE kind='guardrail_halt'").fetchone()
            blocked = rows.execute("SELECT label, error FROM records WHERE kind='guardrail_block'").fetchone()
        finally:
            rows.close()
        self.assertEqual(halted["label"], None)
        self.assertIn("21 api calls", halted["error"])
        self.assertEqual(blocked["label"], "read_file")
        self.assertIn("sensitive_file", blocked["error"])

    def test_a_route_poll_collects_lazily(self):
        write_jsonl(self.requests, [{"event": "post_api_request", "ts": NOW - 5, "model": "m",
                                     "duration_s": 1.0, "usage": {"input_tokens": 1, "output_tokens": 1}}])
        self.assertFalse(self.own.exists())
        out = api.overview(24)
        self.assertTrue(self.own.exists())
        self.assertEqual(out["metrics_source"], "operations")
        self.assertEqual(out["vitals"]["requests"], 1)

    def test_offsets_commit_so_a_restart_neither_drops_nor_duplicates(self):
        write_jsonl(self.requests, [{"event": "post_api_request", "ts": NOW - 5, "model": "m",
                                     "duration_s": 1.0, "usage": {"input_tokens": 1, "output_tokens": 1}}])
        first = api.collect_once(kind="route")
        self.assertEqual(first["records"], 1)
        second = api.collect_once(kind="route")
        self.assertEqual(second["records"], 0)  # nothing new, nothing re-read
        # a new line is picked up exactly once
        write_jsonl(self.requests, [{"event": "post_api_request", "ts": NOW - 4, "model": "m",
                                     "duration_s": 1.0, "usage": {"input_tokens": 1, "output_tokens": 1}}])
        self.assertEqual(api.collect_once(kind="route")["records"], 1)
        # "restart": the offsets are on disk, so a fresh pass adds nothing
        self.assertEqual(api.collect_once(kind="route")["records"], 0)
        # truncation/rotation restarts the offset, and the dedup key collapses the re-read
        self.requests.write_text("")
        write_jsonl(self.requests, [{"event": "post_api_request", "ts": NOW - 5, "model": "m",
                                     "duration_s": 1.0, "usage": {"input_tokens": 1, "output_tokens": 1}}])
        api.collect_once(kind="route")
        conn = api._ro(self.own)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
        finally:
            conn.close()

    # --- 5. retention ---------------------------------------------------------------------

    def seed_ages(self):
        conn = self.own_conn()
        for days in (10, 45, 75, 200):
            add_rec(conn, ts=NOW - days * 86400, kind="api", duration_s=1.0, host="fixture-host")
        conn.close()

    def kept_ages(self):
        conn = api._ro(self.own)
        try:
            return sorted(round((NOW - ts) / 86400) for (ts,) in conn.execute("SELECT ts FROM records"))
        finally:
            conn.close()

    def test_retention_prunes_on_the_collector_pass_for_each_setting(self):
        for days, expected in ((30, [10]), (60, [10, 45]), (90, [10, 45, 75]), (0, [10, 45, 75, 200])):
            with self.subTest(days=days):
                if self.own.exists():
                    self.own.unlink()
                self.settings["metrics_retention_days"] = days
                self.seed_ages()
                out = api.collect_once(kind="route")
                self.assertEqual(self.kept_ages(), expected)
                self.assertEqual(out["pruned"].get("records", 0), 4 - len(expected))

    def test_forever_prunes_nothing(self):
        self.settings["metrics_retention_days"] = 0
        self.seed_ages()
        out = api.collect_once(kind="route")
        self.assertEqual(out["pruned"]["records"], 0)
        self.assertEqual(self.kept_ages(), [10, 45, 75, 200])

    def test_an_unknown_retention_falls_back_to_the_default(self):
        for bad in (7, "nonsense", None):
            self.settings["metrics_retention_days"] = bad
            self.assertEqual(api.retention_days(), api.RETENTION_DEFAULT)

    def test_conformance_results_keep_the_fourteen_day_policy(self):
        conn = self.own_conn()
        conn.execute("INSERT INTO conformance_results (ts, host, rule, status, verified)"
                     " VALUES (?, 'fixture-host', 'r', 'pass', 1)", (NOW - 20 * 86400,))
        conn.execute("INSERT INTO conformance_results (ts, host, rule, status, verified)"
                     " VALUES (?, 'fixture-host', 'r', 'pass', 1)", (NOW - 2 * 86400,))
        conn.commit()
        conn.close()
        api.collect_once(kind="route")
        conn = api._ro(self.own)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conformance_results").fetchone()[0], 1)
        finally:
            conn.close()

    # --- 6. the storage report ------------------------------------------------------------

    def test_storage_report_math_and_projections(self):
        self.settings["metrics_retention_days"] = 60
        conn = self.own_conn()
        add_rec(conn, ts=NOW - 3600, kind="api", duration_s=1.0, host="fixture-host")
        conn.execute("INSERT INTO conformance_results (ts, host, rule, status, verified)"
                     " VALUES (?, 'fixture-host', 'r', 'pass', 1)", (NOW - 3600,))
        conn.commit()
        conn.close()
        rep = api.storage_report(NOW)
        self.assertEqual(rep["retention_days"], 60)
        self.assertEqual(rep["mb_per_day"], api.MB_PER_DAY)
        self.assertEqual(rep["metrics_source"], "operations")
        self.assertTrue(rep["exists"])
        self.assertEqual(rep["tables"][0]["table"], "records")
        self.assertEqual(rep["tables"][0]["rows"], 1)
        self.assertEqual([t["table"] for t in rep["tables"]],
                         ["records", "conformance_results", "health_findings"])
        # the projections are the measured mean per day plus the tabular tables as they are
        sizes = api.table_sizes(self.own)
        other_mb = round((sum(sizes.values()) - sizes.get("records", 0)) / (1024 * 1024), 1)
        got = {p["days"]: p for p in rep["projections"]}
        self.assertEqual(sorted(got), [0, 30, 60, 90])
        self.assertEqual(got[30]["projected_mb"], round(30 * api.MB_PER_DAY + other_mb, 1))
        self.assertEqual(got[60]["projected_mb"], round(60 * api.MB_PER_DAY + other_mb, 1))
        self.assertEqual(got[90]["projected_mb"], round(90 * api.MB_PER_DAY + other_mb, 1))
        self.assertEqual(got[0]["label"], "forever")
        self.assertEqual(got[0]["monthly_mb"], round(api.MB_PER_DAY * 30, 1))
        self.assertEqual([o["days"] for o in rep["options"]], [30, 60, 90, 0])
        self.assertEqual([o["label"] for o in rep["options"]], ["30 days", "60 days", "90 days", "forever"])
        self.assertIn("heartbeat", rep)
        self.assertIn("metrics_dash", rep)
        self.assertIn("collector", rep)

    def test_setting_the_retention_goes_through_the_plugin_settings_writer(self):
        written = {}
        fake = types.ModuleType("hermes_cli.plugins_settings")
        fake.save_plugin_settings = lambda ident, directory, values: written.update(values)
        with mock.patch.dict(sys.modules, {"hermes_cli.plugins_settings": fake}):
            out = api.set_retention(body={"days": 90})
        self.assertEqual(written, {"metrics_retention_days": 90})
        self.assertEqual(out["wrote"], ["metrics_retention_days"])
        for bad in (7, "x", None):
            with mock.patch.dict(sys.modules, {"hermes_cli.plugins_settings": fake}):
                with self.assertRaises(api.HTTPException) as cm:
                    api.set_retention(body={"days": bad})
            self.assertEqual(cm.exception.status_code, 400)

    def test_storage_and_setup_carry_the_report(self):
        out = api.storage()
        self.assertEqual(out["retention_days"], 30)
        self.assertIn("collector", out)
        s = api.setup()
        self.assertIn("storage", s)
        self.assertEqual(s["storage"]["retention_days"], 30)
        self.assertEqual(s["metrics_source"], "none")
        self.assertIn("heartbeat", s)
        self.assertIn("metrics_dash", s)

    # --- 7. degrading honestly, and never writing the fleet DB ----------------------------

    def test_a_failing_source_is_counted_surfaced_and_announced_on_recovery(self):
        (self.root / "metrics").mkdir(parents=True, exist_ok=True)
        good = self.root / "metrics" / "requests-good.jsonl"
        good.write_text("")
        broken = self.root / "metrics" / "requests-broken"
        broken.mkdir()  # a directory where a file should be: reading it raises
        with mock.patch.object(api, "_REQUESTS_JSONL", broken):
            for n in (1, 2, 3):
                out = api.collect_once(kind="tick")
                entry = out["sources"]["requests"]
                self.assertEqual(entry["fails"], n)
                self.assertIn("IsADirectoryError", entry["last_error"])
                self.assertIsNotNone(entry["since"])
            # the third failure is reported (not just counted) and the source backs off
            self.assertIn("requests", out["errors"])
            backed_off = api.collect_once(kind="tick")
            self.assertEqual(backed_off["sources"]["requests"]["fails"], 3)
            self.assertIn("backing off until", backed_off["errors"]["requests"])
            # it shows up where a person looks: the store's own health findings
            items = {i["key"]: i for i in api.attention_route()["items"]}
            self.assertIn("health:OPS2-requests", items)
            self.assertIn("failed 3 times in a row", items["health:OPS2-requests"]["text"])
        # after the backoff expires the source is read again, and the recovery is announced
        with mock.patch.object(api, "_REQUESTS_JSONL", good):
            recovered = api.collect_once(kind="tick", now=NOW + api.SOURCE_BACKOFF_S + 60)
        self.assertEqual(recovered["sources"]["requests"]["fails"], 0)
        self.assertIsNotNone(recovered["sources"]["requests"]["recovered_at"])
        self.assertNotIn("requests", recovered["errors"])
        self.assertNotIn("health:OPS2-requests", {i["key"] for i in api.attention_route()["items"]})

    def test_a_single_hiccup_is_retried_not_reported(self):
        (self.root / "metrics").mkdir(parents=True, exist_ok=True)
        broken = self.root / "metrics" / "requests-broken"
        broken.mkdir()
        with mock.patch.object(api, "_REQUESTS_JSONL", broken):
            out = api.collect_once(kind="tick")
        self.assertEqual(out["sources"]["requests"]["fails"], 1)
        self.assertNotIn("requests", out["errors"])

    def test_an_unwritable_store_is_reported(self):
        path, _ = api.metrics_source()
        with mock.patch.object(api, "ensure_store", side_effect=sqlite3.OperationalError("readonly database")):
            out = api.collect_once(kind="tick")
        self.assertIn("readonly database", out["errors"]["store"])
        self.assertEqual(out["sources"]["store"]["fails"], 1)

    def test_nothing_ever_writes_the_fleet_metrics_db(self):
        make_fleet_db(self.fleet)
        before = (self.fleet.read_bytes(), self.fleet.stat().st_mtime_ns)
        write_jsonl(self.requests, [{"event": "post_api_request", "ts": NOW - 5, "model": "m",
                                     "duration_s": 1.0, "usage": {"input_tokens": 1, "output_tokens": 1}}])
        opened = []
        real_connect = sqlite3.connect

        def spy(target, *a, **kw):
            opened.append((str(target), kw.get("uri", False)))
            return real_connect(target, *a, **kw)

        with mock.patch.object(api.sqlite3, "connect", spy):
            api.collect_once(kind="tick")
            api.overview(24)
            api.conformance()
            api.storage()
        # no connection to the fleet DB was made writable (mode=ro only), and the file is untouched
        for target, uri in opened:
            if str(self.fleet) in target:
                self.assertTrue(uri and "mode=ro" in target, target)
        self.assertEqual((self.fleet.read_bytes(), self.fleet.stat().st_mtime_ns), before)
        self.assertNotIn(str(self.fleet), [str(api.operations_db())])

    # --- 8. recognition -------------------------------------------------------------------

    def test_the_metrics_dashboard_probe_is_cached_and_honest(self):
        class Resp:
            status = 200

            def __init__(self, body):
                self._body = body

            def read(self):
                return json.dumps(self._body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = []

        def up(url, timeout=None):
            calls.append(url)
            return Resp({"revision": "abc1234", "records": 10, "retention_days": 30})

        with mock.patch.object(api.urllib.request, "urlopen", up):
            first = api.metrics_dash_probe()
            self.assertTrue(first["present"])
            self.assertEqual(first["revision"], "abc1234")
            self.assertEqual(first["capabilities"]["records"], 10)
            self.assertEqual(first["link"], api._METRICS_DASH_URL + "/")
            api.metrics_dash_probe()  # cached
            self.assertEqual(len(calls), 1)
            api.metrics_dash_probe(force=True)
            self.assertEqual(len(calls), 2)

        def down(url, timeout=None):
            raise api.urllib.error.URLError("connection refused")

        with mock.patch.object(api.urllib.request, "urlopen", down):
            failed = api.metrics_dash_probe(force=True)
            self.assertFalse(failed["present"])
            self.assertIsNone(failed["link"])
            self.assertEqual(failed["fails"], 1)
            self.assertIsNotNone(failed["since"])
            self.assertIn("URLError", failed["last_error"])
            second = api.metrics_dash_probe(force=True)
            self.assertEqual(second["fails"], 2)
            self.assertEqual(second["since"], failed["since"])  # the run's start, not the latest try
        with mock.patch.object(api.urllib.request, "urlopen", up):
            back = api.metrics_dash_probe(force=True)
        self.assertTrue(back["present"])
        self.assertEqual(back["fails"], 0)
        self.assertIsNone(back["last_error"])
        self.assertIsNotNone(back["recovered_at"])

    # --- 9. heartbeat ---------------------------------------------------------------------

    def test_heartbeat_state_reads_what_the_gateway_wrote(self):
        self.assertEqual(api.heartbeat_state(NOW)["mode"], "none")
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.write_text(json.dumps({"last_tick": NOW, "last_sweep": NOW, "by": "tick"}))
        self.assertEqual(api.heartbeat_state(NOW)["mode"], "tick")
        self.heartbeat.write_text(json.dumps({"last_tick": NOW - 200, "last_sweep": NOW, "by": "own-timer"}))
        hb = api.heartbeat_state(NOW)
        self.assertEqual(hb["mode"], "own-timer")
        self.assertTrue(hb["stale"])
        self.heartbeat.write_text(json.dumps({"last_tick": NOW - 200, "last_sweep": NOW - 1, "by": "tick"}))
        self.assertEqual(api.heartbeat_state(NOW)["mode"], "own-timer")

    def test_the_heartbeat_is_reported_on_the_pages_that_serve_metrics(self):
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.write_text(json.dumps({"last_tick": NOW - 200, "last_sweep": NOW, "by": "own-timer"}))
        self.assertEqual(api.overview(24)["heartbeat"]["mode"], "own-timer")
        self.assertEqual(api.attention_route()["heartbeat"]["mode"], "own-timer")


class HeartbeatFallbackTest(unittest.TestCase):
    """The gateway side (__init__.py): tick, own-timer fallback, and what the page is told."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = {}
        self.ctx = types.SimpleNamespace(state=types.SimpleNamespace(get=self.store.get,
                                                                    set=self.store.__setitem__),
                                         get_config=lambda k, d=None: d)
        self.heartbeat = self.root / "plugin-data" / "operations" / "heartbeat.json"
        self.quiet = [mock.patch.object(hook, "HEARTBEAT_FILE", self.heartbeat),
                      mock.patch.object(api, "_HEARTBEAT", self.heartbeat),
                      mock.patch.object(hook, "_alert", lambda ctx, dry_run=False, **k: None),
                      mock.patch.object(hook, "_drift", lambda ctx, dry_run=False, **k: None),
                      mock.patch.object(api, "_COLLECT_ENABLED", False)]
        self.swept = []
        self.quiet.append(mock.patch.object(hook, "_collect", lambda **k: self.swept.append("collect")))
        for p in self.quiet:
            p.start()

    def tearDown(self):
        for p in self.quiet:
            p.stop()
        self.tmp.cleanup()

    def test_two_missed_intervals_is_what_triggers_the_fallback(self):
        self.assertEqual(hook.TICK_INTERVAL_S, 60)
        now = 1_000_000.0
        self.assertTrue(hook.tick_due(None, now))
        self.assertTrue(hook.tick_due(now - 120, now))
        self.assertTrue(hook.tick_due(now - 200, now))
        self.assertFalse(hook.tick_due(now - 119, now))
        self.assertFalse(hook.tick_due(now, now))

    def test_a_dispatcher_tick_records_itself_and_sweeps(self):
        by = hook._on_heartbeat_tick(self.ctx)
        self.assertEqual(by, "tick")
        self.assertEqual(self.swept, ["collect"])
        self.assertIsNotNone(self.store["heartbeat.last_tick"])
        data = json.loads(self.heartbeat.read_text())
        self.assertEqual(data["by"], "tick")
        self.assertIsNotNone(data["last_sweep"])

    def test_the_own_timer_takes_over_only_when_the_tick_goes_quiet(self):
        # a fresh tick: the fallback must not double-sweep
        self.store["heartbeat.last_tick"] = time.time()
        self.assertIsNone(hook._heartbeat(self.ctx))
        self.assertEqual(self.swept, [])
        self.assertFalse(self.heartbeat.exists())
        # the tick is now stale: the fallback runs and says so
        self.store["heartbeat.last_tick"] = time.time() - hook.TICK_INTERVAL_S * 2 - 1
        self.assertEqual(hook._heartbeat(self.ctx), "own-timer")
        self.assertEqual(self.swept, ["collect"])
        data = json.loads(self.heartbeat.read_text())
        self.assertEqual(data["by"], "own-timer")
        self.assertEqual(api.heartbeat_state()["mode"], "own-timer")
        # and a host that never saw a tick at all
        self.store.clear()
        self.heartbeat.unlink()
        self.assertEqual(hook._heartbeat(self.ctx), "own-timer")
        self.assertEqual(api.heartbeat_state()["mode"], "own-timer")

    def test_the_own_timer_is_not_started_in_a_test_context(self):
        # register() must not enable collection or start a thread for a stand-in ctx
        ctx = mock.Mock()
        with mock.patch.object(hook, "start_own_timer") as timer, \
                mock.patch.object(hook, "_dashboard_api") as dash:
            hook.register(ctx)
        timer.assert_not_called()
        dash.assert_not_called()
        ctx.register_hook.assert_any_call("on_kanban_dispatch_tick", hook._on_tick)


if __name__ == "__main__":
    unittest.main()