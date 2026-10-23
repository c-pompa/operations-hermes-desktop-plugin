"""Connecting GitLab from the page: the logins it may adopt, and the write it performs.

Two things are being held still here. The token: the page is told which logins exist and whether
each has a token, never the token itself, and the only place the value goes is the profile's .env
through Hermes' own credential writer. And the *read* side: the catalogue's rule 11 says a plugin
does not read .env files or another tool's login, so the fixtures below plant exactly those files
and assert that nothing in the setup payload or the source list comes from them.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class GitlabConnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.yaml").write_text("plugins:\n  enabled: [operations]\n")
        for name in ("default", "beta-cloud"):
            home = self.root if name == "default" else self.root / "profiles" / name
            home.mkdir(parents=True, exist_ok=True)
        # a credential store another profile owns: planted so the tests can prove it is never read
        (self.root / "profiles" / "beta-cloud" / ".env").write_text(
            "GITLAB_HOST=gitlab.decoy\nGITLAB_TOKEN=glpat-DECOY-profile-env\n")
        self.secret = {"GITLAB_HOST": "gitlab.example.com", "GITLAB_TOKEN": "glpat-here"}
        self.wrote, self.env_wrote = [], []
        patches = [
            mock.patch.object(api, "get_default_hermes_root", lambda: self.root),
            mock.patch.object(api, "get_hermes_home", lambda: self.root),
            mock.patch.object(api, "get_secret", lambda name: self.secret.get(name, "")),
            mock.patch.object(api, "gitlab_state", lambda fresh=False: {"host": "gitlab.example.com",
                                                                       "has_token": True, "ok": True,
                                                                       "user": "someone", "error": ""}),
            mock.patch("hermes_cli.plugins_settings.save_plugin_settings",
                       lambda pid, d, values: (self.wrote.append(values), list(values))[1]),
            mock.patch("hermes_cli.config_env_routing.save_env_setting",
                       lambda key, value: self.env_wrote.append((key, value))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_page_sees_this_profiles_login_but_never_a_token(self):
        sources = api.gitlab_sources()
        self.assertEqual([s["id"] for s in sources], ["env"])
        self.assertEqual(sources[0], {"id": "env", "label": "this profile's environment",
                                      "host": "gitlab.example.com", "has_token": True})
        self.assertTrue(all("token" not in s for s in sources))
        # the JSON the page receives is the same, token-free
        self.assertNotIn("glpat", json.dumps(api.setup()))

    def test_another_profiles_env_file_is_never_read_or_offered(self):
        # rule 11: a profile's .env is Hermes' credential store, and Hermes is what reads it. The
        # decoy planted in setUp holds both a host and a token; neither may reach the page, and the
        # readers that used to parse it must not come back.
        self.secret = {}
        with mock.patch.object(api, "_gitlab_host", lambda: ""):
            payload = json.dumps(api.setup())
            sources = api.gitlab_sources()
        self.assertNotIn("DECOY", payload)
        self.assertNotIn("gitlab.decoy", payload)
        self.assertEqual(sources, [])
        for gone in ("_env_logins", "_glab_login"):
            self.assertFalse(hasattr(api, gone), f"{gone} is back: a plugin must not read credential stores")

    def test_a_host_with_no_token_is_still_named_as_a_source(self):
        self.secret = {}
        (self.root / "config.yaml").write_text(
            "plugins:\n  entries:\n    gitlab-mr:\n      settings:\n        gitlab_host: gitlab.example.com\n")
        self.assertEqual(api.gitlab_sources(), [{"id": "gitlab-mr", "label": "the gitlab-mr plugin",
                                                 "host": "gitlab.example.com", "has_token": False}])

    def test_a_token_with_no_host_is_offered_for_the_host_already_configured(self):
        # GITLAB_TOKEN set with no GITLAB_HOST is a login for the host this plugin is already on
        self.secret = {"GITLAB_TOKEN": "glpat-only"}
        with mock.patch.object(api, "_gitlab_host", lambda: "gitlab.example.com"):
            sources = api.gitlab_sources()
        self.assertEqual(sources, [{"id": "env", "label": "this profile's environment",
                                    "host": "gitlab.example.com", "has_token": True}])

    def test_adopting_the_environments_login_writes_the_plugins_own_settings(self):
        out = api.setup_gitlab({"source": "env"})
        self.assertEqual(self.wrote, [{"gitlab_host": "gitlab.example.com"}])
        self.assertEqual(self.env_wrote, [("OPERATIONS_GITLAB_TOKEN", self.secret["GITLAB_TOKEN"])])
        self.assertEqual(out["wrote"], ["gitlab_host", "gitlab_token"])
        self.assertTrue(out["gitlab"]["ok"])
        self.assertNotIn("glpat", json.dumps(out))

    def test_typing_a_pair_writes_the_same_two_settings(self):
        out = api.setup_gitlab({"host": "gitlab.example.com", "token": "glpat-typed"})
        self.assertEqual(self.wrote, [{"gitlab_host": "gitlab.example.com"}])
        self.assertEqual(self.env_wrote, [("OPERATIONS_GITLAB_TOKEN", "glpat-typed")])
        self.assertNotIn("glpat-typed", json.dumps(out))

    def test_refusals(self):
        # a source with a host and no token exists, so "adopt it" can be refused for the right reason
        (self.root / "config.yaml").write_text(
            "plugins:\n  entries:\n    gitlab-mr:\n      settings:\n        gitlab_host: gitlab.example.com\n")
        self.secret = {}
        cases = [
            ({"source": "nobody"}, 404),
            ({"source": "profile:beta-cloud"}, 404),  # a profile .env is not a source any more
            ({"source": "glab"}, 404),                # neither is the glab CLI's own config
            ({"source": "gitlab-mr"}, 409),           # a host but no token: not adoptable
            ({"host": "gitlab.example.com"}, 400),    # no token
            ({"token": "glpat-x"}, 400),              # no host
            ({"host": "https://gitlab.example.com", "token": "glpat-x"}, 400),
        ]
        for body, code in cases:
            with self.subTest(body=body):
                with self.assertRaises(HTTPException) as cm:
                    api.setup_gitlab(body)
                self.assertEqual(cm.exception.status_code, code)
                self.assertNotIn("glpat", str(cm.exception.detail))
        self.assertEqual((self.wrote, self.env_wrote), ([], []))

    def test_a_writer_that_refuses_is_reported_without_the_token(self):
        def refuse(pid, d, values):
            raise PermissionError("managed by the organisation")
        with mock.patch("hermes_cli.plugins_settings.save_plugin_settings", refuse):
            with self.assertRaises(HTTPException) as cm:
                api.setup_gitlab({"host": "gitlab.example.com", "token": "glpat-secret"})
        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("managed", cm.exception.detail)
        self.assertNotIn("glpat-secret", str(cm.exception.detail))


class PluginCopyTest(unittest.TestCase):
    """Which copy is serving the page, against the shared install — the quiet failure that hid 0.2.0."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.own = self.root / "profiles" / "gamma-lab" / "plugins" / "operations"
        self.shared = self.root / "plugins" / "operations"

    def tearDown(self):
        self.tmp.cleanup()

    def copy_of(self, version, at):
        at.mkdir(parents=True, exist_ok=True)
        (at / "plugin.yaml").write_text(f"id: operations\nversion: {version}\n")

    def test_a_profile_copy_left_behind_is_reported_as_stale_with_its_own_command(self):
        self.copy_of("0.1.0", self.own)
        self.copy_of("0.3.0", self.shared)
        with mock.patch.object(api, "get_default_hermes_root", lambda: self.root), \
             mock.patch.object(api, "__file__", str(self.own / "dashboard" / "plugin_api.py")), \
             mock.patch.object(api, "_profile_name", lambda: "gamma-lab"):
            out = api.plugin_copy()
        self.assertEqual((out["page"], out["shared"]), ("0.1.0", "0.3.0"))
        self.assertTrue(out["stale"])
        self.assertFalse(out["newer"])
        self.assertIn("hermes -p gamma-lab plugins update operations", out["hint"])

    def test_the_shared_install_serving_default_is_not_stale(self):
        self.copy_of("0.3.0", self.shared)
        with mock.patch.object(api, "get_default_hermes_root", lambda: self.root), \
             mock.patch.object(api, "__file__", str(self.shared / "dashboard" / "plugin_api.py")), \
             mock.patch.object(api, "_profile_name", lambda: "default"):
            out = api.plugin_copy()
        self.assertEqual((out["page"], out["shared"]), ("0.3.0", "0.3.0"))
        self.assertFalse(out["stale"])
        self.assertEqual(out["hint"], "")

    def test_zero_ten_is_newer_than_zero_nine(self):
        self.copy_of("0.10.0", self.own)
        self.copy_of("0.9.0", self.shared)
        with mock.patch.object(api, "get_default_hermes_root", lambda: self.root), \
             mock.patch.object(api, "__file__", str(self.own / "dashboard" / "plugin_api.py")), \
             mock.patch.object(api, "_profile_name", lambda: "writer"):
            out = api.plugin_copy()
        # string order would call 0.10.0 the older one
        self.assertFalse(out["stale"])
        self.assertTrue(out["newer"])

    def test_a_copy_with_no_shared_install_is_not_called_stale(self):
        self.copy_of("0.3.0", self.own)
        with mock.patch.object(api, "get_default_hermes_root", lambda: self.root), \
             mock.patch.object(api, "__file__", str(self.own / "dashboard" / "plugin_api.py")), \
             mock.patch.object(api, "_profile_name", lambda: "gamma-lab"):
            out = api.plugin_copy()
        self.assertEqual((out["page"], out["shared"]), ("0.3.0", None))
        self.assertFalse(out["stale"])

    def test_setup_carries_the_comparison(self):
        self.copy_of("0.1.0", self.own)
        self.copy_of("0.3.0", self.shared)
        with mock.patch.object(api, "get_default_hermes_root", lambda: self.root), \
             mock.patch.object(api, "__file__", str(self.own / "dashboard" / "plugin_api.py")), \
             mock.patch.object(api, "_profile_name", lambda: "gamma-lab"), \
             mock.patch.object(api, "profile_states", lambda: []), \
             mock.patch.object(api, "_standard_project", lambda: ""), \
             mock.patch.object(api, "gitlab_sources", lambda: []), \
             mock.patch.object(api, "_METRICS_DB", self.root / "nope.db"), \
             mock.patch.object(api, "_KANBAN_DB", self.root / "nope.db"), \
             mock.patch.object(api, "gitlab_state", lambda fresh=False: {"host": "", "has_token": False,
                                                                        "ok": False, "user": None, "error": ""}):
            out = api.setup()
        self.assertTrue(out["copy"]["stale"])
        self.assertEqual(out["copy"]["page"], "0.1.0")


if __name__ == "__main__":
    unittest.main()
