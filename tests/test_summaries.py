import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def group(key, raw=1, count=None):
    return {"key": key, "category": "incident", "kind": "error", "text": f"text {key}", "where": "mini",
            "count": count or raw, "raw": [[0, f"line {i}"] for i in range(raw)]}


def reply(content, model="qwen3-4b"):
    return SimpleNamespace(model=model, choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class SummariesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("_SUMMARIES", Path(tmp.name) / "summaries.json"),
                            ("_settings", lambda: {"activity_summaries": True})):
            p = mock.patch.object(api, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

    def llm(self, content):
        def call(**kw):
            self.calls.append(kw)
            kw["route_info"].update(provider="lmstudio", model="qwen3-4b")
            return reply(content)
        return mock.patch("agent.auxiliary_client.call_llm", call)

    def test_each_summary_names_its_model_and_events(self):
        with self.llm('Here: {"e0": "Mini failed 3 times.", "e1": ""}'):
            out = api.summarize([group("a", raw=2, count=3), group("b")])
        self.assertEqual(self.calls[0]["task"], "operations_summary")
        self.assertEqual(set(out), {"a"})  # an empty answer is no summary
        self.assertEqual({k: out["a"][k] for k in ("text", "model", "provider", "events")},
                         {"text": "Mini failed 3 times.", "model": "qwen3-4b", "provider": "lmstudio", "events": 3})

    def test_one_call_reads_at_most_the_line_budget(self):
        groups = [group(str(i), raw=15) for i in range(5)]
        with self.llm("{}"):
            api.summarize(groups)
        sent = self.calls[0]["messages"][1]["content"]
        self.assertEqual(sent.count("raw: "), 30)  # two groups of 15 fit in 40, a third would not

    def test_route_caches_and_reports_pending(self):
        items = [group("a"), group("b", raw=api.SUMMARY_LINES)]
        with mock.patch.object(api, "activity_route", lambda hours: {"items": items}), \
                self.llm('{"e0": "First."}'):
            first = api.activity_summaries({"hours": 24})
            self.assertEqual((list(first["summaries"]), first["pending"]), (["a"], 1))
            self.assertEqual(json.loads(api._SUMMARIES.read_text())["a"]["text"], "First.")
            api.activity_summaries({"hours": 24})
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("text a", self.calls[1]["messages"][1]["content"])  # cached, not asked again

    def test_a_summary_written_meanwhile_is_kept(self):
        def summarize(todo):  # another request writes while this one waits on the model
            api._SUMMARIES.write_text(json.dumps({"other": {"text": "Other.", "ts": 1}}))
            return {"a": {"text": "First.", "ts": 2}}
        with mock.patch.object(api, "activity_route", lambda hours: {"items": [group("a")]}), \
                mock.patch.object(api, "summarize", summarize):
            api.activity_summaries({"hours": 24})
        self.assertEqual(set(json.loads(api._SUMMARIES.read_text())), {"a", "other"})

    def test_model_failure_keeps_cached_and_stops_asking(self):
        with mock.patch.object(api, "activity_route", lambda hours: {"items": [group("a")]}), \
                mock.patch("agent.auxiliary_client.call_llm", side_effect=RuntimeError("no provider")):
            out = api.activity_summaries({"hours": 24})
        self.assertEqual((out["summaries"], out["pending"]), ({}, 0))
        self.assertIn("no provider", out["errors"]["model"])

    def test_hours_that_are_not_a_number_are_refused(self):
        for hours in ("abc", [24], 500):
            with self.assertRaises(HTTPException) as cm:
                api.activity_summaries({"hours": hours})
            self.assertEqual(cm.exception.status_code, 400)

    def test_off_unless_turned_on(self):
        with mock.patch.object(api, "_settings", lambda: {}):
            with self.assertRaises(HTTPException) as cm:
                api.activity_summaries({"hours": 24})
        self.assertEqual(cm.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
