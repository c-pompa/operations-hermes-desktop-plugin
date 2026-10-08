import base64
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import connect_closing
from hermes_yaml import safe_load

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

STANDARD = """\
# The standard.
version: 1
standard: test-baseline

hosts:
  mbp:  { os: macos, roles: [client] }

host_rules:
  # applies_to names roles or an OS.
  - id: tunnel-answers
    title: Tunnel answers
    severity: blocks
    applies_to: [client]
    check: { kind: port, url: "http://127.0.0.1:9119/api/status", timeout_s: 2 }
    why: Desktop reaches the dashboard only through it.
    fix: Re-run macos/install.sh.

# Checked in CI.
repo_rules:
  - id: shellcheck
    title: Shell scripts pass shellcheck
    severity: blocks
    check: { kind: command, run: "shellcheck <every *.sh>", ci_job: shellcheck }
    why: Scripts run unattended.
    fix: Fix what shellcheck reports.
"""

RULE = {"id": "disk-space", "title": "Disk has room", "severity": "warns", "applies_to": ["macos"],
        "check": "{ kind: command, run: 'df -h /', ci_job: disk }", "why": "A full disk stops the collector.",
        "fix": "Free space: true"}


class AddRuleTest(unittest.TestCase):
    def test_appends_to_each_list_and_keeps_the_file_as_it_was(self):
        rule = dict(RULE, check={"kind": "command", "run": "df -h /", "ci_job": "disk"})
        for kind in ("host_rules", "repo_rules"):
            new = api.add_rule(STANDARD, kind, rule)
            self.assertEqual(safe_load(new)[kind][-1], rule)
            removed = [l for l in STANDARD.splitlines() if l not in new.splitlines()]
            self.assertEqual(removed, [])
        new = api.add_rule(STANDARD, "host_rules", rule)
        # The comment that introduces repo_rules stays with it, after the new rule.
        self.assertLess(new.index("disk-space"), new.index("# Checked in CI."))
        self.assertIn('    check: { kind: command, run: "df -h /", ci_job: disk }\n', new)
        self.assertIn('    fix: "Free space: true"\n', new)

    def test_flow_list_is_refused(self):
        with self.assertRaises(HTTPException):
            api.add_rule("host_rules: []\n", "host_rules", {"id": "x"})


class ProposeRuleTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        settings = mock.patch.object(api, "_settings", return_value={"conformance_project": "grp/std"})
        settings.start()
        self.addCleanup(settings.stop)

    def gitlab(self, path, body=None):
        self.calls.append((path, body))
        if path.endswith("files/conformance.yaml?ref=main"):
            return json.dumps({"content": base64.b64encode(STANDARD.encode()).decode(), "commit_id": "abc123"})
        if path.endswith("merge_requests"):
            return json.dumps({"web_url": "https://gl/grp/std/-/merge_requests/7"})
        return "{}"

    def propose(self, kind="host_rules", rule=RULE):
        with mock.patch.object(api, "_gitlab", side_effect=self.gitlab):
            return api.propose_rule(kind, dict(rule))

    def test_opens_an_mr_from_a_branch_off_the_main_it_read(self):
        out = self.propose()
        self.assertEqual(out["mr_url"], "https://gl/grp/std/-/merge_requests/7")
        (_, commit), (_, mr) = [c for c in self.calls if c[1] is not None]
        self.assertEqual(commit["start_sha"], "abc123")
        self.assertEqual(commit["branch"], mr["source_branch"])
        self.assertEqual(mr["target_branch"], "main")
        content = safe_load(commit["actions"][0]["content"])
        self.assertEqual(content["host_rules"][-1]["check"], {"kind": "command", "run": "df -h /", "ci_job": "disk"})

    def test_bad_input_never_reaches_gitlab_writes(self):
        cases = [("host_rules", dict(RULE, id="tunnel-answers"), 409),
                 ("host_rules", dict(RULE, id="Bad Id"), 400),
                 ("host_rules", dict(RULE, why=""), 400),
                 ("host_rules", dict(RULE, check="just text"), 400),
                 ("other", RULE, 400)]
        for kind, rule, code in cases:
            with self.assertRaises(HTTPException) as cm:
                self.propose(kind, rule)
            self.assertEqual(cm.exception.status_code, code, rule)
        self.assertFalse([c for c in self.calls if c[1] is not None])

    def test_a_local_standard_gets_the_rule_written_into_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conformance.yaml"
            path.write_text(STANDARD)
            with mock.patch.object(api, "_settings", return_value={"conformance_project": str(path)}):
                out = self.propose()
            self.assertEqual(out, {"ok": True, "path": str(path)})
            self.assertEqual(safe_load(path.read_text())["host_rules"][-1]["id"], "disk-space")
        self.assertEqual(self.calls, [])

    def test_a_rule_that_does_not_read_back_is_refused(self):
        with mock.patch.object(api, "add_rule", return_value=STANDARD):
            with self.assertRaises(HTTPException) as cm:
                self.propose()
        self.assertEqual(cm.exception.status_code, 400)
        self.assertFalse([c for c in self.calls if c[1] is not None])


