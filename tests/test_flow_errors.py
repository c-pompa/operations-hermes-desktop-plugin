"""Flow's selection: the errors behind its failed count (FlowErrors) and its failed share per hour (FlowHourly)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
STUBS = """
const jsx = (t, p) => ({ t, p }), jsxs = jsx, muted = t => ({ t: 'muted', p: { children: t } }), Skeleton = 'Skeleton'
const TONE = { bad: 'red' }, DIM = 'dim'
const errorRows = (groups, when) => groups.map(g => ({ t: 'row', p: { g, when } }))
const all = (n, out = []) => { if (Array.isArray(n)) n.forEach(c => all(c, out)); else if (n?.p) { out.push(n); all(n.p.children, out) } return out }
"""

ERRORS = STUBS + """
const useErrors = (hours, start) => ({ data: { groups: [
  { platform: 'desktop', profile: 'default', model: 'qwen', served: 'lmstudio', error: 'Connection error.' },
  { platform: null, profile: null, model: 'qwen', served: null, error: 'http_400' },
  { platform: 'cron', profile: null, model: 'glm', served: null, error: 'timeout' }], errors: {} } })
%s
const rows = node => all(FlowErrors({ node, hours: 6, start: null })).filter(n => n.t === 'row').map(n => [n.p.g.error, n.p.when])
console.log(JSON.stringify({ model: rows({ col: 'model', label: 'qwen' }), served: rows({ col: 'served', label: 'unknown' }),
                             profile: rows({ col: 'profile', label: 'no profile' }), none: rows({ col: 'entry', label: 'cli' }) }))
"""

HOURLY = STUBS + """
%s
const paths = [{ ids: ['entry:desktop', 'profile:default', 'model:qwen', 'served:lmstudio'] },
               { ids: ['entry:cli', 'profile:default', 'model:glm', 'served:nous'] }]
const served = [{ id: 'served:lmstudio', label: 'lmstudio' }, { id: 'served:nous', label: 'nous' }]
const hourly = [{ ts: 0, by: { 'served:lmstudio': 3, 'served:nous': 1 }, paths: { 0: 3, 1: 1 }, failed: { 0: 2 } }]
const out = FlowHourly({ hourly, paths, served, sel: 'model:qwen', color: id => id })
const bar = out.p.children[0].p.children[0]
console.log(JSON.stringify({ bar: bar.p.children.filter(Boolean).map(c => [c.p.style.backgroundColor ?? 'count', c.p.style.height ?? c.p.children]),
                             tip: bar.p.title.split('\\n').slice(1), legend: all(out.p.children[2]).map(n => n.p.children).filter(c => typeof c === 'string' || Array.isArray(c)).flat().filter(c => typeof c === 'string') }))
"""


def run(template, start, end):
    fn = JS[JS.index(start):JS.index(end)]
    return json.loads(subprocess.run(["node", "--input-type=module", "-e", template % fn],
                                     capture_output=True, text=True, check=True).stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FlowErrorsTest(unittest.TestCase):
    def test_lists_a_clients_errors_on_the_selection_over_its_window(self):
        out = run(ERRORS, "// What failed among the selection's", "function FlowHourly")
        when = {"hours": 6, "start": None}
        # the router's own error on the same model is not in Flow, so it is not listed
        self.assertEqual(out["model"], [["Connection error.", when]])
        # a missing value matches the label Flow gives it
        self.assertEqual(out["served"], [["timeout", when]])
        self.assertEqual(out["profile"], [["timeout", when]])
        self.assertEqual(out["none"], [])

    def test_failed_requests_draw_red_over_what_served_the_rest(self):
        out = run(HOURLY, "function FlowHourly", "function FlowPage")
        # 3 through qwen, 2 of them failed: a red 2 over lmstudio's 1, scaled to the busiest hour (3)
        self.assertEqual(out["bar"], [["count", 3], ["red", f"{2 / 3 * 100}%"], ["served:lmstudio", f"{1 / 3 * 100}%"]])
        self.assertEqual(out["tip"], ["lmstudio 3, 2 failed"])
        self.assertIn("failed", out["legend"])


if __name__ == "__main__":
    unittest.main()
