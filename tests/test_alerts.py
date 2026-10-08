"""The Desktop alert poll (plugin.js startAlerts), run under node with a stand-in ctx and host."""
import importlib.util
import json
import shutil
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
ALERTS = JS[JS.index("// --- alerts"):JS.index("// --- registration")]

HARNESS = """
const PATH = '/operations'
const toasts = [], os = [], store = %s
let tick = null
const host = { notify: n => toasts.push(n), navigate: () => {} }
globalThis.setInterval = fn => { tick = fn; return 1 }
%s
const polls = %s
const ctx = {
  rest: async () => { const p = polls.shift(); if (p === 'down') throw new Error('down'); return p },
  storage: { get: (k, d) => (k in store ? store[k] : d), set: (k, v) => { store[k] = v } },
  os: { notify: n => os.push(n) },
  onDispose: () => {}
}
const flush = () => new Promise(r => setTimeout(r, 0))
const steps = []
startAlerts(ctx)
await flush()
while (polls.length) { const before = toasts.length; await tick(); await flush(); steps.push(toasts.slice(before).map(t => t.message)) }
console.log(JSON.stringify({ steps, os: os.map(n => n.title), seen: store['alerts.seen'], log: (store['alerts.log'] || []).map(a => a.open ? `open: ${a.text}` : a.text) }))
"""


def item(key, text, source="host", sev="warn"):
    return {"key": key, "text": text, "source": source, "sev": sev, "action": "check it"}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class AlertsTest(unittest.TestCase):
    def run_polls(self, polls, store=None):
        out = subprocess.run(["node", "--input-type=module", "-e", HARNESS % (json.dumps(store or {}), ALERTS, json.dumps(polls))],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_first_poll_records_then_alerts_only_new_items(self):
        a, b = item("host:mbp", "mbp has sent no host stats for 5 min"), item("gateway:x:fatal", "x is fatal", "gateway", "crit")
        out = self.run_polls([
            {"items": [a]},
            {"items": [dict(a, text="mbp has sent no host stats for 6 min"), b]},
            {"items": [b, item("kanban:t1", "card is waiting on you", "kanban")]},
            {"items": [a, b]},
        ])
        # poll 1 baselines; 2 alerts only b (a's text changed, key did not); 3 skips kanban; a came back in 4
        self.assertEqual(out["steps"], [["x is fatal"], [], ["mbp has sent no host stats for 5 min"]])
        self.assertEqual(out["os"], ["Operations: x is fatal", "Operations: mbp has sent no host stats for 5 min"])
        # the Notifications tab's log, newest first, starting with what was open at the first poll
        self.assertEqual(out["log"], ["mbp has sent no host stats for 5 min", "x is fatal", "open: mbp has sent no host stats for 5 min"])

    def test_an_update_lists_what_is_already_open_without_toasting(self):
        a, b = item("host:mbp", "mbp stale"), item("gateway:x:fatal", "x is fatal", "gateway", "crit")
        out = self.run_polls([{"items": [a, b]}, {"items": [a, b]}], store={"alerts.seen": ["host:mbp"]})
        self.assertEqual(out["steps"], [[]])
        self.assertEqual(out["log"], ["x is fatal", "open: mbp stale"])

    def test_an_unreadable_source_does_not_forget_or_re_alert(self):
        a = item("host:mbp", "mbp stale")
        out = self.run_polls([{"items": [a]}, {"items": [], "errors": {"metrics": "locked"}}, "down", {"items": [a]}])
        self.assertEqual(out["steps"], [[], [], []])
        self.assertEqual(out["seen"], ["host:mbp"])

    def test_many_new_items_make_one_summary(self):
        out = self.run_polls([{"items": []}, {"items": [item(f"host:h{n}", f"h{n} stale") for n in range(5)]}])
        self.assertEqual(out["steps"], [["5 new items need attention"]])
        self.assertEqual(len(out["os"]), 1)


NOTIFY = JS[JS.index("// --- notifications"):JS.index("function NotificationsPage")]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UnreadTest(unittest.TestCase):
    def test_unread_until_marked_one_at_a_time_or_all(self):
        js = """
const ALERT_LOG = 'alerts.log', ALERT_READ = 'alerts.read'
const store = { 'alerts.log': [{ key: 'a', at: 300 }, { key: 'b', at: 200, read: true }, { key: 'c', at: 100 }], 'alerts.read': 150 }
const api = { storage: { get: (k, d) => (k in store ? store[k] : d) } }
%s
const before = unreadAlerts()
store['alerts.read'] = 400
console.log(JSON.stringify([before, unreadAlerts()]))
""" % NOTIFY
        out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, check=True)
        # a is unread; b was marked read; c is older than the last Mark all read; then all are read
        self.assertEqual(json.loads(out.stdout), [1, 0])


