"""A failed model call, told properly: what it is, and the one thing to do about it.

The failure text metrics.db keeps is classified by the app's own classifier
(``cron.scheduler_failure_copy`` over ``agent.error_classifier``), so the tests that need a reason
class are skipped where that import does not work — an install without it keeps the raw error text,
which one test pins down by making the import fail. Each remedy is real ``hermes`` commands
prefilled from the record, and for a cron failure the failing job's id, recovered from its run
session (``cron_<job_id>_<timestamp>``).

The JS half renders the same payload (plugin.js errorRows) through a slice of the page under node,
the way tests/test_today_strip.py does.
"""
import builtins
import importlib.util
import json
import shutil
import sqlite3
import subprocess
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

# The shape the incident's record had: a cron job's model call, sent to the provider named in it,
# refused with the provider's own 402 and its own words for an empty account.
BILLING_402 = ("APIStatusError: Error code: 402 - {'error': {'message': 'Insufficient Balance "
               "(request_id: 0f0f0f0f-1111-2222-3333-444444444444)'}}")
AUTH_401 = "APIStatusError: Error code: 401 - {'error': {'message': 'invalid api key'}}"
NO_MODEL_404 = "NotFoundError: Error code: 404 - model not found"

METRICS_SCHEMA = """
CREATE TABLE records (id INTEGER PRIMARY KEY, ts REAL, host TEXT, kind TEXT, platform TEXT, profile TEXT,
  model TEXT, provider TEXT, event TEXT, error TEXT, session_id TEXT, device TEXT, duration_s REAL);
"""


def core_classifier_available():
    try:
        from cron.scheduler_failure_copy import classify_cron_failure_reason  # noqa: F401
    except ImportError:
        return False
    return True


CORE = core_classifier_available()


def without_import(*names):
    """builtins.__import__ that refuses the named modules, for the tests that pin down degradation."""
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name in names or any(name.startswith(n + ".") for n in names):
            raise ImportError(f"no {name} in this Hermes")
        return real(name, *a, **kw)
    return fake


class FailedCallDiagnosisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.metrics = Path(self.tmp.name) / "metrics.db"
        self.kanban = Path(self.tmp.name) / "kanban.db"
        m = sqlite3.connect(self.metrics)
        m.executescript(METRICS_SCHEMA)
        m.commit()
        m.close()
        self.saved = (api._METRICS_DB, api._KANBAN_DB)
        api._METRICS_DB, api._KANBAN_DB = self.metrics, self.kanban

    def tearDown(self):
        api._METRICS_DB, api._KANBAN_DB = self.saved
        self.tmp.cleanup()

    def rec(self, **kw):
        m = sqlite3.connect(self.metrics)
        kw.setdefault("kind", "error")
        kw.setdefault("ts", NOW - 60)
        m.execute(f"INSERT INTO records ({','.join(kw)}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))
        m.commit()
        m.close()

    def one_group(self, **kw):
        """The groups one record produced, over the last hour."""
        self.rec(**kw)
        out = api.errors_route(1, start=NOW - 3600)
        self.assertEqual(out["errors"], {})
        return out["groups"]

    def test_a_billing_402_on_a_cron_job_says_which_account_and_which_commands(self):
        groups = self.one_group(host="mini", platform="cron", profile="default", model="deepseek-flash",
                                provider="deepseek", event="api_request_error", error=BILLING_402,
                                session_id="cron_a1b2c3d4e5f6_20261023_133423")
        self.assertEqual(len(groups), 1)
        g = groups[0]
        # the row still shows the provider's own words, unchanged
        self.assertEqual(g["error"], BILLING_402)
        self.assertEqual((g["provider"], g["model"]), ("deepseek", "deepseek-flash"))
        d = g["diagnosis"]
        self.assertEqual(d["reason"], "billing")
        self.assertEqual(d["job_id"], "a1b2c3d4e5f6")
        self.assertTrue(d["cron"])
        # the cause names the provider account that is empty, and says it is not another pot of money:
        # the whole confusion of the incident was a funded account somewhere else
        self.assertIn("deepseek", d["cause"])
        self.assertIn("the deepseek account itself is out of credit", d["cause"])
        self.assertIn("not the balance of any other account or portal you fund", d["cause"])
        self.assertIn("calls sent to deepseek", d["cause"])
        # and the remedy is the commands an operator would type, with the job id filled in
        self.assertIn("Top up the deepseek account", d["remedy"])
        self.assertIn("`hermes cron edit a1b2c3d4e5f6 --provider <name>`", d["remedy"])
        self.assertIn("`hermes cron run a1b2c3d4e5f6`", d["remedy"])
        self.assertEqual(d["commands"], ["hermes cron edit a1b2c3d4e5f6 --provider <name>",
                                         "hermes cron run a1b2c3d4e5f6"])

    def test_each_provider_gets_its_own_account_named(self):
        for i, provider in enumerate(("deepseek", "nous")):
            self.rec(ts=NOW - 60 * (i + 1), host="mini", platform="cron", profile="default", model="m",
                     provider=provider, event="api_request_error", error=BILLING_402,
                     session_id=f"cron_a1b2c3d4e5f{i}_20261023_133423")
        groups = api.errors_route(1, start=NOW - 3600)["groups"]
        self.assertEqual([g["diagnosis"]["provider"] for g in groups], ["deepseek", "nous"])
        self.assertIn("the deepseek account itself is out of credit", groups[0]["diagnosis"]["cause"])
        self.assertIn("the nous account itself is out of credit", groups[1]["diagnosis"]["cause"])
        self.assertEqual(groups[1]["diagnosis"]["job_id"], "a1b2c3d4e5f1")

    def test_an_auth_failure_offers_the_sign_in_and_a_retry(self):
        groups = self.one_group(host="mini", platform="cron", profile="default", model="deepseek-flash",
                                provider="deepseek", event="api_request_error", error=AUTH_401,
                                session_id="cron_a1b2c3d4e5f6_20261023_090023")
        d = groups[0]["diagnosis"]
        self.assertEqual(d["reason"], "auth")
        self.assertIn("Sign in again for deepseek", d["remedy"])
        self.assertIn("`hermes auth add deepseek`", d["remedy"])
        self.assertIn("`hermes cron run a1b2c3d4e5f6`", d["remedy"])
        self.assertEqual(d["commands"], ["hermes auth add deepseek", "hermes cron run a1b2c3d4e5f6"])

    def test_a_sign_in_names_the_profile_that_failed(self):
        groups = self.one_group(host="mini", platform="cron", profile="alpha-eng", model="glm",
                                provider="deepseek", event="api_request_error", error=AUTH_401,
                                session_id="cron_a1b2c3d4e5f6_20261023_090023")
        self.assertEqual(groups[0]["diagnosis"]["commands"][0], "hermes -p alpha-eng auth add deepseek")

    def test_a_missing_model_points_at_the_job_s_model(self):
        groups = self.one_group(host="mini", platform="cron", profile="default", model="glm-9",
                                provider="nous", event="api_request_error", error=NO_MODEL_404,
                                session_id="cron_a1b2c3d4e5f6_20261023_090023")
        d = groups[0]["diagnosis"]
        self.assertEqual(d["reason"], "model_not_found")
        self.assertIn("the model the call uses was not found", d["cause"])
        self.assertEqual(d["commands"], ["hermes cron edit a1b2c3d4e5f6 --model <name>"])

    def test_a_desktop_or_cli_failure_gets_the_provider_level_remedy(self):
        billing = self.one_group(host="mbp", platform="desktop", profile="default", model="glm",
                                 provider="nous", event="api_request_error", error=BILLING_402)[0]
        d = billing["diagnosis"]
        self.assertFalse(d["cron"])
        self.assertNotIn("job_id", d)
        self.assertIn("Top up the nous account, or send these calls somewhere else", d["remedy"])
        self.assertEqual(d["commands"], [])
        self.assertNotIn("cron edit", d["remedy"])

    def test_a_cron_failure_without_a_recoverable_job_says_where_the_id_is(self):
        # a unified-runtime run (local-…) names no job: the remedy keeps the placeholder and says where to look
        groups = self.one_group(host="mini", platform="cron", profile="default", model="deepseek-flash",
                                provider="deepseek", event="api_request_error", error=BILLING_402,
                                session_id="local-0f0f0f0f111122223333444455556666777788889999aaaabbbbccccdddd")
        d = groups[0]["diagnosis"]
        self.assertEqual(d["reason"], "billing")
        self.assertTrue(d["cron"])
        self.assertNotIn("job_id", d)
        self.assertIn("`hermes cron run <job_id>`", d["remedy"])
        self.assertIn("cron_<job_id>_<timestamp>", d["remedy"])

    def test_a_failure_nothing_can_name_keeps_the_raw_text_alone(self):
        groups = self.one_group(host="mbp", platform="desktop", profile="default", model="glm",
                                provider="nous", event="api_request_error", error="")
        self.assertNotIn("diagnosis", groups[0])

    @unittest.skipUnless(CORE, "this Hermes has no core error classifier")
    def test_no_core_classifier_keeps_the_group_as_the_error_text_it_was(self):
        groups = self.one_group(host="mini", platform="cron", profile="default", model="deepseek-flash",
                                provider="deepseek", event="api_request_error", error=BILLING_402,
                                session_id="cron_a1b2c3d4e5f6_20261023_133423")
        self.assertIn("diagnosis", groups[0])
        with mock.patch.object(builtins, "__import__", without_import("cron.scheduler_failure_copy",
                                                                     "agent.error_classifier")):
            out = api.errors_route(1, start=NOW - 3600)
        self.assertEqual(out["errors"], {})
        g = out["groups"][0]
        self.assertNotIn("diagnosis", g)
        # unchanged otherwise: the same grouping, the same text, the same sessions
        self.assertEqual((g["count"], g["error"], g["sessions"], g["served"]),
                         (1, BILLING_402, ["cron_a1b2c3d4e5f6_20261023_133423"], "no answer (deepseek)"))

    @unittest.skipUnless(CORE, "this Hermes has no core error classifier")
    def test_without_the_core_s_job_id_reader_the_remedy_says_so(self):
        groups = self.one_group(host="mini", platform="cron", profile="default", model="deepseek-flash",
                                provider="deepseek", event="api_request_error", error=BILLING_402,
                                session_id="cron_a1b2c3d4e5f6_20261023_133423")
        self.assertEqual(groups[0]["diagnosis"]["job_id"], "a1b2c3d4e5f6")
        with mock.patch.object(builtins, "__import__", without_import("hermes_state_keys")):
            d = api.errors_route(1, start=NOW - 3600)["groups"][0]["diagnosis"]
        self.assertEqual(d["reason"], "billing")
        self.assertNotIn("job_id", d)
        self.assertEqual(d["commands"], ["hermes cron edit <job_id> --provider <name>",
                                         "hermes cron run <job_id>"])

    @unittest.skipUnless(CORE, "this Hermes has no core error classifier")
    def test_a_classifier_that_trips_leaves_the_failure_list_standing(self):
        self.rec(host="mini", platform="cron", profile="default", model="deepseek-flash", provider="deepseek",
                 event="api_request_error", error=BILLING_402,
                 session_id="cron_a1b2c3d4e5f6_20261023_133423")
        with mock.patch("cron.scheduler_failure_copy.classify_cron_failure_reason",
                        side_effect=RuntimeError("boom")):
            out = api.errors_route(1, start=NOW - 3600)
        self.assertEqual(out["errors"], {})
        self.assertEqual(out["groups"][0]["error"], BILLING_402)
        self.assertNotIn("diagnosis", out["groups"][0])


JS = (ROOT / "plugins" / "operations" / "desktop" / "plugin.js").read_text()

DIAGNOSIS_CAUSE = ("deepseek refused the call: the deepseek account itself is out of credit — the balance deepseek"
                   " holds for API calls, which is not the balance of any other account or portal you fund. Money"
                   " funded elsewhere does not pay for calls sent to deepseek.")
DIAGNOSIS_REMEDY = ("Top up the deepseek account, or repoint the job: `hermes cron edit a1b2c3d4e5f6"
                    " --provider <name>`; then `hermes cron run a1b2c3d4e5f6` to retry.")

ERRORS = """
const TONE = { bad: 'bad' }, MONO = 'mono', DIM = 'dim', Skeleton = 'Skeleton', GoLink = 'GoLink', KindBox = 'KindBox'
const jsx = (t, p) => ({ t, p }), jsxs = jsx, drawerText = (k, t) => t, drawerPart = (k, label, body) => ({ t: 'part', p: { label, children: body } })
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`, fmtClock = ts => `t${ts}`
const DIAGNOSIS = { reason: 'billing', provider: 'deepseek', cron: true, job_id: 'a1b2c3d4e5f6',
  cause: %s, remedy: %s,
  commands: ['hermes cron edit a1b2c3d4e5f6 --provider <name>', 'hermes cron run a1b2c3d4e5f6'] }
const rows = errorRows([
  { count: 14, host: 'mini', platform: 'cron', profile: 'default', model: 'deepseek-flash', provider: 'deepseek',
    served: 'no answer (deepseek)', no_answer: true, event: 'api_request_error',
    error: 'APIStatusError: Error code: 402 - Insufficient Balance',
    first: 100, last: 200, sessions: ['cron_a1b2c3d4e5f6_20261023_133423'], diagnosis: DIAGNOSIS },
  { count: 11, host: 'mbp', platform: null, profile: null, model: 'glm', served: null, event: 'router_request',
    error: null, first: 15, last: 15, sessions: [] }], { hours: 1, start: 0, flow: true })
const all = (n, out = []) => { if (Array.isArray(n)) n.forEach(c => all(c, out)); else if (n?.p) { out.push(n); all(n.p.children, out) } return out }
const text = n => typeof n === 'string' ? n : Array.isArray(n) ? n.filter(c => c != null).map(text).join('|') : n?.p ? text(n.p.children) : ''
const muted = rows.map(r => all(r).filter(n => n.p?.style?.color === DIM).map(n => text(n.p.children)))
const fixes = rows.map(r => all(r).find(n => n.t === FixThis).p.a).map(a => [a.text, a.action, alertBrief(a, 'Stop if stuck.')])
console.log(JSON.stringify({ texts: rows.map(text), muted, fixes }))
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FailedCallRowTest(unittest.TestCase):
    def test_the_row_shows_the_cause_and_the_remedy_muted_under_the_error(self):
        fn = JS[JS.index("// The hour's failed calls grouped"):JS.index("// What the tag on an attention card says")]
        fn += JS[JS.index("// A group of failed calls (errorAlert)"):JS.index("// Two ways to get an alert fixed")]
        harness = ERRORS % (json.dumps(DIAGNOSIS_CAUSE), json.dumps(DIAGNOSIS_REMEDY))
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", fn + harness],
                                        capture_output=True, text=True, check=True).stdout)
        # the provider's own error text first, then the cause and the remedy, each its own muted line
        self.assertEqual(out["texts"][0], "14 errors|cron calls on mini|t100 to t200|"
                                          "APIStatusError: Error code: 402 - Insufficient Balance|"
                                          "profile default · model deepseek-flash · no answer (deepseek) · 1 session|"
                                          + DIAGNOSIS_CAUSE + "|" + DIAGNOSIS_REMEDY + "|"
                                          "Open the session in Trace|Show the model in Flow|Show default on mini on the map|")
        # both muted blocks: the row's facts, then the diagnosis under them. The second row (a failure
        # the backend could not name) keeps its facts and gets no diagnosis line at all.
        self.assertEqual(out["muted"], [["profile default · model deepseek-flash · no answer (deepseek) · 1 session",
                                         DIAGNOSIS_CAUSE + "|" + DIAGNOSIS_REMEDY], ["model glm · no session"]])
        # a failure the backend could not name keeps the row it always had: no line invented for it
        self.assertEqual(out["texts"][1], "11 errors|router on mbp|t15|router_request failed|model glm · no session|"
                                          "Show the host on the map|")
        # and Fix this carries the diagnosis into the chat or card the failure is handed to
        text, action, brief = out["fixes"][0]
        self.assertEqual(text, "14 failed model calls, cron calls on mini: APIStatusError: Error code: 402"
                               " - Insufficient Balance")
        self.assertTrue(action.endswith(DIAGNOSIS_CAUSE + " " + DIAGNOSIS_REMEDY))
        self.assertIn("Seen: profile default; model deepseek-flash; no answer (deepseek);"
                      " deepseek refused the call:", brief)
        self.assertIn("repoint the job: `hermes cron edit a1b2c3d4e5f6 --provider <name>`", brief)
        self.assertIn("Sessions, newest first: cron_a1b2c3d4e5f6_20261023_133423.", brief)
        self.assertFalse(any("refused the call" in b for b in out["fixes"][1]))


if __name__ == "__main__":
    unittest.main()
