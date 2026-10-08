import importlib.util
import json
import sys
import tempfile
import time
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
spec = importlib.util.spec_from_file_location(
    "operations_plugin", ROOT / "plugins" / "operations" / "__init__.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = api._KANBAN_DB
        api._KANBAN_DB = Path(self.tmp.name) / "kanban.db"
        with connect_closing(api._KANBAN_DB) as k:
            self.card = kanban_db.create_task(k, title="Later card", created_by="cloud")
        profiles = mock.patch.object(api.kanban_db, "list_profiles_on_disk", return_value=["default", "coder"])
        profiles.start()
        self.addCleanup(profiles.stop)

    def tearDown(self):
        api._KANBAN_DB = self.saved
        self.tmp.cleanup()

    def assign(self, start_at):
        with mock.patch.object(api.kbd, "dispatch_once", return_value=api.kbd.DispatchResult()) as tick, \
                mock.patch("hermes_cli.kanban._check_dispatcher_presence", return_value=(True, "")):
            return api.card_assign(self.card, "coder", start_at), tick.call_count

    def task(self):
        with connect_closing(api._KANBAN_DB) as k:
            return kanban_db.get_task(k, self.card)

    def test_a_later_start_parks_the_card_and_the_hook_reads_the_time_back(self):
        at = int(time.time()) + 3600
        out, ticks = self.assign(at)
        self.assertEqual((out["status"], out["starts_at"], out["started"], ticks), ("scheduled", at, False, 0))
        self.assertEqual((self.task().status, self.task().assignee), ("scheduled", "coder"))
        with connect_closing(api._KANBAN_DB) as k:
            self.assertEqual((hook.start_at(k, self.card), api._start_at(k, self.card)), (at, at))
        with mock.patch.object(api, "_METRICS_DB", Path(self.tmp.name) / "metrics.db"):
            items = api.handoffs_route(24)["items"]
        card = next(i for i in items if i.get("id") == self.card)
        self.assertEqual(card["starts_at"], at)

    def test_release_due_frees_only_cards_whose_time_has_come(self):
        at = int(time.time()) + 3600
        self.assign(at)
        with connect_closing(api._KANBAN_DB) as k:
            self.assertEqual(hook.release_due(k, at - 1), [])
            self.assertEqual(hook.release_due(k, at), [self.card])
        self.assertEqual(self.task().status, "ready")

    def test_a_card_scheduled_elsewhere_is_left_alone(self):
        with connect_closing(api._KANBAN_DB) as k:
            kanban_db.schedule_task(k, self.card, reason="waiting on a vendor")
            self.assertEqual(hook.release_due(k, time.time() + 10 ** 9), [])

    def test_assigning_a_scheduled_card_without_a_time_starts_it_now(self):
        self.assign(int(time.time()) + 3600)
        out, ticks = self.assign(None)
        self.assertEqual((out["status"], ticks), ("ready", 1))

    def test_rescheduling_moves_the_start(self):
        self.assign(int(time.time()) + 3600)
        later = int(time.time()) + 7200
        self.assertEqual(self.assign(later)[0]["starts_at"], later)
        with connect_closing(api._KANBAN_DB) as k:
            self.assertEqual(hook.start_at(k, self.card), later)

    def test_a_card_claimed_while_rescheduling_is_left_running(self):
        self.assign(int(time.time()) + 3600)
        unblock = kanban_db.unblock_task

        def claimed(k, task_id):  # a dispatcher claims it between the release and the new schedule
            ok = unblock(k, task_id)
            k.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            k.commit()
            return ok
        with mock.patch.object(api.kanban_db, "unblock_task", claimed), self.assertRaises(HTTPException) as cm:
            self.assign(int(time.time()) + 7200)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("is running, not scheduled", cm.exception.detail)
        self.assertEqual(self.task().status, "running")

    def test_a_blocked_card_can_be_scheduled_for_later(self):
        with connect_closing(api._KANBAN_DB) as k:
            kanban_db.block_task(k, self.card, reason="needs a person")
        self.assertEqual(self.assign(int(time.time()) + 3600)[0]["status"], "scheduled")

    def test_tick_hook_skips_dry_runs_and_releases_on_real_ticks(self):
        self.assign(int(time.time()) + 3600)
        with mock.patch.object(hook, "connect_closing") as conn:
            hook._on_tick(board=None, dry_run=True)
            conn.assert_not_called()
        with mock.patch.object(hook, "connect_closing", lambda board=None: connect_closing(api._KANBAN_DB)), \
                mock.patch.object(hook.time, "time", return_value=time.time() + 7200):
            hook._on_tick(board=None, dry_run=False)
        self.assertEqual(self.task().status, "ready")

    def test_register_wires_the_dispatch_tick_hook(self):
        ctx = mock.Mock()
        hook.register(ctx)
        ctx.register_hook.assert_any_call("on_kanban_dispatch_tick", hook._on_tick)


if __name__ == "__main__":
    unittest.main()