spec = importlib.util.spec_from_file_location("operations_plugin", Path(__file__).resolve().parents[1] / "plugins" / "operations" / "__init__.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class DiscordAlertsTest(unittest.TestCase):
    """The gateway-side copy of the poll (__init__.py _alert), with a stand-in ctx, route and sender."""

    def run_ticks(self, polls, target="discord"):
        sent, store = [], {}
        ctx = types.SimpleNamespace(get_config=lambda k: target,
                                    state=types.SimpleNamespace(get=store.get, set=store.__setitem__))
        sender = types.ModuleType("tools.send_message_tool")
        sender.send_message_tool = lambda a: (sent.append((a["target"], a["message"])), '{"success": true}')[1]
        api = types.SimpleNamespace(attention_route=lambda: polls.pop(0))
        with mock.patch.object(hook, "_api", api), mock.patch.dict(sys.modules, {"tools.send_message_tool": sender}):
            for _ in list(polls):
                store["alerts.at"] = 0
                hook._alert(ctx)
        return sent, store

    def test_posts_only_new_non_kanban_items_once(self):
        a, b = item("gateway:x:fatal", "x is fatal", "gateway", "crit"), item("host:mbp", "mbp quiet")
        b["action"] = "check its metrics forwarder"
        sent, store = self.run_ticks([{"items": [a]}, {"items": [a, b, item("kanban:t1", "card", "kanban")]}, {"items": [a, b]}])
        self.assertEqual(sent, [("discord", "Operations: mbp quiet (check its metrics forwarder)")])
        self.assertEqual(store["alerts.seen"], ["gateway:x:fatal", "host:mbp"])

    def test_unreadable_source_keeps_seen_and_many_items_summarise(self):
        a = item("host:mbp", "mbp quiet")
        sent, _ = self.run_ticks([{"items": [a]}, {"items": [], "errors": {"metrics": "locked"}}, {"items": [a]},
                                  {"items": [a] + [item(f"host:h{n}", f"h{n} quiet") for n in range(4)]}])
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1].startswith("Operations: 4 new items need attention\n- h0 quiet"))

    def test_a_failed_send_is_tried_again_on_the_next_poll(self):
        a = item("host:mbp", "mbp quiet")
        for fail in (lambda a: '{"error": "discord down"}', mock.Mock(side_effect=RuntimeError("no adapter"))):
            store = {"alerts.seen": []}
            ctx = types.SimpleNamespace(get_config=lambda k: "discord",
                                        state=types.SimpleNamespace(get=store.get, set=store.__setitem__))
            sender = types.ModuleType("tools.send_message_tool")
            sender.send_message_tool = fail
            api = types.SimpleNamespace(attention_route=lambda: {"items": [a]})
            with mock.patch.object(hook, "_api", api), mock.patch.dict(sys.modules, {"tools.send_message_tool": sender}):
                hook._alert(ctx)
            self.assertEqual(store["alerts.seen"], [])

    def test_off_without_a_target_and_polls_once_a_minute(self):
        sent, store = self.run_ticks([{"items": []}, {"items": [item("host:mbp", "mbp quiet")]}], target=None)
        self.assertEqual((sent, "alerts.seen" in store), ([], False))
        ctx = types.SimpleNamespace(get_config=lambda k: "discord", state=types.SimpleNamespace(
            get=lambda k, d=None: time.time() if k == "alerts.at" else d, set=self.fail))
        hook._alert(ctx)


if __name__ == "__main__":
    unittest.main()
