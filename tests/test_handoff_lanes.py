"""The Handoffs lane chart (plugin.js HandoffLanes), rendered under node with jsx stubbed to plain objects."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
LANES = JS[JS.index("const HKIND"):JS.index("// Hand an open card")]
STUBS = """
const TONE = { purple: 'purple', muted: 'muted' }, OUTCOME_COLOR = {}, MONO = 'mono'
const haptic = () => {}, fmtClock = t => t, dragScroll = null
const jsx = (type, props) => ({ type, props }), jsxs = jsx
const walk = (n, out = []) => {
  if (Array.isArray(n)) n.forEach(c => walk(c, out))
  else if (n && n.props) { out.push(n); walk(n.props.children, out) }
  return out
}
"""
NOW, HOUR = 1_000_000, 3600


class HandoffLanesTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_open_card_from_before_the_window_is_clipped_to_it(self):
        since = NOW - 6 * HOUR
        items = [
            {"id": "old", "kind": "card", "from": "claude-code", "to": "backend-eng", "title": "old", "open": True,
             "status": "ready", "start": NOW - 19 * HOUR, "end": None,
             "runs": [{"start": NOW - 18 * HOUR, "end": NOW - 17 * HOUR}, {"start": NOW - 2 * HOUR, "end": None}]},
            {"id": "new", "kind": "subagent", "from": "coder", "to": "subagent", "title": "new", "open": False,
             "status": "done", "start": NOW - HOUR, "end": NOW - HOUR + 1400},
        ]
        script = STUBS + LANES + f"""
const nodes = walk(HandoffLanes({{ items: {json.dumps(items)}, now: {NOW}, since: {since}, sel: null, onSelect: () => {{}} }}))
const ticks = nodes.filter(n => n.type === 'span' && typeof n.props.children === 'number').map(n => n.props.children)
const keys = nodes.map(n => n.props.key).filter(Boolean)
const bar = nodes.find(n => n.props.key === 'old')
console.log(JSON.stringify({{ ticks, keys, left: bar.props.style.left, runs: bar.props.children.map(r => r.props.style.left) }}))
"""
        out = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                        capture_output=True, text=True, check=True).stdout)
        self.assertTrue(out["ticks"] and min(out["ticks"]) >= since, out["ticks"])
        # the card's sender dot and drop line fell before the window; its bar starts at the left edge
        self.assertNotIn("old-d", out["keys"])
        self.assertNotIn("old-l", out["keys"])
        self.assertIn("new-d", out["keys"])
        self.assertEqual(out["left"], "calc(0% + 0px)")
        # the run that ended before the window is dropped; the later one is placed from the visible start
        self.assertEqual(out["runs"], [f"{4 / 6 * 100}%"])


if __name__ == "__main__":
    unittest.main()
