"""The Overview's Today strip (plugin.js TodayStrip): each mark's drawn hover text and the drawer
a click opens, with links to where the mark shows."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
TIP = JS[JS.index("// The host shows no native tooltips"):JS.index("const drawerTitle")]
STRIP = JS[JS.index("// Today from midnight to midnight"):JS.index("// What the tag on an attention card says")]
# a hook store that keeps state across renders, and a walk over the rendered tree
HARNESS = """
const TONE = { bad: 'bad', warn: 'warn', good: 'good', info: 'info', purple: 'purple' }, MONO = 'mono', DIM = 'dim', BTN = {}
const jsx = (t, p) => ({ t, p }), jsxs = jsx, Drawer = () => null, GoLink = () => null, drawerTitle = x => x, drawerPart = (...a) => a
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`, fmtClock = ts => `t${ts - day0}`, fmtDur = s => `${Math.round(s)}s`
let st = [], k = 0
const useState = v => { const i = k++; if (!(i in st)) st[i] = v; return [st[i], x => { st[i] = x }] }
const day0 = Math.floor(Date.now() / 1000) - 3 * 3600, window = { innerWidth: 1000 }
const at = { currentTarget: { getBoundingClientRect: () => ({ left: 240, width: 20, bottom: 300 }) } }
%s
%s
const all = (n, out = []) => { if (Array.isArray(n)) n.forEach(c => all(c, out)); else if (n?.p) { out.push(n); all(n.p.children, out) } return out }
const hours = Array.from({ length: 24 }, (_, i) => ({ ts: day0 + i * 3600, requests: i ? 0 : 5, errors: i ? 0 : 1, tasks_done: i ? 0 : 1, tasks_failed: 0 }))
const props = { hours,
  changes: [{ ts: day0 + 600, kind: 'merged', text: 'Fix x', where: 'g/ops', by: 'cp', url: 'https://mr' },
            ...[1, 2, 3, 4].map(i => ({ ts: day0 + 3600 + i * 60, kind: 'model', text: `m${i} loaded`, where: 'Mini' }))],
  incidents: [{ key: 'host:mini', source: 'host', sev: 'warn', text: 'mini quiet', action: 'check it', ts: day0 + 60 },
              { key: 'gateway:default:discord:fatal', source: 'gateway', sev: 'crit', text: 'default:discord is fatal', host: 'mbp', ts: day0 + 120 }] }
const render = () => { k = 0; return all(TodayStrip(props)) }
const text = n => typeof n === 'string' ? n : Array.isArray(n) ? n.filter(c => c != null).map(text).join('|') : n?.p ? text(n.p.children) : ''
const buttons = render().filter(n => n.t === 'button')
const change = buttons.find(b => b.p['aria-label'] === 't600: Merged: Fix x')
change.p.onMouseEnter(at)
const shown = render().find(n => n.p.role === 'tooltip'), tip = text(shown)
change.p.onClick()
const drawn = render()
const changeItem = drawn.find(x => x.t === StripDrawer).p.item
buttons.find(b => b.p['aria-label'] === 'since t60: mini quiet').p.onClick()
const incident = render().find(x => x.t === StripDrawer).p.item
buttons.find(b => b.p['aria-label'] === 'since t120: default:discord is fatal').p.onClick()
const gateway = render().find(x => x.t === StripDrawer).p.item
buttons[0].p.onClick()
const hour = render().find(x => x.t === StripDrawer).p.item
buttons.find(b => b.p['aria-label'] === 't3660: Model: m1 loaded').p.onClick()
const model = render().find(x => x.t === StripDrawer).p.item
buttons[1].p.onMouseEnter(at)
const busy = text(render().find(n => n.p.role === 'tooltip'))
console.log(JSON.stringify({ count: buttons.length, tip, place: [shown.p.style.left, shown.p.style.top], hidden: !drawn.some(n => n.p.role === 'tooltip'),
  change: [changeItem.go, changeItem.url, changeItem.lines], incident: [incident.go, incident.lines], hour: [hour.title, hour.lines, hour.go, hour.changes.map(c => c.text), hour.errorsFrom - day0], model: model.go, gateway: gateway.go, busy }))
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TodayStripTest(unittest.TestCase):
    def test_marks_say_what_they_are_and_open_details_with_links(self):
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", HARNESS % (TIP, STRIP)],
                                        capture_output=True, text=True, check=True).stdout)
        # 24 hours of requests, two incidents, one hour with errors, one with tasks, one change
        self.assertEqual(out["count"], 33)
        # a gateway's platform can hold a colon; the map turns g:host:platform into its entry point
        self.assertEqual([g[2] for g in out["gateway"]], ["g:mbp:default:discord", "g:mbp:default:discord"])
        self.assertEqual(out["tip"], "t600|Merged: Fix x|g/ops, merged by cp|click for details")
        # under the mark, on the window
        self.assertEqual(out["place"], ["25%", 306])
        # the click closes the tip and opens the drawer
        self.assertTrue(out["hidden"])
        # a merged MR is not on the map, so it leads only to Merge requests
        self.assertEqual(out["change"], [[["See merge requests", "mrs"]],
                                         "https://mr", ["g/ops, merged by cp"]])
        go, lines = out["incident"]
        self.assertEqual(go[0], ["Show the host on the map", "topology", "h:mini"])
        self.assertEqual(go[1][:3], ["See the map when it started", "topology", "h:mini"])
        self.assertEqual(lines[1:], ["What to do: check it", "from host"])
        title, lines, go, changes, errors_from = out["hour"]
        # the drawer loads the hour's errors from its start
        self.assertEqual(errors_from, 0)
        # a model change selects its device on the map
        self.assertEqual(out["model"][0][:3], ["See the map at that time", "topology", "s:Mini"])
        self.assertEqual(title, "5 requests, 1 error")
        self.assertEqual((lines, changes), (["1 tasks done, 0 failed"], ["Fix x"]))
        self.assertEqual([g[1] for g in go], ["topology", "flow", "trace", "handoffs"])
        # the map of just the hour as it ended; Flow, Trace and Handoffs over just that hour
        self.assertEqual(go[0][3], {"at": go[1][3]["start"] + 3600, "start": go[1][3]["start"]})
        self.assertEqual(go[1][3], go[2][3])
        self.assertEqual(go[1][3], go[3][3])
        self.assertEqual(go[1][3]["hours"], 1)
        # no tasks line for an hour without tasks, and the tip counts the hour's changes
        self.assertEqual(out["busy"], "t3600 to t7200|0 requests, 0 errors|4 changes|click for details")


ERRORS = """
const TONE = { bad: 'bad' }, MONO = 'mono', DIM = 'dim', Skeleton = 'Skeleton', GoLink = 'GoLink', KindBox = 'KindBox'
const jsx = (t, p) => ({ t, p }), jsxs = jsx, drawerText = (k, t) => t, drawerPart = (k, label, body) => ({ t: 'part', p: { label, children: body } })
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`, fmtClock = ts => `t${ts}`
const useErrors = (hours, start) => ({ data: { groups: [
  { count: 14, host: 'mbp', platform: 'subagent', profile: 'default', model: 'glm', served: 'custom', event: 'api_request_error', error: 'Invalid model', first: 10, last: 20, sessions: ['s2', 's1'] },
  { count: 11, host: 'mbp', platform: null, profile: null, model: 'glm', served: null, event: 'router_request', error: null, first: 15, last: 15, sessions: [] }], errors: {} } })
