import importlib.util
import json
import unittest
import urllib.error
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


class FakeResp:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class GitlabClientTest(unittest.TestCase):
    """The client itself: the GET cache, and what a rate limit does."""

    def setUp(self):
        api._gitlab_cache.clear()
        self.addCleanup(api._gitlab_cache.clear)
        for patch in (mock.patch.object(api, "_gitlab_host", lambda: "gitlab.x"),
                     mock.patch.object(api, "_gitlab_token", lambda: "tok")):
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, *replies):
        """A fake urlopen: hands out the replies in order and counts the calls."""
        self.calls = []

        def urlopen(req, timeout=None):
            self.calls.append(req.full_url)
            reply = replies[min(len(self.calls) - 1, len(replies) - 1)]
            if isinstance(reply, Exception):
                raise reply
            return FakeResp(reply)
        return mock.patch.object(api.urllib.request, "urlopen", urlopen)

    @staticmethod
    def too_many(retry_after=None):
        headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
        return urllib.error.HTTPError("https://gitlab.x/api/v4/user", 429, "Too Many Requests", headers, None)

    def test_a_get_is_reused_for_the_ttl_and_a_post_never_is(self):
        with self.serve('{"username": "cpompa"}'):
            self.assertEqual(api._gitlab("user"), '{"username": "cpompa"}')
            self.assertEqual(api._gitlab("user"), '{"username": "cpompa"}')
            self.assertEqual(len(self.calls), 1)  # the second read came from the cache
        # a write is a POST and must always reach GitLab
        with self.serve('{"ok": true}'):
            api._gitlab("projects/g%2Frepo/merge_requests", body={"a": 1})
            api._gitlab("projects/g%2Frepo/merge_requests", body={"a": 1})
            self.assertEqual(len(self.calls), 2)

    def test_a_newer_answer_is_fetched_once_the_ttl_passes(self):
        with self.serve('"first"', '"second"'):
            self.assertEqual(api._gitlab("user"), '"first"')
            api._gitlab_cache[("gitlab.x", "user")] = (api.time.time() - api.GITLAB_CACHE_S - 1, '"first"')
            self.assertEqual(api._gitlab("user"), '"second"')
        self.assertEqual(len(self.calls), 2)

    def test_a_short_rate_limit_is_waited_out_once(self):
        with mock.patch.object(api.time, "sleep") as sleep, self.serve(self.too_many(1), '{"username": "cpompa"}'):
            self.assertEqual(api._gitlab("user"), '{"username": "cpompa"}')
        sleep.assert_called_once_with(1.0)
        self.assertEqual(len(self.calls), 2)

    def test_a_long_rate_limit_is_reported_with_its_wait_instead_of_hammering(self):
        with mock.patch.object(api.time, "sleep") as sleep, self.serve(self.too_many(60)):
            with self.assertRaises(RuntimeError) as cm:
                api._gitlab("user")
        self.assertIn("retry after 60s", str(cm.exception))
        self.assertEqual((len(self.calls), sleep.call_count), (1, 0))

    def test_another_http_error_is_raised_as_it_is(self):
        boom = urllib.error.HTTPError("https://gitlab.x/api/v4/user", 401, "Unauthorized", {}, None)
        with self.serve(boom):
            with self.assertRaises(urllib.error.HTTPError):
                api._gitlab("user")


class MergedPaginationTest(unittest.TestCase):
    def page(self, n, start=0):
        return [{"iid": i, "title": f"MR {i}", "references": {"full": f"g/repo!{i}"}, "web_url": f"u/{i}",
                 "author": {"username": "dev"}, "merged_at": "2026-10-06T10:00:00Z"} for i in range(start, start + n)]

    def test_reads_on_until_a_short_page(self):
        pages = [self.page(100), self.page(1, 100)]
        with mock.patch.object(api, "_gitlab", lambda path, body=None: json.dumps(pages.pop(0))):
            rows, truncated = api.merged_pages(0.0)
        self.assertEqual((len(rows), truncated), (101, False))

    def test_a_window_busier_than_the_cap_says_it_is_truncated(self):
        full = json.dumps(self.page(100))
        paths = []
        with mock.patch.object(api, "_gitlab", lambda path, body=None: (paths.append(path), full)[1]):
            rows, truncated = api.merged_pages(0.0)
        self.assertEqual((len(rows), truncated), (api.MERGED_MAX, True))
        self.assertEqual(len(paths), api.MERGED_MAX // 100)
        self.assertIn("page=5", paths[-1])  # it stopped at the cap, not at a short page


class MrsLimitTest(unittest.TestCase):
    """A window with more open or merged MRs than the page shows must say so."""

    def mr(self, iid, state):
        return {"iid": iid, "project_id": 7, "state": state, "title": f"MR {iid}", "web_url": f"u/{iid}",
                "references": {"full": f"g/repo!{iid}"}, "author": {"username": "dev"},
                "created_at": "2026-10-06T10:00:00Z", "updated_at": "2026-10-06T11:00:00Z"}

    def test_a_page_past_the_limit_marks_the_window_as_truncated(self):
        opened = [self.mr(i, "opened") for i in range(1, api.MRS_LIMIT + 2)]
        paths = []

        def fake(path, body=None):
            paths.append(path)
            return json.dumps(opened if "state=opened" in path else [])

        with mock.patch.object(api, "_gitlab", fake):
            out = api.merge_requests(hours=24)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["items"]), api.MRS_LIMIT)
        self.assertEqual(out["showing"], {"open": api.MRS_LIMIT, "merged": 0, "limit": api.MRS_LIMIT})
        # one row past the limit is asked for, so "there is more" is a fact rather than a full page
        self.assertTrue(all(f"per_page={api.MRS_LIMIT + 1}" in p for p in paths[:2]))

    def test_a_window_that_fits_is_not_marked(self):
        def fake(path, body=None):
            return json.dumps([self.mr(1, "opened")] if "state=opened" in path else [])

        with mock.patch.object(api, "_gitlab", fake):
            out = api.merge_requests(hours=24)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["showing"], {"open": 1, "merged": 0, "limit": api.MRS_LIMIT})

if __name__ == "__main__":
    unittest.main()
