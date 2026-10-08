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
        with mock.patch.object(api, "_settings", lambda: {"conformance_project": " g/std ", "drift_action": "nonsense"}):
            out = api.setup()
        self.assertEqual((out["metrics"], out["kanban"], out["conformance_project"]), (True, False, "g/std"))
        self.assertEqual((out["drift_action"], out["drift_profile"], out["alert_target"]), ("button", "", ""))
        self.assertEqual((out["profile"], out["settings_page"]), ("default", False))

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


def host(name, *statuses, stale=False):
    return {"host": name, "stale": stale, "results": {f"r{i}": {"status": s} for i, s in enumerate(statuses)}}


class AutomaticDriftTest(unittest.TestCase):
    def test_drift_due(self):
        now = time.time()
        hosts = [host("ok", "pass"), host("bad", "pass", "fail"), host("warn", "warn"),
                 host("quiet", "fail", stale=True), host("recent", "fail")]
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
                             conformance: steps.find(x => x.id === 'conformance'), }))
"""

FRESH = {"profiles": [{"profile": "default", "state": "on"}, {"profile": "a", "state": "missing"},
                      {"profile": "b", "state": "off"}],
         "metrics": False, "kanban": True, "conformance_project": "", "alert_target": "",
         "drift_action": "automatic", "drift_profile": ""}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ChecklistTest(unittest.TestCase):
    def run_js(self, s):
        out = subprocess.run(["node", "--input-type=module", "-e", HARNESS % (SETUP, json.dumps(s))],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_fresh_install_lists_each_step_and_toasts_once(self):
        out = self.run_js(FRESH)
        self.assertEqual(out["left"], ["profiles", "metrics", "conformance", "drift"])
        self.assertEqual(out["toasts"], ["4 setup steps left"])

    def test_done_install_has_nothing_left_and_no_toast(self):
        done = {**FRESH, "profiles": [{"profile": "default", "state": "on"}, {"profile": "b", "state": "off"}],
                "metrics": True, "conformance_project": "g/std", "drift_action": "button"}
        out = self.run_js(done)
        self.assertEqual((out["left"], out["toasts"]), ([], []))

    def test_without_a_settings_form_a_step_gives_the_command_and_copies_it(self):
        step = self.run_js({**FRESH, "profile": "coder", "settings_page": False})["conformance"]
        self.assertIn("hermes -p coder config set plugins.entries.operations.settings.conformance_project", step["text"])
        self.assertEqual(step["action"]["label"], "Copy command")
        step = self.run_js({**FRESH, "profile": "default", "settings_page": True})["conformance"]
        self.assertNotIn("hermes -p", step["text"])
        self.assertEqual(step["action"]["label"], "Open plugin settings")
        step = self.run_js({**FRESH, "conformance_project": "g/std", "from_default": ["conformance_project"]})["conformance"]
        self.assertEqual(step["text"], "Checking g/std. Set in the default profile.")


if __name__ == "__main__":
    unittest.main()
