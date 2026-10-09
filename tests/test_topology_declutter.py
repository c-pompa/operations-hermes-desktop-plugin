"""Topology chrome from the style preview: chips, a collapsed change sheet, a collapsed roles sheet."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
BLAST = JS[JS.index("const STATE_COLOR"):JS.index("// The map as nodes in four tiers")]
LAYOUT = (BLAST + "const TONE = new Proxy({}, { get: (_, k) => k }), jsx = (t, p) => ({ key: p?.key }), jsxs = jsx\n"
          + JS[JS.index("// The map as nodes in four tiers"):JS.index("function TopologyMap")])
CHROME = JS[JS.index("const CHANGE_KIND"):JS.index("function TopologyChanges")]
CHANGES = JS[JS.index("function TopologyChanges"):JS.index("// The fifteen layouts")]


def run(script):
    return json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                     capture_output=True, text=True, check=True).stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TopologyDeclutterTest(unittest.TestCase):
    def test_chrome_chips_split_issues_fallback_drift_and_roles(self):
        script = (
            LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`\n"
            + CHROME
            + """
const issues = [
  { key: 'g:mini:discord', tone: 'bad', quiet: false, text: 'discord on mini fatal' },
  { key: 'g:mini:api', tone: 'bad', quiet: false, text: 'api on mini stale' },
  { key: 'r:hermes/light+hermes/triage+hermes/vision+hermes/watcher', tone: 'warn', quiet: false, roles: 4, to: 'gpu-box',
    text: '4 roles on fallback to gpu-box (light, triage, vision, watcher), 25 requests' },
  { key: 'r:hermes/coder', tone: 'warn', quiet: false, roles: 1, to: 'Mini, Pomps',
    text: 'coder on fallback to Mini, Pomps, 3 requests' },
  { key: 'd:mbp', tone: 'drift', quiet: false, text: 'mbp drifted: jobs' },
  { key: 'r:hermes/idle', tone: 'muted', quiet: true, roles: 1, to: 'Mini', text: 'idle on fallback to Mini, 0 requests' }
]
const chips = chromeChips(issues, { roles: Array.from({ length: 11 }, (_, i) => ({ name: 'r' + i })) })
console.log(JSON.stringify(chips.map(c => [c.key, c.tone, c.label, c.items?.length ?? 0])))
"""
        )
        self.assertEqual(run(script), [
            ["issues", "bad", "2 issues", 2],
            ["r:hermes/light+hermes/triage+hermes/vision+hermes/watcher", "warn", "4 fallback · gpu-box", 1],
            ["r:hermes/coder", "warn", "1 fallback · Mini, Pomps", 1],
            ["drift", "drift", "1 drift", 1],
            ["roles", "info", "11 roles", 0]
        ])

    def test_change_pills_group_by_kind(self):
        script = (
            LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`\n"
            + CHROME
            + """
const changes = [
  { ts: 1, node: 'g:mini:a', tone: 'good', text: 'discord on mini connected' },
  { ts: 2, node: 'g:mini:b', tone: 'good', text: 'api on mini connected' },
  { ts: 3, node: 'g:mini:a', tone: 'bad', text: 'discord on mini went stale' },
  { ts: 4, node: 'g:mini:a', tone: 'info', text: 'mini gateway restarted', list: 'discord, api' },
  { ts: 5, node: 'g:mini:a', tone: 'info', text: 'mini gateway restarted' },
  { ts: 6, node: 's:Pomps', tone: 'warn', text: 'Pomps left the pool' },
  { ts: 7, node: 'd:mbp', tone: 'drift', text: 'mbp drifted: jobs' }
]
console.log(JSON.stringify(changePills(changes)))
"""
        )
        self.assertEqual(run(script), [
            [1, "gateway problem", "gateway problems", "bad"],
            [1, "device left the pool", "devices left the pool", "warn"],
            [1, "host drifted", "hosts drifted", "drift"],
            [2, "gateway restart", "gateway restarts", "info"],
            [2, "recovery", "recoveries", "good"]
        ])

    def test_roles_summary_counts_fallback_and_requests(self):
        script = (
            LAYOUT + "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`\n"
            + CHROME
            + """
