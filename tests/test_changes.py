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
    "operations_api_changes", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

# minute-aligned so snapshot minutes are exact
NOW = (int(time.time()) // 60) * 60
MR = {"title": "Ship the thing", "merged_at": "", "references": {"full": "group/repo!7"},
      "web_url": "https://gitlab.example/group/repo/-/merge_requests/7", "merged_by": {"username": "alice"}}


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


class ChangesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.metrics = Path(self.tmp.name) / "metrics.db"
        m = sqlite3.connect(self.metrics)
        m.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, device TEXT, model TEXT)")
        # Mini: embed always; big loaded at -50m, unloaded at -40m, loaded again at -10m and still loaded.
        # host-c: silent for 20 minutes mid-window, which must not read as its model unloading.
        for minute in range(60, 0, -1):
            ts = NOW - minute * 60
            models = ["embed", "embed:2"]
            if 40 < minute <= 50 or minute <= 10:
                models.append("big")
            for model in models:
                m.execute("INSERT INTO records (ts, kind, device, model) VALUES (?, 'pool_status', 'Mini', ?)",
                          (ts + 5, model))
            if not 20 < minute <= 40:
                m.execute("INSERT INTO records (ts, kind, device, model) VALUES (?, 'pool_status', 'host-c', 'coder')",
                          (ts + 5,))
        m.commit()
        m.close()
        self.saved = (api._METRICS_DB, api.urllib.request.urlopen, api._gitlab_token, api._gitlab_host)
        api._METRICS_DB = self.metrics
        api._gitlab_token, api._gitlab_host = (lambda: "test-token"), (lambda: "gitlab.example")
        self.mrs = []
        self.requests = []

        def fake_urlopen(req, timeout):
            self.requests.append(req)
            return io.BytesIO(json.dumps(self.mrs).encode())
        api.urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        api._METRICS_DB, api.urllib.request.urlopen, api._gitlab_token, api._gitlab_host = self.saved
        self.tmp.cleanup()

    def test_model_loads_are_grouped_and_gaps_ignored(self):
        models = [i for i in api.changes(2)["items"] if i["kind"] == "model"]
        self.assertEqual([(i["where"], i["text"], i["count"]) for i in models],
                         [("Mini", "big loaded 2 times, loaded now", 2)])
        self.assertEqual(models[0]["ts"], NOW - 10 * 60)

    def test_single_load_shows_duration(self):
        models = [i for i in api.changes(1)["items"] if i["kind"] == "model"]
        self.assertEqual([i["text"] for i in models], ["big loaded 2 times, loaded now"])
        m = sqlite3.connect(self.metrics)
        m.execute("DELETE FROM records WHERE model='big' AND ts > ?", (NOW - 30 * 60,))
        m.commit()
        m.close()
        models = [i for i in api.changes(1)["items"] if i["kind"] == "model"]
        self.assertEqual([i["text"] for i in models], ["big loaded for 10m"])

    def test_merged_mrs_in_window_only(self):
        self.mrs = [dict(MR, merged_at=iso(NOW - 600)), dict(MR, title="Old", merged_at=iso(NOW - 3 * 86400)),
                    dict(MR, title="No merge time", merged_at=None)]
        out = api.changes(24)
        merged = [i for i in out["items"] if i["kind"] == "merged"]
        self.assertEqual(out["errors"], {})
        self.assertEqual([(i["text"], i["where"], i["by"]) for i in merged], [("Ship the thing", "group/repo!7", "alice")])
        self.assertEqual(self.requests[0].get_header("Private-token"), "test-token")
        self.assertIn("state=merged", self.requests[0].full_url)

    def test_gitlab_failure_keeps_model_changes(self):
        def down(req, timeout):
            raise urllib.error.URLError("connection refused")
        api.urllib.request.urlopen = down
        out = api.changes(2)
        self.assertIn("connection refused", out["errors"]["gitlab"])
        self.assertTrue(any(i["kind"] == "model" for i in out["items"]))

    def test_missing_token_is_reported(self):
        api._gitlab_token = lambda: ""
        self.assertIn("no GitLab login", api.changes(2)["errors"]["gitlab"])


class GlabTokenTest(unittest.TestCase):
    def test_reads_only_this_hosts_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yml"
            cfg.write_text(
                "host: gitlab.example.com\n"
                "hosts:\n"
                "    gitlab.com:\n"
                "        token: wrong-one\n"
                "    gitlab.example.com:\n"
                "        api_host: gitlab.example.com\n"
                "        # a comment\n"
                "        token: 'right-one'\n"
                "    localhost:8929:\n"
                "        token: also-wrong\n")
            saved = (api._GLAB_CONFIG, api.os.environ.pop("GITLAB_TOKEN", None), api._settings, api.get_secret)
            api._GLAB_CONFIG, api._settings, api.get_secret = cfg, dict, (lambda name: None)
            try:
                self.assertEqual(api._gitlab_token(), "right-one")
                cfg.write_text("host: gitlab.example.com\nhosts:\n    gitlab.example.com:\n        user: x\n    other:\n        token: no\n")
                self.assertEqual(api._gitlab_token(), "")
                # no default host and no settings: nothing to log in to
                cfg.write_text("hosts:\n    gitlab.example.com:\n        token: t\n")
                self.assertEqual(api._gitlab_host(), "")
                # the plugin's own token setting wins over glab
                api.get_secret = lambda name: "from-settings" if name == "OPERATIONS_GITLAB_TOKEN" else None
                self.assertEqual(api._gitlab_token(), "from-settings")
            finally:
                api._GLAB_CONFIG, api._settings, api.get_secret = saved[0], saved[2], saved[3]
                if saved[1] is not None:
                    api.os.environ["GITLAB_TOKEN"] = saved[1]


if __name__ == "__main__":
    unittest.main()
