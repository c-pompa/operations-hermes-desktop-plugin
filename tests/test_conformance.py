import importlib.util
import io
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api_conformance", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

REPORT = {"standard": "desktop-fleet-baseline", "errors": [], "results": [
    {"rule": "shellcheck", "status": "enforced_by_job", "ci_job": "shellcheck"},
    {"rule": "no-stale-addresses-in-docs", "status": "warn",
     "findings": [{"file": "ARCHITECTURE.md", "line": 31, "text": "192.0.2.168"}]}]}
YAML = """hosts:
  mini: { os: macos, roles: [backend] }
  mbp: { os: macos, roles: [client] }
host_rules:
  - id: role-jobs-loaded
    title: Every job for the host's roles is loaded
    severity: blocks
    applies_to: [macos]
    check: { kind: job, from_roles: true }
    fix: >
      Run the installer
      for the missing job.
repo_rules:
  - id: shellcheck
    title: Shell scripts pass shellcheck
    severity: blocks
    check: { kind: command, ci_job: shellcheck }
    fix: Fix the reported warning.

  - id: no-stale-addresses-in-docs
    title: "No 192.0.2.x addresses in docs"
    severity: warns
    check:
      kind: absent
      title: nested, not the rule's
    why: >
      People copy addresses out of docs.
    fix: Replace with the 198.51.100.x address.
"""
PIPES = [{"status": "success", "sha": "de043d83abcdef", "web_url": "https://gitlab.example/p/1883",
          "updated_at": "2026-10-05T02:42:00.000Z"}]


NOW = time.time()
SCHEMA = """CREATE TABLE conformance_results (id INTEGER PRIMARY KEY, ts REAL, host TEXT, rule TEXT, standard TEXT,
  standard_rev TEXT, status TEXT, value TEXT, evidence TEXT, verified INTEGER)"""


class ConformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.metrics = Path(self.tmp.name) / "metrics.db"
        m = sqlite3.connect(self.metrics)
        m.execute(SCHEMA)
        for row in [(NOW - 900, "mbp", "role-jobs-loaded", "pass", "7 of 7", "[]", 1),
                    (NOW - 60, "mbp", "role-jobs-loaded", "fail", "6 of 7", '["com.x.digest: not loaded"]', 1),
                    (NOW - 60, "mbp", "installed-revision", "warn", "no install stamp", "[]", 1),
                    (NOW - 4000, "spare", "unclassified", "warn", "not in hosts:", "[]", 0)]:
            m.execute("INSERT INTO conformance_results (ts, host, rule, status, value, evidence, verified,"
                      " standard, standard_rev) VALUES (?,?,?,?,?,?,?, 's', 'de043d8')", row)
        m.commit()
        m.close()
        self.saved = (api.urllib.request.urlopen, api._gitlab_token, api._gitlab_host, api._settings, api._METRICS_DB,
                      api._LOCAL_STANDARD)
        api._METRICS_DB = self.metrics
        api._LOCAL_STANDARD = Path(self.tmp.name) / "conformance" / "conformance.yaml"
        api._gitlab_token, api._gitlab_host = (lambda: "test-token"), (lambda: "gitlab.example")
        self.settings = {"conformance_project": "group/iac"}
        api._settings = lambda: self.settings
        self.requests = []
        self.down = set()

        def fake_urlopen(req, timeout):
            url = req.full_url
            self.requests.append(url)
            source = "report" if "artifacts" in url else "rules" if "conformance.yaml" in url else "pipeline"
            if source in self.down:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            body = {"report": json.dumps(REPORT), "rules": YAML, "pipeline": json.dumps(PIPES)}[source]
            return io.BytesIO(body.encode())
        api.urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        (api.urllib.request.urlopen, api._gitlab_token, api._gitlab_host, api._settings, api._METRICS_DB,
         api._LOCAL_STANDARD) = self.saved
        self.tmp.cleanup()

    def test_not_set_up_without_a_project(self):
        self.settings = {"conformance_project": ""}
        out = api.conformance()
        self.assertEqual((out["project"], out["repo"], out["errors"]), (None, None, {}))
        self.assertEqual(self.requests, [])

    def test_report_with_rule_text_and_pipeline(self):
        out = api.conformance()
        self.assertEqual(out["errors"], {})
        self.assertEqual(out["standard"], "desktop-fleet-baseline")
        shell, docs = out["repo"]
        self.assertEqual((shell["status"], shell["severity"], shell["findings"]), ("enforced_by_job", "blocks", []))
        self.assertEqual((docs["title"], docs["severity"], docs["fix"]),
                         ("No 192.0.2.x addresses in docs", "warns", "Replace with the 198.51.100.x address."))
        self.assertEqual(docs["findings"][0]["line"], 31)
        self.assertEqual((out["pipeline"]["status"], out["pipeline"]["sha"]), ("success", "de043d83"))
        self.assertTrue(all(u.startswith("https://gitlab.example/api/v4/projects/group%2Fiac/") for u in self.requests))

    def test_hosts_show_their_newest_report_next_to_the_standard(self):
        out = api.conformance()
        self.assertEqual(out["host_rules"], [{"rule": "role-jobs-loaded", "title": "Every job for the host's roles is loaded",
                                              "severity": "blocks", "fix": "Run the installer for the missing job.",
                                              "applies_to": ["macos"]}])
        mbp, mini, spare = out["hosts"]
        self.assertEqual((mbp["host"], mbp["stale"], mbp["verified"], mbp["standard_rev"]), ("mbp", False, True, "de043d8"))
        self.assertEqual(mbp["results"]["role-jobs-loaded"],
                         {"status": "fail", "value": "6 of 7", "evidence": ["com.x.digest: not loaded"]})
        self.assertEqual(set(mbp["results"]), {"role-jobs-loaded", "installed-revision"})
        self.assertEqual((mini["host"], mini["ts"], mini["stale"], mini["results"], mini["roles"]),
                         ("mini", None, True, {}, ["backend"]))
        self.assertEqual((spare["declared"], spare["stale"], spare["verified"]), (False, True, False))

    def test_hosts_without_a_collector_table(self):
        api._METRICS_DB = Path(self.tmp.name) / "absent.db"
        out = api.conformance()
        self.assertIn("not found", out["errors"]["hosts"])
        self.assertIsNone(out["hosts"])
        self.assertEqual(len(out["repo"]), 2)
        sqlite3.connect(api._METRICS_DB).close()
        self.assertIn("no such table", api.conformance()["errors"]["hosts"])

    def test_missing_rule_text_keeps_the_report(self):
        self.down = {"rules"}
        out = api.conformance()
        self.assertIn("rules", out["errors"])
        self.assertEqual([r["status"] for r in out["repo"]], ["enforced_by_job", "warn"])
        self.assertNotIn("title", out["repo"][0])
        self.assertEqual(out["host_rules"], [])
        self.assertEqual([h["host"] for h in out["hosts"]], ["mbp", "spare"])

    def test_missing_report_is_reported(self):
        self.down = {"report"}
        out = api.conformance()
        self.assertIsNone(out["repo"])
        self.assertIn("404", out["errors"]["report"])
        self.assertEqual(out["pipeline"]["status"], "success")

    def test_a_local_standard_is_read_without_gitlab(self):
        folder = Path(self.tmp.name) / "std"
        folder.mkdir()
        (folder / "conformance.yaml").write_text("standard: mine\n" + YAML)
        self.settings = {"conformance_project": str(folder)}
        out = api.conformance()
        self.assertEqual(self.requests, [])
        self.assertEqual((out["local"], out["standard"], out["repo"], out["pipeline"], out["errors"]),
                         (True, "mine", None, None, {}))
        self.assertEqual([r["rule"] for r in out["host_rules"]], ["role-jobs-loaded"])
        self.assertEqual([h["host"] for h in out["hosts"]], ["mbp", "mini", "spare"])
        self.assertNotIn("repo_url", out)

    def test_with_no_project_a_started_local_standard_is_used(self):
        self.settings = {"conformance_project": ""}
        made = api.create_local_standard()
        self.assertEqual(made["path"], str(api._LOCAL_STANDARD))
        out = api.conformance()
        self.assertEqual((out["project"], out["local"], out["standard"], out["errors"]),
                         (str(api._LOCAL_STANDARD), True, "local", {}))
        self.assertEqual(self.requests, [])
        with self.assertRaises(api.HTTPException) as cm:
            api.create_local_standard()
        self.assertEqual(cm.exception.status_code, 409)
        # the starter takes a proposed rule like any standard
        new = api.add_rule(api._STARTER, "host_rules", {"id": "x", "title": "X"})
        self.assertEqual(api.safe_load(new)["host_rules"], [{"id": "x", "title": "X"}])


if __name__ == "__main__":
    unittest.main()
