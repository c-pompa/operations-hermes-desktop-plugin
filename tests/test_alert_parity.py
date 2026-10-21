"""The alert rules exist twice: desktop/plugin.js startAlerts while Desktop runs, and __init__.py
new_alerts on the gateway's dispatcher tick (Discord, Desktop closed). They have to agree on which
items count, which of them are new, and what an unreadable source means — otherwise one path alerts
where the other is silent, and the plugin "notifies twice" or "not at all" depending on which
machine happens to be awake.

One payload sequence goes through both here: the JavaScript under node, the Python directly.
"""
import importlib.util
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "operations"
JS = (ROOT / "desktop" / "plugin.js").read_text()
ALERTS = JS[JS.index("// --- alerts"):JS.index("// --- registration")]

spec = importlib.util.spec_from_file_location("operations_plugin", ROOT / "__init__.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

HARNESS = """
const PATH = '/operations'
const store = {}, toasts = []
let tick = null
const host = { notify: n => toasts.push(n.title === 'Operations' ? n.message : n.message), navigate: () => {} }
globalThis.setInterval = fn => { tick = fn; return 1 }
%s
const polls = %s
const ctx = {
  rest: async () => (polls.length ? polls.shift() : { items: [] }),
  storage: { get: (k, d) => (k in store ? store[k] : d), set: (k, v) => { store[k] = v } },
  os: { notify: () => {} },
  onDispose: () => {}
}
const flush = () => new Promise(r => setTimeout(r, 0))
// each round reports the alerts that round raised, not every alert so far
let told = 0
const since = () => { const next = toasts.slice(told); told = toasts.length; return next }
// the first poll runs as startAlerts registers, so it covers the first payload
const rounds = polls.length - 1
startAlerts(ctx)
await flush()
const out = [{ seen: store['alerts.seen'] || null, alerts: since() }]
for (let i = 0; i < rounds; i += 1) {
  await tick()
  await flush()
  out.push({ seen: store['alerts.seen'] || null, alerts: since() })
}
console.log(JSON.stringify(out))
"""


def item(key, text, source="host", sev="warn"):
    return {"key": key, "text": text, "source": source, "sev": sev, "action": "check it"}


# The sequence both paths see. It exercises: a baseline poll, a new item, a changed text on a known
# key, a kanban card (left to the Kanban plugin), an item with no key (ignored), and a source that
# cannot be read (its items are missing, not cleared).
PAYLOADS = [
    {"items": [item("host:mbp", "mbp has sent no host stats for 5 min")]},
    {"items": [item("host:mbp", "mbp has sent no host stats for 6 min"), item("gateway:x:fatal", "x is fatal", "gateway", "crit")]},
    {"items": [item("gateway:x:fatal", "x is fatal", "gateway", "crit"),
               item("kanban:t1", "card is waiting on you", "kanban"),
               {"text": "no key at all", "source": "host", "sev": "warn"}]},
    {"items": [], "errors": {"metrics": "locked"}},
    {"items": [item("host:mbp", "mbp has sent no host stats for 8 min"), item("gateway:x:fatal", "x is fatal", "gateway", "crit")]},
]


def js_rounds():
    out = subprocess.run(["node", "--input-type=module", "-e", HARNESS % (ALERTS, json.dumps(PAYLOADS))],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def py_rounds():
    seen, rounds = None, []
    for payload in PAYLOADS:
        fresh, seen = hook.new_alerts(payload, seen)
        rounds.append({"seen": seen, "fresh": fresh})
    return rounds


@unittest.skipUnless(shutil.which("node"), "node not installed")
class AlertParityTest(unittest.TestCase):
    def test_both_paths_remember_the_same_keys(self):
        js, py = js_rounds(), py_rounds()
        self.assertEqual([r["seen"] for r in js], [r["seen"] for r in py])

    def test_both_paths_alert_on_the_same_items(self):
        js, py = js_rounds(), py_rounds()
        for n, (a, b) in enumerate(zip(js, py)):
            with self.subTest(round=n):
                # the desktop side writes one toast per item with the item's text as the message
                # (a summary line instead when more than three arrive at once, which this sequence
                # never does), and the gateway side builds its Discord message from the same items
                self.assertEqual(sorted(a["alerts"]), sorted(i["text"] for i in b["fresh"]))

    def test_a_source_that_cannot_be_read_forgets_nothing_on_either_side(self):
        js, py = js_rounds(), py_rounds()
        # PAYLOADS[3] lost the metrics source: its items are missing, not cleared, so both keep the
        # keys they had and neither re-alerts (an item that "returns" must not look new again)
        self.assertEqual((js[3]["alerts"], py[3]["fresh"]), ([], []))
        self.assertEqual(js[3]["seen"], js[2]["seen"])
        self.assertEqual(js[3]["seen"], py[3]["seen"])

    def test_the_payloads_cover_the_cases_that_matter(self):
        # guards the fixture itself: a kanban item, a keyless item and an unreadable source are in it
        items = [i for p in PAYLOADS for i in p["items"]]
        self.assertTrue(any(i.get("source") == "kanban" for i in items))
        self.assertTrue(any(not i.get("key") for i in items))
        self.assertTrue(any(p.get("errors") for p in PAYLOADS))

    def test_the_two_comments_still_promise_the_same_rules(self):
        # a cheap tripwire: new_alerts' docstring names startAlerts as its twin. If someone adds a
        # third alert path, this test is where they land.
        self.assertIn("plugin.js startAlerts in Python", hook.new_alerts.__doc__)
        self.assertIn("function startAlerts", JS)


if __name__ == "__main__":
    unittest.main()