%s
const all = (n, out = []) => { if (Array.isArray(n)) n.forEach(c => all(c, out)); else if (n?.p) { out.push(n); all(n.p.children, out) } return out }
const text = n => typeof n === 'string' ? n : Array.isArray(n) ? n.filter(c => c != null).map(text).join('|') : n?.p ? text(n.p.children) : ''
const part = HourErrors({ start: 0 })
const groups = part.p.children.p.children
console.log(JSON.stringify({ label: part.p.label, texts: groups.map(text),
  links: groups.map(g => all(g).filter(n => n.t === 'GoLink').map(n => [n.p.tab, n.p.sel, n.p.start ?? null, n.p.hours ?? null, n.p.at ?? null])) }))
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HourErrorsTest(unittest.TestCase):
    def test_each_error_says_what_failed_where_and_links_there_over_the_hour(self):
        fn = JS[JS.index("function HourErrors"):JS.index("// What the tag on an attention card says")]
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", ERRORS % fn],
                                        capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out["label"], "Errors this hour (25)")
        self.assertEqual(out["texts"], [
            "14 errors|subagent calls on mbp|t10 to t20|Invalid model|profile default · model glm · served by custom · 2 sessions"
            "|Open the newest of 2 sessions in Trace|Show the model in Flow|Show default on mbp on the map",
            "11 errors|router on mbp|t15|router_request failed|model glm · no session|Show the host on the map"])
        self.assertEqual(out["links"], [
            # the map has no host node: a client's errors select their profile there, over the hour
            [["trace", "s2", 0, 1, None], ["flow", "model:glm", 0, 1, None], ["topology", "p:mbp:default", 0, None, 3600]],
            # the router's own errors have no session and are not in Flow
            [["topology", "h:mbp", 0, None, 3600]]])


if __name__ == "__main__":
    unittest.main()
