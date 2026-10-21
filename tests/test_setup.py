"""First-run setup: GET /setup, the setup checklist and toast (plugin.js), and the automatic
drift card on the gateway tick (__init__.py _drift)."""
import importlib.util
import json
import shutil
import subprocess
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "operations"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


api = load("operations_api", ROOT / "dashboard" / "plugin_api.py")
hook = load("operations_plugin", ROOT / "__init__.py")


class SetupRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        configs = {
            "default": "plugins:\n  enabled:\n    - operations\n",
            "a": "# comment\nplugins:\n  enabled: []\n",
            "b": "plugins:\n  disabled:\n    - operations\n  enabled:\n    - operations\n",
            "c": "model: x\n",
        }
        for name, text in configs.items():
            f = self.root / "config.yaml" if name == "default" else self.root / "profiles" / name / "config.yaml"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        (self.root / "metrics.db").touch()
        self.patches = [mock.patch.object(api, "get_default_hermes_root", lambda: self.root),
                        mock.patch.object(api, "get_hermes_home", lambda: self.root),
                        mock.patch.object(api, "load_config", lambda: api.safe_load((api.get_hermes_home() / "config.yaml").read_text())),
                        mock.patch.object(api.kanban_db, "list_profiles_on_disk", lambda: sorted(configs)),
                        mock.patch.object(api, "_METRICS_DB", self.root / "metrics.db"),
                        mock.patch.object(api, "_KANBAN_DB", self.root / "kanban.db"),
                        mock.patch.object(api, "_LOCAL_STANDARD", self.root / "conformance" / "conformance.yaml")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_profile_states(self):
        self.assertEqual(api.profile_states(), [
            {"profile": "a", "state": "missing"}, {"profile": "b", "state": "off"},
            {"profile": "c", "state": "missing"}, {"profile": "default", "state": "on"}])

    def test_setup_reports_sources_and_settings(self):
        with mock.patch.object(api, "_settings", lambda: {"conformance_project": " g/std ", "drift_action": "nonsense"}), \
                mock.patch.object(api, "gitlab_state", lambda: {"host": "", "has_token": False, "ok": False,
                                                               "user": None, "error": "no GitLab host set"}):
            out = api.setup()
        self.assertEqual((out["metrics"], out["kanban"], out["conformance_project"]), (True, False, "g/std"))
        self.assertEqual((out["drift_action"], out["drift_profile"], out["alert_target"]), ("button", "", ""))
        self.assertEqual((out["profile"], out["settings_page"]), ("default", False))
        # the checklist's GitLab step reads this key
        self.assertEqual(out["gitlab"]["host"], "")

    def test_gitlab_host_from_default_is_named(self):
        entries = ("plugins:\n  entries:\n    operations:\n      settings:\n"
                   "        gitlab_host: gitlab.cpompa.com\n")
        (self.root / "config.yaml").write_text(entries)
        (self.root / "profiles" / "b" / "config.yaml").write_text("plugins:\n  enabled: [operations]\n")
        with mock.patch.object(api, "get_hermes_home", lambda: self.root / "profiles" / "b"), \
                mock.patch.object(api, "gitlab_state", dict):
            out = api.setup()
        self.assertEqual(out["from_default"], ["gitlab_host"])

    def test_a_named_profile_with_its_own_copy_has_a_settings_page(self):
        home = self.root / "profiles" / "b"
        (home / "plugins" / "operations").mkdir(parents=True)
        with mock.patch.object(api, "get_hermes_home", lambda: home), mock.patch.object(api, "_settings", dict):
            out = api.setup()
        self.assertEqual((out["profile"], out["settings_page"]), ("b", True))

    def test_a_named_profile_uses_defaults_settings_for_what_it_leaves_unset(self):
        entries = "plugins:\n  entries:\n    operations:\n      settings:\n"
        (self.root / "config.yaml").write_text(entries + "        conformance_project: g/std\n        alert_target: discord\n"
                                               "        activity_summaries: true\n")
        home = self.root / "profiles" / "b"
        (home / "config.yaml").write_text(entries + "        conformance_project: ''\n        activity_summaries: false\n"
                                          "        drift_action: automatic\n")
        with mock.patch.object(api, "get_hermes_home", lambda: home):
            settings, out = api._settings(), api.setup()
        # its own false and automatic stand; the empty project falls back to default's
        self.assertEqual(settings, {"conformance_project": "g/std", "alert_target": "discord",
                                    "activity_summaries": False, "drift_action": "automatic"})
        self.assertEqual(out["from_default"], ["alert_target", "conformance_project"])
        self.assertEqual(api.setup()["from_default"], [])


def host(name, *statuses, stale=False, verified=True):
    return {"host": name, "stale": stale, "verified": verified,
            "results": {f"r{i}": {"status": s} for i, s in enumerate(statuses)}}


class AutomaticDriftTest(unittest.TestCase):
    def test_drift_due(self):
        now = time.time()
        hosts = [host("ok", "pass"), host("bad", "pass", "fail"), host("warn", "warn"),
                 host("quiet", "fail", stale=True), host("recent", "fail"),
                 host("unverified", "fail", verified=False)]
        self.assertEqual(hook.drift_due(hosts, {"warn": now - 25 * 3600, "recent": now - 3600}, now), ["bad", "warn"])

    local = Path("/nonexistent/conformance.yaml")

    def run_tick(self, settings, hosts, fix=None, store=None):
        store = {} if store is None else store
        calls = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "x.db"
        db.touch()

        def fix_drift(h, profile):
            calls.append((h, profile))
            if fix:
                raise fix

        stub = types.SimpleNamespace(_METRICS_DB=db, _KANBAN_DB=db, _LOCAL_STANDARD=self.local, _ro=lambda p: mock.Mock(),
                                     host_results=lambda m, d, now: hosts, drift_card=fix_drift, HTTPException=HTTPException)
        ctx = types.SimpleNamespace(get_config=settings.get, state=types.SimpleNamespace(get=store.get, set=store.__setitem__))
        with mock.patch.object(hook, "_api", stub):
            hook._drift(ctx)
        return calls, store

    AUTO = {"drift_action": "automatic", "drift_profile": "ops", "conformance_project": "g/std"}

    def test_files_a_card_per_failing_host_once_a_day(self):
        calls, store = self.run_tick(self.AUTO, [host("bad", "fail"), host("ok", "pass")])
        self.assertEqual(calls, [("bad", "ops")])
        self.assertIn("bad", store["drift.last"])
        store["drift.at"] = 0
        calls, _ = self.run_tick(self.AUTO, [host("bad", "fail")], store=store)
        self.assertEqual(calls, [])

    def test_off_unless_automatic_with_a_profile_and_project(self):
        for settings in ({**self.AUTO, "drift_action": "button"}, {**self.AUTO, "drift_profile": ""},
                         {**self.AUTO, "conformance_project": ""}):
            self.assertEqual(self.run_tick(settings, [host("bad", "fail")]), ([], {}))

    def test_a_started_local_standard_stands_in_for_the_project(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml") as f:
            self.local = Path(f.name)
            calls, _ = self.run_tick({**self.AUTO, "conformance_project": ""}, [host("bad", "fail")])
        self.assertEqual(calls, [("bad", "ops")])

    def test_a_failed_attempt_is_retried_and_nothing_to_fix_waits(self):
        _, store = self.run_tick(self.AUTO, [host("bad", "fail")], fix=HTTPException(502, "GitLab down"))
        self.assertEqual(store["drift.last"], {})
        _, store = self.run_tick(self.AUTO, [host("bad", "fail")], fix=HTTPException(409, "nothing failing"))
        self.assertIn("bad", store["drift.last"])


JS = (ROOT / "desktop" / "plugin.js").read_text()
SETUP = JS[JS.index("// --- first-run setup"):JS.index("function SetupChecklist")]
HARNESS = """
const ID = 'operations', PATH = '/operations'
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
const toasts = [], store = {}
const host = { notify: n => toasts.push(n), navigate: () => {} }
%s
const s = %s
const steps = setupSteps(s)
const ctx = { rest: async () => { if (s.error) throw new Error(s.error); return s },
              storage: { get: (k, d) => (k in store ? store[k] : d), set: (k, v) => { store[k] = v } } }
setupToast(ctx)
await new Promise(r => setTimeout(r, 0))
setupToast(ctx)
await new Promise(r => setTimeout(r, 0))
console.log(JSON.stringify({ left: steps.filter(x => !x.done).map(x => x.id), toasts: toasts.map(t => t.message),
                             conformance: steps.find(x => x.id === 'conformance'),
                             gitlab: steps.find(x => x.id === 'gitlab'),
                             steps: Object.fromEntries(steps.map(x => [x.id, x])) }))
"""

FRESH = {"profiles": [{"profile": "default", "state": "on"}, {"profile": "a", "state": "missing"},
                      {"profile": "b", "state": "off"}],
         "metrics": False, "kanban": True, "conformance_project": "", "alert_target": "",
         "gitlab": {"host": "", "has_token": False, "ok": False, "user": None, "error": "no GitLab host set"},
         "drift_action": "automatic", "drift_profile": ""}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ChecklistTest(unittest.TestCase):
    def run_js(self, s):
        out = subprocess.run(["node", "--input-type=module", "-e", HARNESS % (SETUP, json.dumps(s))],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_fresh_install_lists_each_step_and_toasts_once(self):
        out = self.run_js(FRESH)
        # this profile has Operations on, so that step is done: eight profiles never needing the
        # page must not leave a box nobody can tick
        self.assertEqual(out["left"], ["metrics", "gitlab", "conformance", "drift"])
        self.assertEqual(out["toasts"], ["4 setup steps left"])

    def test_the_profile_step_is_about_this_profile_and_names_the_others(self):
        on = self.run_js({**FRESH, "profile": "backend-eng", "profiles": [
            {"profile": "default", "state": "on"}, {"profile": "backend-eng", "state": "on"},
            {"profile": "writer", "state": "missing"}]})["steps"]["profiles"]
        self.assertTrue(on["done"])
        self.assertIn("On in 2 of 3 profiles", on["text"])
        self.assertIn("Not on in writer", on["text"])
        off = self.run_js({**FRESH, "profile": "backend-eng", "profiles": [
            {"profile": "backend-eng", "state": "off"}]})["steps"]["profiles"]
        self.assertFalse(off["done"])
        self.assertIn("plugins: disabled", off["text"])

    def test_done_install_has_nothing_left_and_no_toast(self):
        done = {**FRESH, "profiles": [{"profile": "default", "state": "on"}, {"profile": "b", "state": "off"}],
                "metrics": True, "conformance_project": "g/std", "drift_action": "button",
                "gitlab": {"host": "gitlab.cpompa.com", "has_token": True, "ok": True, "user": "cpompa", "error": ""}}
        out = self.run_js(done)
        self.assertEqual((out["left"], out["toasts"]), ([], []))

    def test_the_gitlab_step_says_which_half_is_missing(self):
        step = self.run_js({**FRESH, "profile": "coder", "settings_page": False,
                            "gitlab": {"host": "gitlab.x", "has_token": False, "ok": False, "error": "no GitLab token set"}})["gitlab"]
        self.assertFalse(step["done"])
        self.assertIn("set but no token is", step["text"])
        self.assertIn("hermes -p coder config set plugins.entries.operations.settings.gitlab_token", step["text"])
        self.assertEqual(step["action"]["label"], "Copy command")
        # a host answering is done, and says so by name
        step = self.run_js({**FRESH, "gitlab": {"host": "gitlab.x", "has_token": True, "ok": True, "user": "cpompa", "error": ""}})["gitlab"]
        self.assertEqual((step["done"], step["text"]), (True, "Connected to gitlab.x as cpompa."))
        self.assertIsNone(step["action"])
        # a host that is set but silent is not done, and its error is shown rather than a spinner
        step = self.run_js({**FRESH, "gitlab": {"host": "gitlab.x", "has_token": True, "ok": False, "user": None, "error": "timed out"}})["gitlab"]
        self.assertFalse(step["done"])
        self.assertIn("did not answer (timed out)", step["text"])

    def test_the_gitlab_step_offers_the_login_already_on_the_machine(self):
        # a complete source for another host, and one for the host the plugin is pointed at:
        # the matching one wins, so pressing it cannot quietly move the plugin to another GitLab
        step = self.run_js({**FRESH, "gitlab": {
            "host": "gitlab.cpompa.com", "has_token": False, "ok": False, "user": None, "error": "no GitLab token set",
            "sources": [{"id": "glab", "label": "the glab CLI", "host": "gitlab.other", "has_token": True},
                        {"id": "profile:ops", "label": "the ops profile's .env", "host": "gitlab.cpompa.com", "has_token": True}],
        }})["gitlab"]
        self.assertEqual(step["connect"]["source"], "profile:ops")
        self.assertEqual(step["connect"]["label"], "the ops profile's .env")
        self.assertEqual(step["connect"]["host"], "gitlab.cpompa.com")

    def test_the_gitlab_step_offers_no_adoption_without_a_token_anywhere(self):
        step = self.run_js({**FRESH, "gitlab": {
            "host": "gitlab.cpompa.com", "has_token": False, "ok": False, "user": None, "error": "no GitLab token set",
            "sources": [{"id": "gitlab-mr", "label": "the gitlab-mr plugin", "host": "gitlab.cpompa.com", "has_token": False}],
        }})["gitlab"]
        # the host is prefilled for typing a token, but nothing claims to be adoptable
        self.assertIsNone(step["connect"]["source"])
        self.assertEqual(step["connect"]["host"], "gitlab.cpompa.com")
        self.assertEqual(step["connect"]["sources"], 1)

    def test_without_a_settings_form_a_step_gives_the_command_and_copies_it(self):
        step = self.run_js({**FRESH, "profile": "coder", "settings_page": False})["conformance"]
        self.assertIn("hermes -p coder config set plugins.entries.operations.settings.conformance_project", step["text"])
        self.assertEqual(step["action"]["label"], "Copy command")
        step = self.run_js({**FRESH, "profile": "default", "settings_page": True})["conformance"]
        self.assertNotIn("hermes -p", step["text"])
        self.assertEqual(step["action"]["label"], "Open plugin settings")
        step = self.run_js({**FRESH, "conformance_project": "g/std", "from_default": ["conformance_project"]})["conformance"]
        self.assertEqual(step["text"], "Checking g/std. Set in the default profile.")


class GitlabStateTest(unittest.TestCase):
    """The checklist's probe of GitLab: no request without a login, one request per GITLAB_PROBE_S."""

    def setUp(self):
        self.calls = []
        patches = [mock.patch.object(api, "_gitlab_host", lambda: "gitlab.cpompa.com"),
                   mock.patch.object(api, "_gitlab_token", lambda: "tok")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        api._GITLAB_PROBE.update(at=0.0, host="", value=None)
        self.addCleanup(lambda: api._GITLAB_PROBE.update(at=0.0, host="", value=None))

    def probe(self, reply):
        def fake(path, body=None):
            self.calls.append(path)
            if isinstance(reply, Exception):
                raise reply
            return json.dumps(reply)
        return mock.patch.object(api, "_gitlab", fake)

    def test_a_missing_login_asks_nothing(self):
        with mock.patch.object(api, "_gitlab_host", lambda: ""), self.probe({"username": "x"}):
            out = api.gitlab_state()
        self.assertEqual((out["ok"], out["has_token"], out["error"]), (False, True, "no GitLab host set"))
        with mock.patch.object(api, "_gitlab_token", lambda: ""), self.probe({"username": "x"}):
            out = api.gitlab_state()
        self.assertEqual((out["ok"], out["has_token"], out["error"]), (False, False, "no GitLab token set"))
        self.assertEqual(self.calls, [])

    def test_a_login_that_answers_is_ok_and_cached_for_the_ttl(self):
        with self.probe({"username": "cpompa"}):
            first = api.gitlab_state()
            again = api.gitlab_state()
            fresh = api.gitlab_state(fresh=True)
        self.assertEqual((first["ok"], first["user"]), (True, "cpompa"))
        self.assertEqual(again["user"], "cpompa")
        self.assertEqual(len(self.calls), 2)  # the cached read, then the fresh one
        later = time.time() + api.GITLAB_PROBE_S + 1
        with mock.patch.object(api.time, "time", lambda: later), self.probe({"username": "cpompa"}):
            api.gitlab_state()
        self.assertEqual(len(self.calls), 3)

    def test_a_host_that_does_not_answer_says_why(self):
        with self.probe(urllib.error.URLError("timed out")):
            out = api.gitlab_state()
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])
        with self.probe(RuntimeError("401 Unauthorized")):
            out = api.gitlab_state(fresh=True)
        self.assertEqual(out["error"], "401 Unauthorized")
        # an answer with no username is not a working login
        with self.probe({}):
            out = api.gitlab_state(fresh=True)
        self.assertEqual((out["ok"], out["error"]), (False, "GitLab answered without a username"))


if __name__ == "__main__":
    unittest.main()
