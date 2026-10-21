"""The card actions: assign/start, unblock, close, and the alert-to-card path.

Starting a card is a targeted one: it goes through the dispatcher's own lane step for that card
only. These tests drive the real dispatch code against a real board with the worker spawn stubbed,
and fail if a whole-board tick happens instead.
"""
import contextlib
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


def no_tick(*a, **kw):
    raise AssertionError("a whole-board tick ran: it starts cards this page was not asked to start")


@contextlib.contextmanager
def locked(held):
    yield held


class CardActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = api._KANBAN_DB
        api._KANBAN_DB = Path(self.tmp.name) / "kanban.db"
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        # workspace_kind "dir" keeps the test out of the real ~/.hermes workspaces root
        with connect_closing(api._KANBAN_DB) as k:
            self.card = kanban_db.create_task(k, title="Tracking card", created_by="cloud",
                                              workspace_kind="dir", workspace_path=str(self.ws))
        profiles = mock.patch.object(api.kanban_db, "list_profiles_on_disk", return_value=["default", "coder"])
        profiles.start()
        self.addCleanup(profiles.stop)
        # the real spawn starts a worker process; record what the dispatcher asked for instead
        self.spawned = []
        spawn = mock.patch.object(api.kbd, "_default_spawn", self.fake_spawn)
        spawn.start()
        self.addCleanup(spawn.stop)
        presence = mock.patch("hermes_cli.kanban._check_dispatcher_presence", return_value=(True, ""))
        presence.start()
        self.addCleanup(presence.stop)
        # the dispatcher refuses a card whose assignee is not a profile on disk: "coder" is one here
        known = mock.patch.object(api.kbd, "_profile_exists_fn",
                                  lambda: (lambda name: name in ("default", "coder")))
        known.start()
        self.addCleanup(known.stop)

    def tearDown(self):
        api._KANBAN_DB = self.saved
        self.tmp.cleanup()

    def fake_spawn(self, task, workspace, *, board=None):
        self.spawned.append((task.id, str(workspace)))
        return 4242

    def status(self, card=None):
        with connect_closing(api._KANBAN_DB) as k:
            t = kanban_db.get_task(k, card or self.card)
            return t.status, t.assignee

    def ready_card(self, title="Held back"):
        with connect_closing(api._KANBAN_DB) as k:
            return kanban_db.create_task(k, title=title, workspace_kind="dir", workspace_path=str(self.ws))

    def test_an_alert_files_one_card_for_the_profile(self):
        out = api.fix_alert("health:R8", " 20 errors\nin the last 15 min ", "Raised at 9:00.", "coder")
        again = api.fix_alert("health:R8", "20 errors in the last 15 min", "Raised at 9:01.", "coder")
        with connect_closing(api._KANBAN_DB) as k:
            t = kanban_db.get_task(k, out["card"])
        self.assertEqual((t.title, t.body, t.assignee), ("Look into: 20 errors in the last 15 min", "Raised at 9:00.", "coder"))
        self.assertEqual((again["card"], again["existing"]), (out["card"], True))
        # the card it files for is the only card it starts
        self.assertEqual([c for c, _ in self.spawned], [out["card"]])
        with self.assertRaises(HTTPException) as cm:
            api.fix_alert("health:R8", "x", "", "nobody")
        self.assertEqual(cm.exception.status_code, 400)

    def test_assignees_are_profiles_on_disk(self):
        self.assertEqual(api.assignees(), {"assignees": ["default", "coder"]})

    # These drive the real dispatcher through start_task_once, which a Hermes older than that function
    # does not have: on such a host the plugin falls back to a whole-board tick, covered below.
    @unittest.skipUnless(hasattr(api.kbd, "start_task_once"), "this Hermes predates start_task_once")
    def test_assign_starts_that_card_and_leaves_another_ready_card_alone(self):
        other = self.ready_card()
        with mock.patch.object(api.kbd, "dispatch_once", side_effect=no_tick):
            out = api.card_assign(self.card, "coder", None)
        self.assertEqual(self.spawned, [(self.card, str(self.ws))])
        self.assertEqual((out["started"], out["also_started"], out["warning"]), (True, [], ""))
        self.assertEqual(self.status(), ("running", "coder"))
        with connect_closing(api._KANBAN_DB) as k:
            held = kanban_db.get_task(k, other)
            self.assertIsNotNone(held)
            runs = k.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (other,)).fetchone()[0]
            events = [r[0] for r in k.execute("SELECT kind FROM task_events WHERE task_id=?", (other,))]
        # the card nobody asked about is exactly as it was: ready, unclaimed, no run, no new events
        self.assertEqual((held.status, held.claim_lock, runs), ("ready", None, 0))
        self.assertEqual(events, ["created"])

    @unittest.skipUnless(hasattr(api.kbd, "start_task_once"), "this Hermes predates start_task_once")
    def test_assign_says_when_the_profile_is_busy(self):
        busy = self.ready_card("Already running")
        with connect_closing(api._KANBAN_DB) as k:
            kanban_db.assign_task(k, busy, "coder")
            kanban_db.claim_task(k, busy)
        with mock.patch.object(api, "load_config", lambda: {"kanban": {"max_in_progress_per_profile": 1}}), \
                mock.patch.object(api.kbd, "dispatch_once", side_effect=no_tick):
            out = api.card_assign(self.card, "coder", None)
        self.assertEqual((out["started"], self.spawned), (False, []))
        self.assertIn("busy with 1 card(s)", out["warning"])
        self.assertEqual(self.status(), ("ready", "coder"))

    @unittest.skipUnless(hasattr(api.kbd, "start_task_once"), "this Hermes predates start_task_once")
    def test_a_board_locked_by_another_dispatcher_starts_nothing_here(self):
        with mock.patch.object(api.kbd._kbc, "_dispatch_tick_lock", lambda path: locked(False)), \
                mock.patch.object(api.kbd, "dispatch_once", side_effect=no_tick):
            out = api.card_assign(self.card, "coder", None)
        # the lock holder is mid-tick and claims it: nothing started here, nothing to warn about
        self.assertEqual((out["started"], out["warning"], self.spawned), (False, "", []))

    def test_memory_pressure_is_named_instead_of_a_generic_warning(self):
        res = api.kbd.DispatchResult(memory_pressure="critical")
        with mock.patch.object(api.kbd, "start_task_once", return_value=res, create=True), \
                mock.patch.object(api.kbd, "dispatch_once", side_effect=no_tick):
            out = api.card_assign(self.card, "coder", None)
        self.assertFalse(out["started"])
        self.assertIn("critical memory pressure", out["warning"])

    def test_a_card_hermes_holds_back_says_why(self):
        res = api.kbd.DispatchResult(respawn_guarded=[(self.card, "recent_success")])
        with mock.patch.object(api.kbd, "start_task_once", return_value=res, create=True), \
                mock.patch.object(api.kbd, "dispatch_once", side_effect=no_tick):
            out = api.card_assign(self.card, "coder", None)
        self.assertFalse(out["started"])
        self.assertIn("recent_success", out["warning"])

    def test_a_spawn_that_blocks_the_card_says_so(self):
        res = api.kbd.DispatchResult(auto_blocked=[self.card])
        with mock.patch.object(api.kbd, "start_task_once", return_value=res, create=True), \
                mock.patch.object(api.kbd, "dispatch_once", side_effect=no_tick):
            out = api.card_assign(self.card, "coder", None)
        self.assertFalse(out["started"])
        self.assertIn("blocked the card", out["warning"])

    def test_an_older_hermes_ticks_and_names_every_other_card_it_started(self):
        other = self.ready_card("Also ready")
        res = api.kbd.DispatchResult(spawned=[(self.card, "coder", "/ws"), (other, "coder", "/ws")])
        with mock.patch.object(api.kbd, "start_task_once", None, create=True), \
                mock.patch.object(api.kbd, "dispatch_once", return_value=res) as tick:
            out = api.card_assign(self.card, "coder", None)
        tick.assert_called_once()
        self.assertEqual((out["started"], out["also_started"]), (True, [other]))
        self.assertIn(other, out["warning"])
        self.assertIn("whole board", out["warning"])

    def test_the_fallback_tick_reports_a_dead_dispatcher(self):
        with mock.patch.object(api.kbd, "start_task_once", None, create=True), \
                mock.patch.object(api.kbd, "dispatch_once", return_value=api.kbd.DispatchResult()), \
                mock.patch("hermes_cli.kanban._check_dispatcher_presence", return_value=(False, "No gateway is running")):
            out = api.card_assign(self.card, "coder", None)
        self.assertEqual((out["started"], out["warning"]), (False, "No gateway is running"))

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
        # the start itself is a separate concern (and a separate test): stub both ways to start
        # nothing, so this test is only about the reason, the reply and the card's status
        with mock.patch.object(api.kbd, "start_task_once", return_value=api.kbd.DispatchResult(), create=True), \
                mock.patch.object(api.kbd, "dispatch_once", return_value=api.kbd.DispatchResult()):
            out = api.card_unblock(self.card, "Go ahead, restart now")
        self.assertEqual(out["status"], "ready")
        with connect_closing(api._KANBAN_DB) as k:
            c = kanban_db.list_comments(k, self.card)[-1]
        self.assertEqual((c.author, c.body), ("dashboard", "Go ahead, restart now"))

    @unittest.skipUnless(hasattr(api.kbd, "start_task_once"), "this Hermes predates start_task_once")
    def test_unblocking_starts_that_card_and_nothing_else(self):
        other = self.ready_card("Held back")
        with connect_closing(api._KANBAN_DB) as k:
            kanban_db.assign_task(k, self.card, "coder")
            kanban_db.block_task(k, self.card, reason="need a person")
        with mock.patch.object(api.kbd, "dispatch_once", side_effect=no_tick):
            out = api.card_unblock(self.card, "go ahead")
        self.assertEqual((out["started"], self.spawned), (True, [(self.card, str(self.ws))]))
        self.assertEqual(self.status(), ("running", "coder"))
        self.assertEqual(self.status(other), ("ready", None))

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
