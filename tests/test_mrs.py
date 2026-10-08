import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def job(i, name, stage, status, allow_failure=False):
    return {"id": i, "name": name, "stage": stage, "status": status, "allow_failure": allow_failure}


class StagesTest(unittest.TestCase):
    def test_stage_state_from_its_jobs(self):
        jobs = [
            job(5, "deploy", "deploy", "manual"),
            job(1, "build", "build", "success"),
            job(2, "lint", "test", "failed"),
            job(6, "lint", "test", "success"),       # the retry replaces the failed run
            job(3, "unit", "test", "running"),
            job(4, "audit", "eval", "failed", allow_failure=True),
        ]
        self.assertEqual(api.pipeline_stages(jobs),
                         [["build", "ok"], ["test", "run"], ["eval", "ok"], ["deploy", "skip"]])

    def test_failed_beats_running(self):
        jobs = [job(1, "a", "test", "running"), job(2, "b", "test", "failed")]
        self.assertEqual(api.pipeline_stages(jobs), [["test", "fail"]])


class MrsRouteTest(unittest.TestCase):
    def mr(self, iid, state, **kw):
        return {"iid": iid, "project_id": 7, "state": state, "title": f"MR {iid}", "web_url": f"u/{iid}",
                "references": {"full": f"g/repo!{iid}"}, "author": {"username": "dev"},
                "created_at": "2026-10-06T10:00:00Z", "updated_at": "2026-10-06T11:00:00Z", **kw}

    def test_open_and_recently_merged_with_pipelines(self):
        now = api.time.time()
        old = api.time.strftime("%Y-%m-%dT%H:%M:%SZ", api.time.gmtime(now - 3 * 86400))
        new = api.time.strftime("%Y-%m-%dT%H:%M:%SZ", api.time.gmtime(now - 3600))
        replies = {
            "state=opened": [self.mr(1, "opened", draft=True, reviewers=[{"username": "rev"}])],
            "state=merged": [self.mr(2, "merged", merged_at=new), self.mr(3, "merged", merged_at=old)],
            "merge_requests/1/pipelines": [], "merge_requests/2/pipelines": [{"id": 9, "status": "success", "web_url": "p/9"}],
            "pipelines/9/jobs": [job(1, "build", "build", "success")],
        }

        def fake(path, body=None):
            return json.dumps(next(v for k, v in replies.items() if k in path))

        with mock.patch.object(api, "_gitlab", fake):
            out = api.merge_requests(hours=24)
        self.assertEqual(out["errors"], {})
        self.assertEqual([(i["iid"], i["state"]) for i in out["items"]], [(1, "draft"), (2, "merged")])
        self.assertEqual(out["items"][0]["reviewers"], ["rev"])
        self.assertIsNone(out["items"][0]["pipeline"])
        self.assertEqual(out["items"][1]["pipeline"], {"status": "success", "url": "p/9", "stages": [["build", "ok"]]})
        self.assertEqual(out["items"][1]["repo"], "g/repo")

    def test_pipelines_see_the_request_context(self):
        # the profile's secrets are scoped by a context variable; worker threads must carry it
        scope = api.contextvars.ContextVar("scope", default=None)
        mrs = [self.mr(1, "opened"), self.mr(2, "opened")]

        def fake(path, body=None):
            if "state=" in path:
                return json.dumps(mrs if "opened" in path else [])
            if scope.get() != "ops":
                raise RuntimeError("could not read this profile's OPERATIONS_GITLAB_TOKEN")
            return json.dumps([])

        token = scope.set("ops")
        try:
            with mock.patch.object(api, "_gitlab", fake):
                out = api.merge_requests(hours=24)
        finally:
            scope.reset(token)
        self.assertEqual(out["errors"], {})
        self.assertEqual([i["pipeline_unavailable"] for i in out["items"]], [False, False])

    def test_unreadable_pipeline_is_marked_not_missing(self):
        def fake(path, body=None):
            if "state=" in path:
                return json.dumps([self.mr(1, "opened")] if "opened" in path else [])
            raise RuntimeError("boom")

        with mock.patch.object(api, "_gitlab", fake):
            out = api.merge_requests(hours=24)
        self.assertIsNone(out["items"][0]["pipeline"])
        self.assertTrue(out["items"][0]["pipeline_unavailable"])
        self.assertIn("1 of 1 pipelines unavailable: boom", out["errors"]["pipelines"])

    def test_no_login_is_reported(self):
        with mock.patch.object(api, "_gitlab", side_effect=RuntimeError("no GitLab login")):
            out = api.merge_requests(hours=24)
        self.assertEqual(out["items"], [])
        self.assertIn("no GitLab login", out["errors"]["gitlab"])


if __name__ == "__main__":
    unittest.main()
