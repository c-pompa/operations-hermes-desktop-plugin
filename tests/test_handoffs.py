import importlib.util
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

NOW = int(time.time())


class HandoffsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.metrics, self.kanban = root / "metrics.db", root / "kanban.db"
        m = sqlite3.connect(self.metrics)
        m.execute("CREATE TABLE records (ts REAL, host TEXT, kind TEXT, profile TEXT, session_id TEXT,"
                  " parent_session_id TEXT, model TEXT, duration_s REAL)")
        stops = [(NOW - 100, "mbp", "coder", "c1", "p1", 60.0), (NOW - 90, "mbp", "coder", "c2", "p1", 30.0),
                 (NOW - 50, "mini", None, "c3", "p2", None), (NOW - 900000, "mbp", "coder", "c4", "p3", 5.0)]
        for ts, host, prof, sid, parent, dur in stops:
            m.execute("INSERT INTO records VALUES (?, ?, 'subagent_stop', ?, ?, ?, NULL, ?)", (ts, host, prof, sid, parent, dur))
        for sid, model, n in (("c1", "flash", 3), ("c2", "flash", 2), ("c3", "glm", 1)):
            for _ in range(n):
                m.execute("INSERT INTO records VALUES (?, 'mbp', 'api', 'coder', ?, NULL, ?, 1)", (NOW - 120, sid, model))
        m.commit()
        m.close()
        k = sqlite3.connect(self.kanban)
        k.executescript("""
CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, created_at INTEGER, created_by TEXT, completed_at INTEGER);
CREATE TABLE task_runs (task_id TEXT, profile TEXT, started_at INTEGER, ended_at INTEGER, outcome TEXT, error TEXT);
""")
        k.execute("INSERT INTO tasks VALUES ('t1', 'Ship it', 'backend-eng', 'done', ?, 'cloud', ?)", (NOW - 7200, NOW - 3600))
        k.execute("INSERT INTO tasks VALUES ('t2', 'Old open', NULL, 'ready', ?, 'cloud', NULL)", (NOW - 9000000,))
        k.execute("INSERT INTO tasks VALUES ('t3', 'Old done', 'writer', 'done', ?, 'cloud', ?)", (NOW - 9000000, NOW - 8000000))
        k.execute("INSERT INTO task_runs VALUES ('t1', 'backend-eng', ?, ?, 'crashed', ?)", (NOW - 7000, NOW - 6000, "x" * 500))
        k.execute("INSERT INTO task_runs VALUES ('t1', 'backend-eng', ?, ?, 'completed', NULL)", (NOW - 5000, NOW - 3600))
        k.commit()
        k.close()
        self.saved = api._METRICS_DB, api._KANBAN_DB
        api._METRICS_DB, api._KANBAN_DB = self.metrics, self.kanban

    def tearDown(self):
        api._METRICS_DB, api._KANBAN_DB = self.saved
        self.tmp.cleanup()

    def test_cards_with_runs_and_open_cards_from_any_time(self):
        out = api.handoffs_route(24)
        self.assertEqual(out["errors"], {})
        cards = {i["id"]: i for i in out["items"] if i["kind"] == "card"}
        self.assertEqual(set(cards), {"t1", "t2"})
        self.assertEqual((cards["t2"]["to"], cards["t2"]["open"]), ("unassigned", True))
        runs = cards["t1"]["runs"]
        self.assertEqual([r["outcome"] for r in runs], ["crashed", "completed"])
        self.assertEqual((len(runs[0]["error"]), runs[1]["error"]), (api.RUN_ERROR_CHARS, None))

    def test_subagents_group_by_parent_session(self):
        subs = [i for i in api.handoffs_route(24)["items"] if i["kind"] == "subagent"]
        self.assertEqual([(s["from"], s["where"], s["title"]) for s in subs],
                         [("coder", "mini", "1 subagent, 1 request"), ("coder", "mbp", "2 subagents, 5 requests")])
        self.assertEqual((subs[1]["start"], subs[1]["end"]), (NOW - 160, NOW - 90))
        self.assertEqual(subs[1]["children"][0]["model"], "flash")

    def test_pairs_put_open_work_first(self):
        pairs = api.handoffs_route(24)["pairs"]
        self.assertEqual([(p["from"], p["to"], p["count"], p["open"]) for p in pairs],
                         [("cloud", "unassigned", 1, 1), ("coder", "subagent", 2, 0), ("cloud", "backend-eng", 1, 0)])

    def test_older_metrics_without_profile_and_missing_kanban(self):
        m = sqlite3.connect(self.metrics)
        m.execute("ALTER TABLE records DROP COLUMN profile")
        m.commit()
        m.close()
        api._KANBAN_DB = Path(self.tmp.name) / "absent.db"
        out = api.handoffs_route(24)
        self.assertEqual(set(out["errors"]), {"kanban"})
        self.assertEqual(sorted(i["from"] for i in out["items"]), ["mbp (no profile)", "mini (no profile)"])
        self.assertFalse(api._KANBAN_DB.exists())

    def test_start_limits_to_that_window(self):
        out = api.handoffs_route(1, start=NOW - 7300)
        self.assertEqual(out["start"], NOW - 7300)
        # t1 was created in it; t2 was open then; t3 was done long before; no subagent stopped in it
        self.assertEqual({i["id"] for i in out["items"]}, {"t1", "t2"})
        self.assertEqual([i["id"] for i in api.handoffs_route(1, start=NOW - 200)["items"] if i["kind"] == "subagent"],
                         ["subagents:p2", "subagents:p1"])

    def test_read_only(self):
        before = self.metrics.read_bytes(), self.kanban.read_bytes()
        api.handoffs_route(168)
        self.assertEqual((self.metrics.read_bytes(), self.kanban.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
