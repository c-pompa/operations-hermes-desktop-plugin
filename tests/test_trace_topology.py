import importlib.util
import json
import shutil
import subprocess
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

NOW = time.time()

SCHEMA = """
CREATE TABLE records (id INTEGER PRIMARY KEY, ts REAL, host TEXT, event TEXT, kind TEXT, platform TEXT,
  model TEXT, duration_s REAL, error TEXT, label TEXT, device TEXT, cpu_load REAL, mem_used_gb REAL,
  mem_total_gb REAL, session_id TEXT, parent_session_id TEXT, turn_id TEXT, profile TEXT, provider TEXT,
  ttft_s REAL, tps REAL, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  reasoning_tokens INTEGER, finish_reason TEXT);
CREATE TABLE health_findings (ts REAL, rule TEXT, sev TEXT, text TEXT, action TEXT);
CREATE TABLE conformance_results (ts REAL, host TEXT, rule TEXT, standard_rev TEXT, status TEXT, value TEXT,
  evidence TEXT, verified INTEGER);
"""
JS = (ROOT / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
BLAST = JS[JS.index("const STATE_COLOR"):JS.index("// The map as nodes in four tiers")]
# the layouts, with colors as their names and each element only its key
LAYOUT = (BLAST + "const TONE = new Proxy({}, { get: (_, k) => k }), jsx = (t, p) => ({ key: p?.key }), jsxs = jsx\n"
          + JS[JS.index("// The map as nodes in four tiers"):JS.index("function TopologyMap")])


def rec(conn, **kw):
    conn.execute(f"INSERT INTO records ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))


class TraceTopologyTest(unittest.TestCase):
    real_router_roles = staticmethod(api.router_roles)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "metrics.db"
        m = sqlite3.connect(self.db)
        m.executescript(SCHEMA)
        # parent session: two turns, a cloud call, a pool call, then a failed call
        rec(m, ts=NOW - 100, duration_s=5, kind="api", host="mini", session_id="p", turn_id="p:t1", platform="cli",
            profile="coder", model="glm", provider="nous", input_tokens=100, output_tokens=10, ttft_s=0.5)
        rec(m, ts=NOW - 80, duration_s=10, kind="api", host="mini", session_id="p", turn_id="p:t2", platform="cli",
            profile="coder", model="qwen", device="host-a", provider="lmstudio", input_tokens=50, output_tokens=5)
        rec(m, ts=NOW - 70, kind="error", event="api_request_error", host="mini", session_id="p", turn_id="p:t2",
            model="glm", error="403 spending limit")
        # its subagent
        rec(m, ts=NOW - 60, duration_s=3, kind="api", host="mini", session_id="c", turn_id="c:t1", platform="cli",
            profile="coder", model="glm", provider="nous")
        rec(m, ts=NOW - 55, duration_s=8, kind="subagent_stop", host="mini", session_id="c", parent_session_id="p",
            profile="coder", label="explore")
        # topology
        rec(m, ts=NOW - 10, kind="host_stats", host="mini", cpu_load=1.0, mem_used_gb=10, mem_total_gb=32)
        rec(m, ts=NOW - 20, kind="gateway_status", host="mini", platform="ai:discord", label="fatal", error="intents off")
        rec(m, ts=NOW - 20, kind="gateway_status", host="mbp", platform="default:feishu", label="stale")
        rec(m, ts=NOW - 20, kind="gateway_status", host="mbp", platform="default:api", label="connected")
        rec(m, ts=NOW - 5, kind="pool_status", device="host-a", model="qwen")
        rec(m, ts=NOW - 5, kind="pool_status", device="host-c", model="coder")
        rec(m, ts=NOW - 9 * 86400, kind="api", host="old", session_id="old", model="x", provider="gone")
        # the week before: glm on nous usually takes 5s, so the session's 20s call below is slow
        for i in range(5):
            rec(m, ts=NOW - 3600 - i, duration_s=5, kind="api", host="mini", session_id="w", model="glm", provider="nous",
                output_tokens=20)
        rec(m, ts=NOW - 30, duration_s=20, kind="api", host="mini", session_id="s", turn_id="s:t1", model="glm",
            provider="nous", ttft_s=12, output_tokens=25)
        rec(m, ts=NOW - 2, duration_s=4, kind="api", host="mini", session_id="s", turn_id="s:t2", model="glm",
            provider="nous", output_tokens=15)
        # a session that began two days ago and is still running
        rec(m, ts=NOW - 2 * 86400, duration_s=60, kind="api", host="mini", session_id="long", model="glm", provider="nous",
            platform="cron", profile="coder")
        rec(m, ts=NOW - 40, duration_s=6, kind="api", host="mini", session_id="long", model="glm", provider="nous",
            platform="desktop", profile="coder")
        for host, ts, rule, status in [("mbp", NOW - 900, "jobs", "pass"), ("mbp", NOW - 60, "jobs", "fail"),
                                       ("mbp", NOW - 60, "keys", "warn"), ("mini", NOW - 60, "jobs", "pass")]:
            m.execute("INSERT INTO conformance_results VALUES (?, ?, ?, 'r1', ?, '', '[]', 1)", (ts, host, rule, status))
        m.commit()
        m.close()
        patch = mock.patch.object(api, "_METRICS_DB", self.db)
        patch.start()
        self.addCleanup(patch.stop)
        aliases = mock.patch.object(api, "_DEVICE_ALIASES", root / "aliases.json")
        aliases.start()
        self.addCleanup(aliases.stop)
        (root / "aliases.json").write_text(json.dumps({"mbp": "host-a"}))
        roles = mock.patch.object(api, "router_roles", return_value={"url": "http://router", "error": "stub", "roles": []})
        roles.start()
        self.addCleanup(roles.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sessions_newest_first_with_subagent_links(self):
        out = api.trace(24)
        self.assertEqual(out["errors"], {})
        by = {s["session_id"]: s for s in out["sessions"]}
        self.assertEqual([s["session_id"] for s in out["sessions"]], ["s", "long", "c", "p", "w"])
        # described whole, as opening it shows it, though only its last call is in the window
        self.assertEqual((by["long"]["calls"], by["long"]["start"]), (2, NOW - 2 * 86400 - 60))
        # Begun by cron, continued in Desktop: the list and the trace both name each, in order.
        trace = api.trace_session("long")
        for x in (by["long"], trace):
            self.assertEqual((x["platform"], x["profile"]), ("cron then desktop", "coder"))
        # a failed call is still a call, so the list agrees with the trace's count
        self.assertEqual((by["p"]["calls"], by["p"]["errors"], by["p"]["subagents"]), (3, 1, 1))
        self.assertEqual(by["p"]["models"], ["glm", "qwen"])
        self.assertEqual(by["p"]["start"], NOW - 105)
        self.assertEqual(by["c"]["parent"], "p")

    def test_a_findings_cut_short_session_is_the_one_begun_by_then_active_last(self):
        m = sqlite3.connect(self.db)
        for day in (1, 2):
            rec(m, ts=NOW - 86400 * (3 - day), kind="api", session_id=f"cron_ab_2026100{day}_0800")
        m.commit()
        m.close()
        m = api._ro(self.db)
        try:
            self.assertEqual(api.find_session(m, "cron_ab_202610", NOW)["session_id"], "cron_ab_20261002_0800")
            self.assertEqual(api.find_session(m, "cron_ab_202610", NOW - 86400 * 1.5)["session_id"], "cron_ab_20261001_0800")
            self.assertIsNone(api.find_session(m, "cron_ab_202610", NOW - 86400 * 3))
            # the whole id is its own prefix
            self.assertEqual(api.find_session(m, "p", NOW)["session_id"], "p")
        finally:
            m.close()

    def test_start_limits_sessions_to_that_window(self):
        # only "w" made calls an hour ago; "long" began two days ago, so 2 days back has it alone
        self.assertEqual([x["session_id"] for x in api.trace(1, start=NOW - 3700)["sessions"]], ["w"])
        self.assertEqual([x["session_id"] for x in api.trace(1, start=NOW - 2 * 86400 - 60)["sessions"]], ["long"])

    def test_session_calls_in_order_with_turns_and_served_by(self):
        out = api.trace_session("p")
        self.assertEqual(out["errors"], {})
        calls = out["calls"]
        self.assertEqual([c["turn"] for c in calls], [1, 2, 2])
        self.assertEqual([c["served"] for c in calls], ["nous", "host-a", "unknown"])
        self.assertEqual([c["pool_device"] for c in calls], [False, True, False])
        self.assertEqual(calls[0]["ttft_s"], 0.5)
        self.assertEqual(calls[2]["error"], "403 spending limit")
        self.assertEqual([(a["session_id"], a["calls"], a["label"]) for a in out["subagents"]], [("c", 1, "explore")])
        self.assertEqual(api.trace_session("c")["parent"], "p")

    def test_topology_links_gateways_hosts_and_what_served_them(self):
        out = api.topology_route(24)
        self.assertEqual(out["errors"], {})
        hosts = {h["host"]: h for h in out["hosts"]}
        self.assertEqual(set(hosts), {"mini", "mbp"})
        self.assertFalse(hosts["mbp"]["stats"])
        self.assertEqual(hosts["mbp"]["device"], "host-a")
        self.assertEqual({(g["host"], g["state"]) for g in out["gateways"]}, {("mini", "fatal"), ("mbp", "connected")})
        self.assertEqual({(e["host"], e["served"], e["requests"]) for e in out["edges"]},
                         {("mini", "nous", 10), ("mini", "host-a", 1)})
        self.assertEqual(next(e for e in out["edges"] if e["served"] == "nous")["models"], {"glm": 10})
        self.assertEqual(hosts["mbp"]["drift"], ["jobs"])
        self.assertEqual(hosts["mini"]["drift"], [])
        served = {s["id"]: s for s in out["served"]}
        self.assertTrue(served["host-a"]["live"])
        self.assertTrue(served["host-c"]["pool_device"])
        self.assertFalse(served["nous"]["pool_device"])
        self.assertNotIn("gone", served)

    def test_timeline_replays_states_and_changes(self):
        m = sqlite3.connect(self.db)
        rec(m, ts=NOW - 3000, kind="host_stats", host="mini", cpu_load=1.0, mem_used_gb=10, mem_total_gb=32)
        rec(m, ts=NOW - 30 * 3600, kind="gateway_status", host="mini", platform="ai:discord", label="connected")
        rec(m, ts=NOW - 50, kind="harness_sample", host="mini", model="qwen")
        # a client's failed call counts on its line, as in Flow; the router's own error does not
        rec(m, ts=NOW - 40, kind="error", host="mbp", platform="subagent", profile="default", model="glm", provider="custom", error="400")
        rec(m, ts=NOW - 40, kind="error", host="mbp", model="glm", error="http_400")
        m.commit()
        m.close()
        t = api.topology_route(24)["timeline"]
        self.assertEqual([f[1:] for f in t["flow"] if f[2] == "subagent"], [["mbp", "subagent", "default", "glm", "custom", 0, 1, 1, 0]])
        self.assertEqual(t["step"], 600)
        # traffic by entry point, profile, model and what served it, summed over the buckets
        flow = {}
        for f in t["flow"]:
            if f[2] == "cli":
                flow[tuple(f[3:7])] = [a + b for a, b in zip(flow.get(tuple(f[3:7]), [0, 0, 0]), f[7:])]
        self.assertEqual(flow, {("coder", "glm", "nous", 0): [2, 0, 110], ("coder", "qwen", "host-a", 1): [1, 0, 55]})
        self.assertEqual([h[1:] for h in t["harness"]], [["mini", "qwen", 1]])
        self.assertEqual(sum(b[5] for b in t["buckets"] if b[2] == "nous"), 10)
        self.assertEqual(t["states"]["d:mbp"], [[NOW - 900, []], [NOW - 60, ["jobs"]]])
        self.assertEqual([s[1] for s in t["states"]["h:mini"]], [True, False, True])
        # the connected state is older than the window's start, so it is the baseline and fatal is a change
        self.assertEqual(t["states"]["g:mini:ai:discord"], [[NOW - 30 * 3600, "connected"], [NOW - 20, "fatal"]])
        texts = [c["text"] for c in t["changes"]]
        self.assertIn("ai:discord on mini went fatal", texts)
        self.assertIn("mbp drifted: jobs failing", texts)
        self.assertIn("mini stopped reporting", texts)
        self.assertIn("mini is reporting again", texts)
        self.assertEqual(t["changes"], sorted(t["changes"], key=lambda c: -c["ts"]))

    def test_timeline_folds_a_gateway_restart(self):
        m = sqlite3.connect(self.db)
        for ts, platform, label in ((NOW - 7300, "default:api", "connected"), (NOW - 7200, "default:api", "stopped"),
                                    (NOW - 7140, "default:api", "connected"), (NOW - 7300, "default:bot", "connected"),
                                    (NOW - 7200, "default:bot", "stopped"),
                                    (NOW - 7150, "default:bot", "stale"), (NOW - 5000, "default:bot", "stopped"),
                                    (NOW - 4000, "default:bot", "connected")):
            rec(m, ts=ts, kind="gateway_status", host="mini", platform=platform, label=label)
        m.commit()
        m.close()
        t = api.topology_route(24)["timeline"]
        got = [(c["text"], c["tone"]) for c in t["changes"] if c["node"] in ("g:mini:default:api", "g:mini:default:bot")]
        self.assertEqual(got, [("default:bot on mini connected", "good"), ("default:bot on mini stopped", "bad"),
                               ("default:api on mini restarted", "info"), ("default:bot on mini restarted and went stale", "bad")])

    def test_timeline_skips_lone_pool_snapshots(self):
        m = sqlite3.connect(self.db)
        for ts in (NOW - 9000, NOW - 4000):
            rec(m, ts=ts, kind="pool_status", device="0f0f0f0f", model="coder")
        for ts in range(int(NOW - 3000), int(NOW - 2700), 60):
            rec(m, ts=ts, kind="pool_status", device="host-c", model="coder")
        m.commit()
        m.close()
        t = api.topology_route(24)["timeline"]
        self.assertNotIn("s:0f0f0f0f", t["states"])
        texts = [c["text"] for c in t["changes"]]
        self.assertFalse(any("0f0f0f0f" in x for x in texts))
        self.assertIn("host-c left the pool", texts)

    def test_why_splits_call_time_from_waits_and_names_slow_calls(self):
        why = api.trace_session("s")["why"]
        self.assertEqual(why[0], "Model calls took 24.0s of the session's 48.0s (50%); the rest was between calls:"
                                 " tools, a person, or waiting to be picked up.")
        self.assertEqual(why[1], "The longest wait was 24.0s, before call 2.2.")
        self.assertIn("Call 1.1 took 20.0s, 4.0x the usual 5.0s for glm on nous at that output length; its first token came after 12.0s.", why)
        self.assertFalse(any("Call 2.2" in w for w in why))

    def test_typical_is_calls_like_it_from_the_week_before_it(self):
        m = sqlite3.connect(self.db)
        # Long answers, much slower, but only four: a 2000-token call has too few like it for a typical time.
        for i in range(4):
            rec(m, ts=NOW - 7200 - i, duration_s=30, kind="api", host="mini", session_id="w", model="glm",
                provider="nous", output_tokens=2000)
        # Fast calls after the first call started are not in its baseline; the second counts them and p's 5s call, but not call 1.1, which is its own session.
        for i in range(5):
            rec(m, ts=NOW - 20 + i, duration_s=1, kind="api", host="mini", session_id="w", model="glm",
                provider="nous", output_tokens=20)
        rec(m, ts=NOW - 1, duration_s=60, kind="api", host="mini", session_id="s", turn_id="s:t3", model="glm",
            provider="nous", output_tokens=2000)
        m.commit()
        m.close()
        calls = api.trace_session("s")["calls"]
        self.assertEqual([(c["usual_s"], c["usual_n"]) for c in calls], [(5, 5), (5, 11), (None, 4)])

    def test_topology_without_host_checks_has_no_drift(self):
        m = sqlite3.connect(self.db)
        m.execute("DROP TABLE conformance_results")
        m.commit()
        m.close()
        out = api.topology_route(24)
        self.assertEqual(out["errors"], {})
        self.assertEqual({h["host"]: h["drift"] for h in out["hosts"]}, {"mbp": [], "mini": []})

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_blast_radius(self):
        data = {"hosts": [{"host": "mini", "device": "Mini"}, {"host": "mbp", "device": "MBP"}],
                "gateways": [{"host": "mbp", "platform": "default:discord"}],
                "edges": [{"host": "mini", "served": "MBP", "requests": 10, "models": {"qwen": 6, "glm": 4}},
                          {"host": "mini", "served": "nous", "requests": 3, "models": {"glm": 3}},
                          {"host": "mbp", "served": "deepseek", "requests": 2, "models": {"ds": 2}}]}
        script = BLAST + f"const d = {json.dumps(data)}\n" + (
            "console.log(JSON.stringify(['h:mbp', 's:MBP', 's:nous', 'g:mbp:default:discord'].map(s => blastRadius(d, s))))")
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                        capture_output=True, text=True, check=True).stdout)
        host, device, provider, gateway = out
        self.assertEqual((host["cut"], host["alt"]), (3, 1))
        self.assertEqual([l["text"] for l in host["lines"]], [
            "Entry point default:discord goes with it", "Requests made on mbp stop (2 in the window)",
            "mini: qwen (6 requests) has no other server", "mini: glm (4 requests) also went to nous"])
        self.assertEqual((device["cut"], device["alt"]), (1, 1))
        self.assertEqual((provider["cut"], provider["alt"]), (0, 1))
        self.assertEqual(gateway["lines"], [{"text": "Requests arriving through default:discord would stop. Nothing else"
                                                     " depends on it.", "cut": True}])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_burst_marks_each_dependent(self):
        dev = lambda d, live=True: {"id": d, "pool_device": True, "live": live, "models": []}
        data = {"hosts": [{"host": "mini"}, {"host": "mbp", "stale": True}],
                "gateways": [{"host": "mini", "platform": "a", "state": "connected"},
                             {"host": "mini", "platform": "b", "state": "fatal"},
                             {"host": "mbp", "platform": "c", "state": "connected"}],
                "edges": [{"host": "mini", "served": "MBP", "requests": 5}, {"host": "mini", "served": "nous", "requests": 1},
                          {"host": "mbp", "served": "MBP", "requests": 2}],
                "served": [dev("MBP"), dev("Off", live=False), {"id": "nous", "pool_device": False, "models": []}]}
        script = LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`\n" + f"const g = topoGraph({json.dumps(data)})\n" + (
            "console.log(JSON.stringify(['s:MBP', 'h:mini', null].map(s => { const b = layoutBurst(g, s); "
            "return [b.centre, b.fate, b.head, Object.keys(b.pos).length] })))")
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                        capture_output=True, text=True, check=True).stdout)
        device, host, auto = out
        # mini still reaches nous; mbp (stale) and gateway b (fatal) were already broken; a and c follow their host
        self.assertEqual(device[1], {"h:mini": "alt", "h:mbp": "was", "g:mini:a": "alt", "g:mini:b": "was", "g:mbp:c": "cut"})
        self.assertEqual(device[2], "If MBP went down: 1 dependent cut off, 2 with another route, 2 already broken")
        self.assertEqual(host[1], {"g:mini:a": "cut", "g:mini:b": "was"})
        self.assertEqual(host[3], 5)
        # nothing selected: the node with the most dependents
        self.assertEqual(auto[0], "s:MBP")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_topology_at_replays_the_map_from_the_timeline(self):
        out = api.topology_route(24)
        out["generated_at"] = NOW
        script = BLAST + f"const d = {json.dumps(out)}\n" + (
            f"console.log(JSON.stringify([topologyAt(d, {NOW - 500}), topologyAt(d, {NOW - 15}), topologyAt(d, null) === d,"
            f" topologyAt(d, null, {NOW - 400})]))")
        before, after, now, hour = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                                       capture_output=True, text=True, check=True).stdout)
        self.assertTrue(now)
        # 500s ago: mbp's report still passed and the gateways had not reported yet
        self.assertEqual(before["gateways"], [])
        self.assertEqual({h["host"]: h["drift"] for h in before["hosts"]}, {"mbp": [], "mini": []})
        self.assertEqual(sum(e["requests"] for e in before["edges"]), sum(
            b[5] for b in out["timeline"]["buckets"] if b[0] <= NOW - 500))
        self.assertEqual({h["host"]: h["drift"] for h in after["hosts"]}["mbp"], ["jobs"])
        self.assertEqual({(g["host"], g["state"]) for g in after["gateways"]}, {("mini", "fatal"), ("mbp", "connected")})
        # from a time on: only the requests since then, the states as now
        self.assertEqual(sum(e["requests"] for e in hour["edges"]), sum(
            b[5] for b in out["timeline"]["buckets"] if b[0] >= NOW - 400))
        self.assertEqual(hour["gateways"], out["gateways"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_blast_radius_follows_router_roles(self):
        dev = lambda d, *ms: {"id": d, "pool_device": True, "live": True, "models": [{"name": x} for x in ms]}
        cand = lambda m, live=True, fit=True: {"model": m, "live": live, "fit": fit}
        data = {"hosts": [], "gateways": [], "edges": [],
                "served": [dev("MBP", "big", "small"), dev("Mini", "small", "tiny")],
                "router": {"roles": [
                    {"name": "main", "strict": False, "resolved": "big", "candidates": [cand("big"), cand("small")]},
                    {"name": "voice", "strict": True, "resolved": "big", "candidates": [cand("big")]},
                    {"name": "fit", "strict": False, "resolved": "big",
                     "candidates": [cand("big"), cand("tiny", fit=False), cand("off", live=False)]},
                    {"name": "light", "strict": False, "resolved": "small", "candidates": [cand("small")]}]}}
        script = BLAST + f"const d = {json.dumps(data)}\n" + "console.log(JSON.stringify(blastRadius(d, 's:MBP')))"
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                        capture_output=True, text=True, check=True).stdout)
        # light still has small on Mini, so it is not affected.
        self.assertEqual(out["lines"], [
            {"text": "Role main moves from big to small on Mini", "cut": False},
            {"text": "Role voice stops: it is strict and no other candidate is loaded", "cut": True},
            {"text": "Role fit falls through to its last candidate, off, which is not loaded anywhere still up",
             "cut": True}])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_flow_graph_follows_requests_from_entry_to_server(self):
        dev = lambda d, live, *ms: {"id": d, "pool_device": True, "live": live, "models": [{"name": x} for x in ms]}
        cand = lambda m: {"model": m, "live": True, "fit": True}
        data = {"generated_at": 1000, "hosts": [{"host": "mini", "stale": True}],
                "gateways": [{"host": "mini", "platform": "coder:discord", "state": "fatal"}],
                "served": [dev("MBP", True, "big"), dev("Mini", True, "small"), dev("Off", False)],
                "router": {"roles": [{"name": "hermes/main", "strict": False, "resolved": "small",
                                      "candidates": [cand("big"), cand("small")]}]},
                "timeline": {"step": 100, "states": {"g:mini:coder:discord": [[400, "fatal"]]},
                             "flow": [[900, "mini", "cli", "coder", "hermes/main", "Mini", 1, 3, 1, 50],
                                      [500, "mini", "cli", "coder", "glm", "nous", 0, 2, 0, 10]],
                             "harness": [[900, "mini", "big", 4]]}}
        script = (LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`, fmtDur = s => `${s}s`\n"
                  + f"const d = {json.dumps(data)}\n" + (
            "const g = flowGraph(d), c = flowGraph(d, { collapse: true, router: true })\n"
            "const ok = p => Object.values(p.pos).every(q => Number.isFinite(q.x) && Number.isFinite(q.y))\n"
            "console.log(JSON.stringify({ edges: Object.fromEntries(g.edges.map(e => [`${e.a}>${e.b}`, [e.kind, e.requests]])),"
            " entry: g.nodes.find(n => n.id === 'e:discord').state, nous: g.nodes.find(n => n.id === 's:nous').sub,"
            " collapsed: c.nodes.map(n => n.id).filter(id => /^[pr]:/.test(id)),"
            " placed: [g, c].flatMap(x => [layoutCircles(x), layoutSwitchboard(x)].map(p => ok(p) && x.nodes.every(n => p.pos[n.id]))),"
            " issues: topoIssues(d, 1000).map(i => i.text), hits: issueHits({ nodes: ['p:mini:'] }, 'p:mini:coder'),"
            " legend: flowLegend(0, g.edges).map(x => x.key),"
            " router: [c, flowGraph({ ...d, timeline: {} }, { router: true })].map(x => x.nodes.find(n => n.id === 'r:router').state ?? null) }))"))
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                        capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out["edges"], {
            "e:discord>p:mini:coder": ["fatal", 0], "e:cli>p:mini:coder": ["entry", 5],
            "p:mini:coder>r:hermes/main": ["alias", 3], "r:hermes/main>s:Mini": ["fallback", 3],
            "p:mini:coder>s:nous": ["cloud", 2], "x:mini>s:MBP": ["harness", 4]})
        self.assertEqual(out["entry"], "fatal")
        self.assertEqual(out["nous"], ["glm"])
        self.assertEqual(out["collapsed"], ["p:hermes", "r:router"])
        self.assertEqual(out["placed"], [True] * 4)
        self.assertEqual(out["issues"], ["coder:discord on mini fatal for 600s", "Off offline", "mini not reporting",
                                         "main on fallback to Mini, 3 requests"])
        self.assertTrue(out["hits"])
        # only the kinds of line drawn, in the legend's order
        # a line with failed calls adds the failed key
        self.assertEqual(out["legend"], ["fentry", "falias", "ffallback", "fcloud", "fharness", "ffatal", "ffailed"])
        self.assertEqual(out["router"], [None, "no requests"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_a_shared_router_line_shows_fallback_over_first_choice(self):
        cand = lambda m: {"model": m, "live": True, "fit": True}
        data = {"hosts": [], "gateways": [], "served": [{"id": "Mini", "pool_device": True, "live": True, "models": [{"name": "small"}]}],
                "router": {"roles": [{"name": "hermes/main", "strict": False, "resolved": "small", "candidates": [cand("big"), cand("small")]},
                                     {"name": "hermes/light", "strict": False, "resolved": "small", "candidates": [cand("small")]}]},
                "timeline": {}}
        script = (LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`\n"
                  + f"const d = {json.dumps(data)}\n"
                  + "const e = x => x.edges.map(e => [e.a, e.kind, e.under ?? null, e.title.split('\\n').slice(1)])\n"
                  + "const c = flowGraph(d, { router: true })\n"
                  + "console.log(JSON.stringify([e(c), e(flowGraph(d)), flowLegend(0, c.edges).map(x => x.key)]))")
        router, roles, legend = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                                          capture_output=True, text=True, check=True).stdout)
        self.assertEqual(router, [["r:router", "fallback", "primary", ["On fallback: main", "First choice: light"]]])
        self.assertEqual(sorted(roles), [["r:hermes/light", "primary", None, []], ["r:hermes/main", "fallback", None, []]])
        self.assertEqual(legend, ["fprimary", "ffallback"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_every_router_role_has_a_box_and_a_big_ring_widens_the_map(self):
        cand = lambda m: {"model": m, "live": True, "fit": True}
        role = lambda n, m: {"name": n, "strict": False, "resolved": m, "candidates": [cand(m)]}
        data = {"hosts": [], "gateways": [], "served": [{"id": "Mini", "pool_device": True, "live": True, "models": [{"name": "small"}]}],
                "router": {"roles": [role("hermes/main", "small"), role("fleet/none", "gone")]
                                    + [role(f"hermes/r{i}", "small") for i in range(9)]},
                "timeline": {"flow": [[900, "mini", "cli", p, "hermes/main", "Mini", 1, 3, 1, 50] for p in ("coder", "ops", "home")]}}
        script = (LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`\n"
                  + f"const d = {json.dumps(data)}\n"
                  + "const g = flowGraph(d), L = layoutCircles(g), x = t => g.nodes.filter(n => n.tier === t).map(n => L.pos[n.id].x)\n"
                  + "const small = layoutCircles(flowGraph(d, { router: true }))\n"
                  + "console.log(JSON.stringify([g.nodes.filter(n => n.tier === 2).length, g.nodes.find(n => n.id === 'r:fleet/none')?.color,"
                  + " L.W > 900 && Math.max(...x(1)) < Math.min(...x(2)) && Math.max(...x(2)) < Math.min(...x(3)), small.W]))")
        roles, color, clear, width = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                                               capture_output=True, text=True, check=True).stdout)
        # a role nothing in the pool serves still gets its box, in the bad colour
        self.assertEqual((roles, color), (11, "bad"))
        self.assertTrue(clear)
        self.assertEqual(width, 900)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_hovering_a_map_node_or_line_draws_its_text(self):
        data = {"hosts": [], "gateways": [], "served": [{"id": "Mini", "pool_device": True, "live": True, "models": [{"name": "small"}]}],
                "timeline": {"flow": [[900, "mini", "cli", "coder", "glm", "Mini", 0, 3, 0, 10]]}}
        tip = JS[JS.index("// The host shows no native tooltips"):JS.index("const drawerTitle")]
        script = (BLAST + "const TONE = new Proxy({}, { get: (_, k) => k }), jsx = (t, p) => ({ t, p, key: p?.key }), jsxs = jsx\n"
                  + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`, fmtDur = s => s, muted = x => x, haptic = () => {}, dragScroll = () => {}\n"
                  + "const window = { innerWidth: 1000 }\nlet st = [], k = 0\n"
                  + "const useState = v => { const i = k++; if (!(i in st)) st[i] = v; return [st[i], x => { st[i] = x }] }\n"
                  + tip + JS[JS.index("// The map as nodes in four tiers"):JS.index("function BlastRadius")]
                  + f"const g = flowGraph({json.dumps(data)})\n"
                  + "const all = (n, out = []) => { if (Array.isArray(n)) n.forEach(c => all(c, out)); else if (n?.p) { out.push(n); all(n.p.children, out) } return out }\n"
                  + "const render = () => { k = 0; return all(TopologyMap({ g, layout: 'switchboard', onSelect: () => {} })) }\n"
                  + "const text = n => typeof n === 'string' ? n : Array.isArray(n) ? n.filter(c => c != null).map(text).join('|') : n?.p ? text(n.p.children) : ''\n"
                  + "const r = render(), out = []\n"
                  + "for (const el of [r.find(n => n.p['aria-label'] === 'Mini, pool device'), r.find(n => n.t === 'path' && n.p.onMouseEnter)]) {\n"
                  + "  el.p.onMouseEnter({ clientX: 500, clientY: 100 })\n"
                  + "  const t = render().find(n => n.p.role === 'tooltip'); out.push([text(t), t.p.style.left, t.p.style.top])\n"
                  + "  el.p.onMouseLeave() }\n"
                  + "console.log(JSON.stringify([out, render().some(n => n.p.role === 'tooltip' || n.t === 'title')]))")
        out, left = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                              capture_output=True, text=True, check=True).stdout)
        # a node's name, requests and the lines under it; a line's ends and requests; under the pointer
        self.assertEqual(out, [["Mini, pool device, 3 requests|small|glm", "50%", 116],
                               ["Entry point cli to coder on mini: 3 requests", "50%", 116]])
        # leaving clears it, and no native titles are left to double it
        self.assertFalse(left)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_issues_say_why_and_what_to_do(self):
        cand = lambda m, live: {"model": m, "live": live, "fit": True}
        data = {"hosts": [{"host": "mini", "stale": True, "device": "Mini", "last_seen": 400, "drift": ["jobs"]}],
                "gateways": [{"host": "mini", "platform": "ai:discord", "state": "fatal", "error": "intents"}],
                "served": [{"id": "Mini", "pool_device": True, "live": True, "models": [{"name": "small"}]},
                           {"id": "MBP", "pool_device": True, "live": True, "models": [{"name": "tiny"}]}],
                "router": {"roles": [
                    {"name": "hermes/idle", "strict": False, "resolved": "tiny", "candidates": [cand("big", False), cand("tiny", True)]},
                    {"name": "hermes/busy", "strict": False, "resolved": "small", "candidates": [cand("big", False), cand("small", True)]},
                    {"name": "hermes/also", "strict": False, "resolved": "small", "candidates": [cand("huge", False), cand("small", True)]},
                    {"name": "fleet/dead", "strict": False, "resolved": "gone", "candidates": [cand("huge", False), cand("gone", False)]}]},
                "timeline": {"flow": [[900, "mini", "cli", "coder", "hermes/busy", "Mini", 1, 2, 0, 5]]}}
        script = (LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`, fmtDur = s => `${s}s`\n"
                  + f"console.log(JSON.stringify(topoIssues({json.dumps(data)}, 1000).map(i => [i.key, i.tone, i.cause, i.facts, i.go, i.cmd?.text ?? null, i.text])))")
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                        capture_output=True, text=True, check=True).stdout)
        by = {i[0]: i[1:] for i in out}
        # the role on fallback with no requests goes last, muted
        self.assertEqual([i[0] for i in out][-1], "r:hermes/idle")
        self.assertEqual(by["r:hermes/idle"][0], "muted")
        self.assertEqual(by["r:hermes/idle"][5], "idle on fallback to MBP, 0 requests")
        self.assertEqual(by["r:hermes/idle"][2], ["big: not loaded", "tiny: serving now"])
        self.assertIn("big, the role's first choice, is not loaded", by["r:hermes/idle"][1])
        # two roles on fallback to the same device are one issue, linking only the one with requests
        both = by["r:hermes/busy+hermes/also"]
        self.assertEqual(both[5], "2 roles on fallback to Mini (busy, also), 2 requests")
        self.assertEqual(both[3], [["See busy traffic", "flow", "model:hermes/busy"]])
        self.assertEqual(both[2], ["busy, big: not loaded", "busy, small: serving now", "also, huge: not loaded", "also, small: serving now"])
        self.assertIn("also: huge, the role's first choice, is not loaded", both[1])
        # nothing loaded anywhere: not a fallback, nothing serves it
        self.assertEqual(by["r:fleet/dead"][0], "muted")
        self.assertEqual(by["r:fleet/dead"][5], "fleet/dead: nothing in the pool serves it, 0 requests")
        self.assertEqual(by["r:fleet/dead"][2], ["huge: not loaded", "gone: not loaded"])
        self.assertEqual(by["g:mini:ai:discord"][2], ["Last error: intents"])
        self.assertIn("gateway restart", by["g:mini:ai:discord"][4])
        # its device still serves, so the host is up and the forwarder stopped
        self.assertIn("metrics forwarder has stopped", by["h:mini"][1])
        self.assertEqual(by["h:mini"][2], ["Last report 600s ago"])
        self.assertIn("ai.hermes.metricsfwd", by["h:mini"][4])
        self.assertIn("results of its last report", by["d:mini"][1])
        self.assertEqual(by["d:mini"][3], [["Open jobs", "conformance", "host:mini:jobs"]])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_a_gateway_restart_is_one_change_per_host(self):
        ch = lambda ts, node, text, tone="info": {"ts": ts, "node": node, "text": text, "tone": tone}
        changes = [ch(9, "g:mini:a:discord", "a:discord on mini restarted"),
                   ch(9, "g:mini:b:api", "b:api on mini restarted and went fatal", "bad"),
                   ch(8, "d:mbp", "mbp drifted: jobs failing", "drift"),
                   ch(7, "g:mini:a:discord", "a:discord on mini restarted"),
                   ch(6, "g:mbp:a:discord", "a:discord on mbp stopped", "bad")]
        merge = JS[JS.index("// A gateway restart takes"):JS.index("// What changed on the map")]
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", merge + f"console.log(JSON.stringify(mergeRestarts({json.dumps(changes)})))"],
                                        capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, [
            {"ts": 9, "node": "g:mini:b:api", "tone": "bad", "text": "mini gateway restarted", "list": "a:discord, b:api went fatal"},
            changes[2], changes[3], changes[4]])

    def test_router_roles_reads_status_and_reports_a_missing_router(self):
        status = {"roles": {"hermes/main": {"strict": False, "resolved": "big", "candidates_detail": [
            {"id": "big", "live": True, "fit": True}, {"id": "small", "live": False, "fit": None}]}}}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(status).encode()
        with mock.patch.dict(api.os.environ, {"MODEL_ROUTER_URL": "http://r:1/", "MODEL_ROUTER_TOKEN": "t"}), \
                mock.patch.object(api.urllib.request, "urlopen", return_value=resp) as urlopen:
            out = type(self).real_router_roles()
        req = urlopen.call_args[0][0]
        self.assertEqual((req.full_url, req.get_header("Authorization")), ("http://r:1/router/status", "Bearer t"))
        self.assertEqual(out["roles"], [{"name": "hermes/main", "strict": False, "resolved": "big", "candidates": [
            {"model": "big", "live": True, "fit": True}, {"model": "small", "live": False, "fit": True}]}])
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=api.urllib.error.URLError("refused")):
            self.assertEqual(type(self).real_router_roles()["roles"], [])

    def test_missing_database_reads_empty(self):
        with mock.patch.object(api, "_METRICS_DB", Path(self.tmp.name) / "none.db"):
            self.assertEqual(api.trace(24)["sessions"], [])
            self.assertEqual(api.trace_session("p")["calls"], [])
            out = api.topology_route(24)
            self.assertEqual((out["hosts"], out["edges"]), ([], []))
            self.assertIn("metrics", out["errors"])


if __name__ == "__main__":
    unittest.main()
