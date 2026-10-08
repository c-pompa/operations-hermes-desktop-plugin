import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api_projects", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

KEY = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
STD = """repo_rules:
  - id: no-stale-addresses
    title: No old host names in code
    severity: blocks
    check: { kind: absent, pattern: 'OLDHOST-[0-9]+', paths: ["*.sh"] }
    exceptions:
      - { path: old.sh, reason: on purpose, owner: x }
    fix: Use the hostname.
  - id: shellcheck
    title: Shell scripts pass shellcheck
    severity: blocks
    check: { kind: command, ci_job: shellcheck }
"""


class ProjectChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo, self.plain = base / "repo", base / "plain"
        self.repo.mkdir()
        self.plain.mkdir()
        home = "/Users" + "/someone/"  # split so this file does not match the rule itself
        files = {"deploy.sh": f"ssh OLDHOST-20\ncd {home}app\n", "old.sh": "ping OLDHOST-5\n",
                 "notes.md": f"path {home}x\n", "tweet.md": f"RT: {KEY} | quoted\n", "id_ed25519": KEY + "\n", "blob.bin": "\0" + KEY,
                 "ignored.sh": "OLDHOST-9\n"}
        for name, text in files.items():
            (self.repo / name).write_text(text)
        (self.repo / ".gitignore").write_text("ignored.sh\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        self.saved = (api._settings, api._gitlab, api._LOCAL_STANDARD)
        self.settings = {"conformance_project": ""}
        api._LOCAL_STANDARD = base / "no-local-standard.yaml"
        api._settings = lambda: self.settings
        api._gitlab = lambda path: STD
        api._scan_cache.clear()

    def tearDown(self):
        api._settings, api._gitlab, api._LOCAL_STANDARD = self.saved
        self.tmp.cleanup()

    def test_built_in_standard_without_a_project(self):
        out = api.conformance_projects(root=[str(self.repo), str(self.plain), str(self.repo) + "-gone"])
        self.assertEqual([r["id"] for r in out["rules"]], [r["id"] for r in api.DEFAULT_REPO_RULES])
        repo, plain, gone = out["projects"]
        res = repo["results"]
        # a key's own BEGIN line, not one quoted in prose (tweet.md)
        self.assertEqual(res["no-private-keys"]["findings"], [{"file": "id_ed25519", "line": 1, "text": KEY}])
        self.assertEqual(res["no-private-keys"]["status"], "fail")
        # warns, and only in code: notes.md is not in its paths
        self.assertEqual((res["no-home-paths"]["status"], [f["file"] for f in res["no-home-paths"]["findings"]]),
                         ("warn", ["deploy.sh"]))
        self.assertEqual(res["no-api-tokens"], {"status": "pass", "count": 0, "findings": []})
        self.assertEqual((plain["error"], gone["error"]), ("not a git repository", "folder not found"))

    def test_project_adds_its_absent_rules_with_exceptions(self):
        self.settings = {"conformance_project": "group/iac"}
        out = api.conformance_projects(root=[str(self.repo)])
        ids = [r["id"] for r in out["rules"]]
        self.assertIn("no-stale-addresses", ids)
        self.assertNotIn("shellcheck", ids)  # needs a tool; left to that repo's CI
        res = out["projects"][0]["results"]["no-stale-addresses"]
        # old.sh is excepted and ignored.sh is not tracked
        self.assertEqual([(f["file"], f["line"]) for f in res["findings"]], [("deploy.sh", 1)])

    def test_unreachable_standard_still_runs_the_built_in_rules(self):
        self.settings = {"conformance_project": "group/iac"}

        def down(path):
            raise OSError("no route")
        api._gitlab = down
        out = api.conformance_projects(root=[str(self.repo)])
        self.assertEqual(out["errors"], {"rules": "no route"})
        self.assertEqual(out["projects"][0]["results"]["no-private-keys"]["status"], "fail")

    def test_an_unchanged_repo_is_not_read_again_and_an_edit_shows_at_once(self):
        scans, real = [], api.scan_project
        api.scan_project = lambda root, rules: scans.append(root) or real(root, rules)
        self.addCleanup(setattr, api, "scan_project", real)
        status = lambda: api.conformance_projects(root=[str(self.repo)])["projects"][0]["results"]["no-api-tokens"]["status"]
        self.assertEqual((status(), status(), len(scans)), ("pass", "pass", 1))
        (self.repo / "old.sh").write_text("token ghp_" + "a" * 36 + "\n")
        self.assertEqual((status(), len(scans)), ("fail", 2))
        (self.repo / "old.sh").write_text("ping\n")
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "x"],
                       check=True)
        self.assertEqual((status(), status(), len(scans)), ("pass", "pass", 3))

    def test_a_repos_fsmonitor_is_not_run(self):
        marker = self.repo.parent / "ran"
        hook = self.repo.parent / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(self.repo), "config", "core.fsmonitor", str(hook)], check=True)
        api.repo_state(str(self.repo))
        self.assertFalse(marker.exists())

    def test_a_projects_own_conformance_yaml_adds_rules_for_that_project_only(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (other / "a.txt").write_text("TODO-1\n")
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        subprocess.run(["git", "-C", str(other), "add", "."], check=True)
        (self.repo / "conformance.yaml").write_text("""repo_rules:
  - id: no-todo
    title: No TODO markers
    check: { kind: absent, pattern: 'TODO-[0-9]+', paths: "*.sh" }
  - id: no-private-keys
    severity: warns
    check: { kind: absent, pattern: 'never', paths: ["*"] }
  - id: broken
    check: { kind: absent, pattern: '(', paths: ["*"] }
""")
        (self.repo / "deploy.sh").write_text("TODO-2\n")
        out = api.conformance_projects(root=[str(self.repo), str(other)])
        rule = next(r for r in out["rules"] if r["id"] == "no-todo")
        self.assertEqual((rule["source"], rule["severity"]), ("conformance.yaml", "warns"))
        repo, oth = out["projects"]
        self.assertEqual(repo["results"]["no-todo"]["status"], "warn")
        self.assertNotIn("no-todo", oth["results"])  # another project's rule is not applied here
        # the built-in rule keeps its own definition, and the broken rule is reported, not run
        self.assertEqual(repo["results"]["no-private-keys"]["status"], "fail")
        self.assertIn("broken: pattern does not compile", repo["rules_error"])
        self.assertNotIn("broken", repo["results"])

    def test_an_unreadable_conformance_yaml_is_reported_and_the_rest_still_run(self):
        (self.repo / "conformance.yaml").write_text("repo_rules: [unclosed\n")
        row = api.conformance_projects(root=[str(self.repo)])["projects"][0]
        self.assertTrue(row["rules_error"].startswith("conformance.yaml:"))
        self.assertEqual(row["results"]["no-private-keys"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
