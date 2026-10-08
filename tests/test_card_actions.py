import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import connect_closing

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class CardActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = api._KANBAN_DB
        api._KANBAN_DB = Path(self.tmp.name) / "kanban.db"
        with connect_closing(api._KANBAN_DB) as k:
            self.card = kanban_db.create_task(k, title="Tracking card", created_by="cloud")
        profiles = mock.patch.object(api.kanban_db, "list_profiles_on_disk", return_value=["default", "coder"])
        profiles.start()
        self.addCleanup(profiles.stop)

    def tearDown(self):
        api._KANBAN_DB = self.saved
        self.tmp.cleanup()

    def status(self):
        with connect_closing(api._KANBAN_DB) as k:
            t = kanban_db.get_task(k, self.card)
            return t.status, t.assignee

    def test_an_alert_files_one_card_for_the_profile(self):
        with mock.patch.object(api.kbd, "dispatch_once", return_value=api.kbd.DispatchResult()), \
                mock.patch("hermes_cli.kanban._check_dispatcher_presence", return_value=(True, "")):
            out = api.fix_alert("health:R8", " 20 errors\nin the last 15 min ", "Raised at 9:00.", "coder")
            again = api.fix_alert("health:R8", "20 errors in the last 15 min", "Raised at 9:01.", "coder")
        with connect_closing(api._KANBAN_DB) as k:
            t = kanban_db.get_task(k, out["card"])
        self.assertEqual((t.title, t.body, t.assignee), ("Look into: 20 errors in the last 15 min", "Raised at 9:00.", "coder"))
        self.assertEqual((again["card"], again["existing"]), (out["card"], True))
        with self.assertRaises(HTTPException) as cm:
            api.fix_alert("health:R8", "x", "", "nobody")
        self.assertEqual(cm.exception.status_code, 400)

    def test_assignees_are_profiles_on_disk(self):
        self.assertEqual(api.assignees(), {"assignees": ["default", "coder"]})

    def assign(self, result):
        with mock.patch.object(api.kbd, "dispatch_once", return_value=result) as tick, \
                mock.patch("hermes_cli.kanban._check_dispatcher_presence", return_value=(False, "No gateway is running")):
            out = api.card_assign(self.card, "coder", None)
        tick.assert_called_once()
        return out

    def test_assign_starts_the_profiles_worker_now(self):
        out = self.assign(api.kbd.DispatchResult(spawned=[(self.card, "coder", "/ws")]))
        self.assertEqual(self.status(), ("ready", "coder"))
        self.assertEqual((out["started"], out["warning"]), (True, ""))

    def test_assign_says_why_the_worker_did_not_start(self):
        out = self.assign(api.kbd.DispatchResult(skipped_per_profile_capped=[(self.card, "coder", 2)]))
        self.assertFalse(out["started"])
        self.assertIn("busy with 2", out["warning"])
        self.assertEqual(self.assign(api.kbd.DispatchResult())["warning"], "No gateway is running")

    def test_assign_refuses_an_unknown_profile(self):
        with self.assertRaises(HTTPException) as cm:
            api.card_assign(self.card, "nobody")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.status(), ("ready", None))

    def test_close_done_and_archived(self):
        with self.assertRaises(HTTPException) as cm:
            api.card_close(self.card, "done", " ")
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(api.card_close(self.card, "done", "Fixed by hand")["status"], "done")
        with connect_closing(api._KANBAN_DB) as k:
            other = kanban_db.create_task(k, title="Not needed")
        self.assertEqual(api.card_close(other, "archived", "")["status"], "archived")

    def test_unknown_card_and_outcome(self):
        with self.assertRaises(HTTPException) as cm:
            api.card_close("t_missing", "done", "x")
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(HTTPException) as cm:
            api.card_close(self.card, "deleted", "")
        self.assertEqual(cm.exception.status_code, 400)

    def test_blocked_card_shows_why_and_unblocks_with_a_reply(self):
        with connect_closing(api._KANBAN_DB) as k:
            kanban_db.assign_task(k, self.card, "coder")
            kanban_db.block_task(k, self.card, reason="Need the go-ahead for a restart")
        with mock.patch.object(api, "_METRICS_DB", Path(self.tmp.name) / "metrics.db"):
            card = next(i for i in api.handoffs_route(24)["items"] if i.get("id") == self.card)
        self.assertEqual(card["blocked_reason"], "Need the go-ahead for a restart")
        with mock.patch.object(api.kbd, "dispatch_once",
                               return_value=api.kbd.DispatchResult(spawned=[(self.card, "coder", "/ws")])) as tick:
            out = api.card_unblock(self.card, "Go ahead, restart now")
        tick.assert_called_once()
        self.assertEqual((out["status"], out["started"]), ("ready", True))
        with connect_closing(api._KANBAN_DB) as k:
            c = kanban_db.list_comments(k, self.card)[-1]
        self.assertEqual((c.author, c.body), ("dashboard", "Go ahead, restart now"))

    def test_overview_lists_cards_waiting_on_a_person(self):
        with connect_closing(api._KANBAN_DB) as k:
            kanban_db.assign_task(k, self.card, "coder")
            kanban_db.block_task(k, self.card, reason="Need the go-ahead for a restart", kind="needs_input")
            triage = kanban_db.create_task(k, title="New idea", triage=True)
            kanban_db.create_task(k, title="Just ready")
        with mock.patch.object(api, "_METRICS_DB", Path(self.tmp.name) / "metrics.db"):
            items = {i["card"]: i for i in api.overview(24)["attention"] if i["source"] == "kanban"}
        self.assertEqual(set(items), {self.card, triage})
        self.assertEqual((items[self.card]["text"], items[self.card]["action"]),
                         ("Tracking card is waiting on you (coder)", "Need the go-ahead for a restart"))
        self.assertEqual((items[triage]["text"], items[triage]["action"]), ("New idea needs triage", "sort it on the Kanban page"))
        self.assertTrue(all(i["ts"] for i in items.values()))

    def test_unblocking_a_card_that_is_not_blocked_is_refused_without_a_comment(self):
        with self.assertRaises(HTTPException) as cm:
            api.card_unblock(self.card, "hello")
        self.assertEqual(cm.exception.status_code, 409)
        with connect_closing(api._KANBAN_DB) as k:
            self.assertEqual(kanban_db.list_comments(k, self.card), [])

    def test_no_board_is_not_created(self):
        api._KANBAN_DB = Path(self.tmp.name) / "absent.db"
        with self.assertRaises(HTTPException):
            api.card_close("t1", "done", "x")
        self.assertFalse(api._KANBAN_DB.exists())


if __name__ == "__main__":
    unittest.main()
