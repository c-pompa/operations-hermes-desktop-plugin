"""Drag-to-scroll on cut-off grids and maps (plugin.js dragScroll), run under node with a stand-in element."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
DRAG = JS[JS.index("// Press, hold and drag"):JS.index("// Rows against checks")]
HARNESS = """
const on = () => { const ls = {}; return { ls,
  addEventListener: (t, f, o) => { (ls[t] = ls[t] || []).push(Object.assign(e => f(e), { f, once: o?.once })) },
  removeEventListener: (t, f) => { ls[t] = (ls[t] || []).filter(g => g.f !== f) },
  fire: (t, e) => (ls[t] || []).slice().forEach(g => { if (g.once) ls[t] = ls[t].filter(h => h !== g); g(e) }) } }
globalThis.window = on()
const el = Object.assign(on(), { dataset: {}, style: {}, scrollWidth: %d, clientWidth: 300, scrollLeft: 100 })
const target = { closest: () => null }
dragScroll(el)
dragScroll(el)
const clicks = []
const drag = (dx, label) => {
  el.fire('pointerdown', { button: 0, clientX: 200, target })
  window.fire('pointermove', { clientX: 200 + dx })
  window.fire('pointerup', {})
  el.fire('click', { stopPropagation: () => {}, preventDefault: () => clicks.push(label) })
}
drag(-80, 'after drag')
const afterDrag = el.scrollLeft
drag(3, 'after tap')
console.log(JSON.stringify({ afterDrag, afterTap: el.scrollLeft, swallowed: clicks, downs: el.ls.pointerdown.length,
                             left: (window.ls.pointermove || []).length }))
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class DragScrollTest(unittest.TestCase):
    def run_js(self, scroll_width):
        out = subprocess.run(["node", "--input-type=module", "-e", DRAG + HARNESS % scroll_width],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_drag_scrolls_and_swallows_only_the_click_after_a_drag(self):
        out = self.run_js(900)
        self.assertEqual((out["afterDrag"], out["afterTap"]), (180, 180))
        self.assertEqual(out["swallowed"], ["after drag"])
        self.assertEqual((out["downs"], out["left"]), (1, 0))

    def test_a_box_that_fits_does_not_drag(self):
        out = self.run_js(300)
        self.assertEqual((out["afterDrag"], out["swallowed"]), (100, []))


if __name__ == "__main__":
    unittest.main()