class BusinessModeOffTest(unittest.TestCase):
    """Business mode is switched off in this version: single mode, and a rule cannot go to an org."""

    def test_single_and_refused(self):
        api._org_cache.update(at=0.0, value=None)
        self.assertEqual(api.org_mode(fresh=True), {"mode": "single", "org_id": None, "org_name": None,
                                                    "role": None, "reason": "business mode is not available yet"})
        with self.assertRaises(HTTPException) as cm:
            api.propose_to_org("host_rules", {}, "", {})
        self.assertEqual(cm.exception.status_code, 409)


# Hermes dropped org skill sync upstream; org_mode then catches the ImportError and reports single mode
@unittest.skipUnless(importlib.util.find_spec("tools.skills_sync_client"), "this Hermes has no org skill sync")
class OrgTest(unittest.TestCase):
    """Business mode through Hermes' own sync client, faked here: nothing reaches a sync server."""

    def setUp(self):
        from tools import skills_sync_client as ssc
        from tools import skills_sync_client_org as sso
        self.ssc = ssc
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.skills = Path(self.tmp.name) / "skills"
        self.proposed = []
        self.answer = {"ok": True, "proposal_pending": True, "proposal_id": "p1"}
        self.identity = {"org_id": "org_1", "org_role": "MEMBER", "claims": {"org_name": "Example Org"}}
        for target, attr, value in (
                (sso, "resolve_org_identity", lambda: self.identity),
                (sso, "propose_skill", lambda name, message=None: self.proposed.append((name, message)) or self.answer),
                (ssc, "sync_feature_enabled", lambda: True),
                (ssc, "resolve_sync_base_url", lambda: "https://sync.example"),
                (ssc, "_skills_dir", lambda: self.skills),
                (ssc, "_skill_rel_path", lambda name: None),
                (api, "_settings", lambda: {"conformance_project": "grp/std"})):
            p = mock.patch.object(target, attr, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(api, "BUSINESS_MODE", True)
        p.start()
        self.addCleanup(p.stop)
        api._org_cache.update(at=0.0, value=None)
        self.addCleanup(api._org_cache.update, at=0.0, value=None)

    def gitlab(self, path, body=None):
        if body is not None:
            raise AssertionError("business mode wrote to GitLab")
        return json.dumps({"content": base64.b64encode(STANDARD.encode()).decode(), "commit_id": "abc123"})

    def propose(self, rule=RULE):
        with mock.patch.object(api, "_gitlab", side_effect=self.gitlab):
            return api.propose_rule("host_rules", dict(rule), "org")

    def test_mode_needs_an_org_and_sync(self):
        self.assertEqual(api.org_mode(fresh=True), {"mode": "business", "org_id": "org_1", "org_name": "Example Org",
                                                    "role": "MEMBER", "reason": ""})
        with mock.patch.object(self.ssc, "sync_feature_enabled", return_value=False):
            off = api.org_mode(fresh=True)
        self.assertEqual((off["mode"], off["org_id"]), ("single", "org_1"))
        self.assertIn("sync is off", off["reason"])
        from tools import skills_sync_client_org as sso
        with mock.patch.object(sso, "resolve_org_identity", side_effect=self.ssc.SyncInertError("not in an org")):
            self.assertEqual(api.org_mode(fresh=True)["mode"], "single")

    def test_new_skill_is_seeded_from_the_repo_and_proposed(self):
        out = self.propose()
        self.assertEqual((out["status"], out["proposal_id"], out["skill"], out["org"]),
                         ("proposal_pending", "p1", "test-baseline", "Example Org"))
        skill = self.skills / "standards" / "test-baseline"
        std = safe_load((skill / "conformance.yaml").read_text())
        self.assertEqual([r["id"] for r in std["host_rules"]], ["tunnel-answers", "disk-space"])
        md = (skill / "SKILL.md").read_text()
        self.assertTrue(md.startswith("---\nname: test-baseline\n"))
        self.assertIn("### Disk has room (disk-space)", md)
        self.assertEqual(self.proposed, [("test-baseline", "Add host rule disk-space: Disk has room")])

    def test_existing_copy_is_edited_keeping_hand_written_text(self):
        skill = self.skills / "_org" / "org_1" / "standards" / "test-baseline"
        skill.mkdir(parents=True)
        (skill / "conformance.yaml").write_text(STANDARD)
        (skill / "SKILL.md").write_text(f"---\nname: test-baseline\n---\nOwners: ops.\n\n{api.SOP_START}\nold\n{api.SOP_END}\nEnd.\n")
        self.answer = {"ok": True, "merged": True}
        with mock.patch.object(self.ssc, "_skill_rel_path", return_value=Path("_org/org_1/standards/test-baseline")):
            self.assertEqual(self.propose()["status"], "published")
            md = (skill / "SKILL.md").read_text()
            for text in ("Owners: ops.", "End.", "### Tunnel answers (tunnel-answers)", "### Disk has room (disk-space)"):
                self.assertIn(text, md)
            self.assertNotIn("old", md)
            with self.assertRaises(HTTPException) as cm:
                self.propose()
        self.assertEqual(cm.exception.status_code, 409)

    def test_refused_outside_business_mode_and_a_failed_send_keeps_the_edit(self):
        with mock.patch.object(self.ssc, "sync_feature_enabled", return_value=False):
            with self.assertRaises(HTTPException) as cm:
                self.propose()
        self.assertEqual(cm.exception.status_code, 409)
        self.assertFalse(self.skills.exists())
        from tools import skills_sync_client_org as sso
        with mock.patch.object(sso, "propose_skill", side_effect=self.ssc.SyncError("offline")):
            with self.assertRaises(HTTPException) as cm:
                self.propose()
        self.assertEqual(cm.exception.status_code, 502)
        self.assertIn("hermes sync propose test-baseline", cm.exception.detail)
        self.assertIn("disk-space", (self.skills / "standards" / "test-baseline" / "conformance.yaml").read_text())


class FixDriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        m = sqlite3.connect(root / "metrics.db")
        m.execute("CREATE TABLE conformance_results (ts REAL, host TEXT, rule TEXT, standard_rev TEXT, status TEXT,"
                  " value TEXT, evidence TEXT, verified INTEGER)")
        m.execute("INSERT INTO conformance_results VALUES (?, 'mbp', 'tunnel-answers', 'abc', 'fail', 'refused', ?, 1)",
                  (time.time() - 60, json.dumps(["GET http://127.0.0.1:9119/api/status: refused"])))
        m.commit()
        m.close()
        for name, value in (("_METRICS_DB", root / "metrics.db"), ("_KANBAN_DB", root / "kanban.db"),
                            ("_FIXES", root / "fixes.json")):
            p = mock.patch.object(api, name, value)
            p.start()
            self.addCleanup(p.stop)
        with connect_closing(api._KANBAN_DB):
            pass
        for target, kw in ((api, {"_settings": {"conformance_project": "grp/std"}, "_gitlab": STANDARD}),
                           (api.kanban_db, {"list_profiles_on_disk": ["default", "ops"]})):
            for attr, value in kw.items():
                p = mock.patch.object(target, attr, return_value=value)
                p.start()
                self.addCleanup(p.stop)
        p = mock.patch.object(api, "_start_now", side_effect=lambda out, task_id, profile: out)
        p.start()
        self.addCleanup(p.stop)

    def test_files_one_card_with_the_evidence_and_fix(self):
        out = api.fix_drift("mbp", "ops")
        self.assertFalse(out["existing"])
        with connect_closing(api._KANBAN_DB) as k:
            t = kanban_db.get_task(k, out["card"])
        self.assertEqual((t.assignee, t.created_by, t.status), ("ops", "dashboard", "ready"))
        for text in ("Tunnel answers (tunnel-answers, blocks): fail - refused", "Evidence: GET", "Fix: Re-run macos/install.sh."):
            self.assertIn(text, t.body)
        again = api.fix_drift("mbp", "ops")
        self.assertEqual((again["card"], again["existing"]), (out["card"], True))
        with connect_closing(api._KANBAN_DB) as k:
            kanban_db.archive_task(k, out["card"])
        self.assertNotEqual(api.fix_drift("mbp", "ops")["card"], out["card"])

    def test_only_the_route_starts_the_card(self):
        with mock.patch.object(api, "_start_now", side_effect=lambda out, task_id, profile: out) as start:
            card = api.drift_card("mbp", "ops")["card"]
            start.assert_not_called()
            with connect_closing(api._KANBAN_DB) as k:
                kanban_db.archive_task(k, card)
            api.fix_drift("mbp", "ops")
            start.assert_called_once()

    def test_a_fix_recorded_meanwhile_waits_for_the_lock(self):
        import threading
        with api._locked(api._FIXES):
            t = threading.Thread(target=api._record_fix, args=("mini", {"ts": 1.0}))
            t.start()
            t.join(0.2)
            self.assertTrue(t.is_alive())
            api._FIXES.write_text(json.dumps({"mbp": {"ts": 2.0}}))
        t.join()
        self.assertEqual(set(api._read_fixes()), {"mbp", "mini"})

    def test_by_hand_records_the_fix_and_files_no_card(self):
        std = STANDARD.replace("    fix: Re-run macos/install.sh.\n", "    fix: Re-run macos/install.sh.\n    remediate: ./macos/install.sh\n", 1)
        with mock.patch.object(api, "_gitlab", return_value=std):
            out = api.fix_drift("mbp", None)
        self.assertEqual((out["card"], out["status"], out["commands"]), (None, "by hand", ["./macos/install.sh"]))
        with connect_closing(api._KANBAN_DB) as k:
            self.assertEqual(k.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        fix = api._read_fixes()["mbp"]
        self.assertEqual((fix["via"], fix["rules"]), ("hand", ["tunnel-answers"]))
        card = api.fix_drift("mbp", "ops")
        self.assertEqual(api._read_fixes()["mbp"]["card"], card["card"])

    def test_a_non_text_value_and_no_metrics_db(self):
        with sqlite3.connect(api._METRICS_DB) as m:
            m.execute("UPDATE conformance_results SET value = 3")
        with connect_closing(api._KANBAN_DB) as k:
            pass
        out = api.fix_drift("mbp", "ops")
        with connect_closing(api._KANBAN_DB) as k:
            self.assertIn("fail - 3", kanban_db.get_task(k, out["card"]).body)
        api._METRICS_DB.unlink()
        with self.assertRaises(HTTPException) as cm:
            api.fix_drift("mbp", "ops")
        self.assertEqual(cm.exception.status_code, 404)

    def test_refuses_a_passing_host_and_an_unknown_profile(self):
        for host, profile, code in (("mini", "ops", 409), ("mbp", "nobody", 400), ("mini", None, 409)):
            with self.assertRaises(HTTPException) as cm:
                api.fix_drift(host, profile)
            self.assertEqual(cm.exception.status_code, code)


class FixStatesTest(unittest.TestCase):
    def test_waits_for_a_newer_report_then_says_fixed_or_still_failing(self):
        fix = {"ts": 100.0, "via": "hand", "card": None, "rules": ["a", "b"]}
        hosts = [{"host": "old", "ts": 90.0, "results": {}},
                 {"host": "good", "ts": 200.0, "results": {"a": {"status": "pass"}, "b": {"status": "pass"}}},
                 {"host": "bad", "ts": 200.0, "results": {"a": {"status": "pass"}, "b": {"status": "warn"}}}]
        out = api.fix_states({h: fix for h in ("old", "good", "bad", "gone")}, hosts)
        self.assertEqual({h: f["state"] for h, f in out.items()},
                         {"old": "waiting", "good": "fixed", "bad": "still failing", "gone": "waiting"})
        self.assertEqual((out["bad"]["failing"], out["bad"]["report_ts"]), (["b"], 200.0))


class FixProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "app"
        self.repo.mkdir()
        self.token = "glpat-" + "x" * 20
        (self.repo / "run.sh").write_text(f"export T={self.token}\ncd /Users" + "/someone/app\n")
        (self.repo / "ok.py").write_text("print('fine')\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        api._scan_cache.clear()
        p = mock.patch.object(api, "_KANBAN_DB", base / "kanban.db")
        p.start()
        self.addCleanup(p.stop)
        with connect_closing(api._KANBAN_DB):
            pass
        for target, attr, value in ((api, "_settings", {}), (api.kanban_db, "list_profiles_on_disk", ["ops"])):
            p = mock.patch.object(target, attr, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(api, "_start_now", side_effect=lambda out, task_id, profile: out)
        p.start()
        self.addCleanup(p.stop)

    def test_files_one_card_naming_each_line_but_not_its_text(self):
        out = api.fix_project(str(self.repo), "ops")
        with connect_closing(api._KANBAN_DB) as k:
            t = kanban_db.get_task(k, out["card"])
        self.assertEqual((t.title, t.assignee, t.created_by), ("Fix conformance findings in app", "ops", "dashboard"))
        for text in ("No API tokens in the repo (no-api-tokens, blocks, from built-in): 1 line", "  run.sh:1",
                     "no-home-paths, warns", "  run.sh:2", "Fix: Revoke the token"):
            self.assertIn(text, t.body)
        self.assertNotIn(self.token, t.body)
        self.assertEqual(api.fix_project(str(self.repo), "ops")["card"], out["card"])

    def test_refuses_a_clean_project_a_non_repo_and_an_unknown_profile(self):
        (self.repo / "run.sh").write_text("echo hi\n")
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        for root, profile, code in ((self.repo, "ops", 409), (plain, "ops", 409), (self.repo, "nobody", 400)):
            with self.assertRaises(HTTPException) as cm:
                api.fix_project(str(root), profile)
            self.assertEqual(cm.exception.status_code, code)


if __name__ == "__main__":
    unittest.main()