const data = {
  router: { roles: [
    { name: 'hermes/main', resolved: 'big', candidates: [{ model: 'big' }] },
    { name: 'hermes/light', resolved: 'tiny', candidates: [{ model: 'big' }, { model: 'tiny' }] },
    { name: 'hermes/triage', resolved: 'tiny', candidates: [{ model: 'big' }, { model: 'tiny' }] }
  ] },
  timeline: { flow: [
    [1, 'mini', 'cli', 'coder', 'hermes/main', 'Mini', 0, 17, 0, 1],
    [1, 'mini', 'cli', 'coder', 'hermes/triage', 'Mini', 0, 25, 0, 1]
  ] }
}
console.log(JSON.stringify(rolesSummary(data)))
"""
        )
        self.assertEqual(run(script), {"n": 3, "fallback": 2, "requests": 42})

    def test_change_sheet_keeps_pills_until_opened(self):
        harness = (
            "const TONE = new Proxy({}, { get: (_, k) => k }), DRIFT = 'drift', jsx = (t, p) => ({ t, p, key: p?.key }), jsxs = jsx\n"
            "const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`, muted = x => x, haptic = () => {}\n"
            "const cn = (...a) => a.filter(Boolean).join(' '), GoLink = (p) => ({ t: 'a', p })\n"
            + JS[JS.index("// A gateway restart takes"):JS.index("// What changed on the map")]
            + "\nlet st = [], k = 0\n"
            "const useState = v => { const i = k++; if (!(i in st)) st[i] = typeof v === 'function' ? v() : v; return [st[i], x => { st[i] = typeof x === 'function' ? x(st[i]) : x }] }\n"
            + CHROME + CHANGES
            + """
const all = (n, out = []) => { if (Array.isArray(n)) n.forEach(c => all(c, out)); else if (n?.p) { out.push(n); all(n.p.children, out) } return out }
const text = n => typeof n === 'string' ? n : Array.isArray(n) ? n.filter(c => c != null).map(text).join('|') : n?.p ? text(n.p.children) : ''
const data = { edges: [{ served: 'Mini', requests: 90 }, { served: 'cloud', requests: 10 }],
  timeline: { more_changes: 0, changes: [
    { ts: 100, node: 'g:mini:a', tone: 'info', text: 'mini gateway restarted', list: 'discord, api' },
    { ts: 90, node: 'g:mini:a', tone: 'good', text: 'discord on mini connected' },
    { ts: 80, node: 'd:mbp', tone: 'drift', text: 'mbp drifted: jobs' }
  ], flow: [[90, 'mini', 'cli', 'coder', 'main', 'Mini', 0, 90, 0, 10]] } }
const render = () => { k = 0; return all(TopologyChanges({ data, at: null, fmt: ts => `t${ts}`, hours: 24, start: null, onPick: () => {} })) }
const closed = render()
closed.find(n => n.t === 'button' && n.p['aria-expanded'] === false).p.onClick()
const opened = render()
console.log(JSON.stringify({
  closed: text(closed),
  opened: text(opened),
  events: {
    closed: closed.filter(n => n.t === 'button' && /gateway restarted|on mini connected|mbp drifted/.test(text(n))).length,
    opened: opened.filter(n => n.t === 'button' && /gateway restarted|on mini connected|mbp drifted/.test(text(n))).length
  }
}))
"""
        )
        out = run(harness)
        self.assertIn("3 changes", out["closed"])
        self.assertIn("1 gateway restart", out["closed"])
        self.assertIn("1 recovery", out["closed"])
        self.assertIn("1 host drifted", out["closed"])
        self.assertNotIn("served 90%", out["closed"])
        self.assertEqual(out["events"]["closed"], 0)
        self.assertIn("served 90%", out["opened"])
        self.assertEqual(out["events"]["opened"], 3)


class ViewMenuTest(unittest.TestCase):
    def test_width_is_inline_since_desktop_has_no_arbitrary_width_classes(self):
        self.assertNotIn("w-[", JS)
        self.assertIn("width: 'min(26rem, calc(100vw - 2rem))'", JS)
