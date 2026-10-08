"""Flow's served colors (plugin.js servedColors): each device keeps its color once seen."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
COLORS = JS[JS.index("// no red: red marks"):JS.index("const pct = ")]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ServedColorsTest(unittest.TestCase):
    def test_a_device_keeps_its_color_when_others_come_and_go(self):
        js = """
const store = {}
const api = { storage: { set: (k, v) => { store[k] = v } } }
const storeGet = (k, d) => (k in store ? store[k] : d)
%s
const day1 = servedColors(['deepseek', 'nous', 'mbp', 'custom'])
const day2 = servedColors(['nous', 'desktop', 'custom'])
console.log(JSON.stringify([day1, day2, SERVED_COLORS]))
""" % COLORS
        day1, day2, palette = json.loads(subprocess.run(["node", "--input-type=module", "-e", js],
                                                        capture_output=True, text=True, check=True).stdout)
        # busiest first takes the first colors; a newcomer takes an unused one, not a taken one
        self.assertEqual(list(day1.values()), palette[:4])
        self.assertEqual((day2["nous"], day2["custom"]), (day1["nous"], day1["custom"]))
        self.assertNotIn(day2["desktop"], day1.values())


if __name__ == "__main__":
    unittest.main()
