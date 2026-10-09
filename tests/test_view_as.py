"""View as is a business feature: off, everyone sees the first role and the picker and role editor
are hidden (desktop/plugin.js VIEW_AS)."""
import unittest
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "plugins" / "operations" / "desktop" / "plugin.js").read_text()


class ViewAsOffTest(unittest.TestCase):
    def test_off_and_gating_the_picker_editor_and_stored_role(self):
        self.assertIn("const VIEW_AS = false", JS)
        for gated in ("(VIEW_AS && list.find(r => r.id === roleId)) || list[0]",
                      "VIEW_AS && role ? jsxs(Select", "VIEW_AS && roles ? jsx('button'"):
            self.assertIn(gated, JS)


if __name__ == "__main__":
    unittest.main()
