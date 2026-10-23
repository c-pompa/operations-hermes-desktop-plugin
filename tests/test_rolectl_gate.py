"""The once-a-day toast must not advertise a private tool on someone else's install.

`/models/agents` reports `rolectl_mcp` as the path when rolectl-mcp.py is on this machine and ""
when it is not. The toast used to fire whenever no profile could fix router roles, so an install
running a different model router was told to turn on a helper it does not have — advice that ends
nowhere for the user reading it.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "operations"
JS = (ROOT / "desktop" / "plugin.js").read_text()
START = JS.index("const ROLECTL_TOASTED")
# end on the next function declaration, not on a comment: rewording a banner silently moved this
# slice's boundary once already, and the slice is what the toast tests actually execute
SLICE = JS[START:JS.index("function gitlabConnect(s)", START)]

HARNESS = """
const PATH = '/operations'
const toasts = [], store = {}
const host = { notify: n => toasts.push(n), navigate: () => {} }
const canChat = () => true
const fixRolectl = () => {}
const setInterval = () => 1, clearInterval = () => {}
%s
const payload = %s
const ctx = { rest: async () => payload,
              storage: { get: (k, d) => (k in store ? store[k] : d), set: (k, v) => { store[k] = v } },
              onDispose: () => {} }
rolectlToast(ctx)
await new Promise(r => setTimeout(r, 0))
// a second run on the same day must not repeat it
rolectlToast(ctx)
await new Promise(r => setTimeout(r, 0))
console.log(JSON.stringify({ marked: store['rolectl.toasted'] === new Date().toDateString(),
                             toasts: toasts.map(t => ({ message: t.message, detail: t.detail, action: t.action?.label })) }))
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RolectlGateTest(unittest.TestCase):
    def run_js(self, payload):
        out = subprocess.run(["node", "--input-type=module", "-e", HARNESS % (SLICE, json.dumps(payload))],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_no_toast_without_rolectl_mcp_because_there_is_nothing_to_turn_on(self):
        self.assertEqual(self.run_js({"router": {"url": "x"}, "rolectl_mcp": "", "profiles": {}})["toasts"], [])

    def test_a_toast_when_rolectl_mcp_is_there_and_no_profile_is_set_up(self):
        out = self.run_js({"router": {"url": "x"}, "rolectl_mcp": "/opt/rolectl-mcp.py", "profiles": {}})
        toasts = out["toasts"]
        # two runs in one day, one toast: the marker stops the repeat
        self.assertEqual(len(toasts), 1)
        self.assertTrue(out["marked"])
        self.assertEqual(toasts[0]["message"], "No profile can fix model router roles.")
        self.assertEqual(toasts[0]["action"], "Fix in a chat")

    def test_no_toast_when_a_profile_can_already_fix_roles(self):
        self.assertEqual(self.run_js({"router": {"url": "x"}, "rolectl_mcp": "/p/rolectl-mcp.py",
                                      "profiles": {"default": "m"}})["toasts"], [])

    def test_no_toast_without_a_router(self):
        self.assertEqual(self.run_js({"router": None, "rolectl_mcp": "/p/rolectl-mcp.py",
                                      "profiles": {}})["toasts"], [])


if __name__ == "__main__":
    unittest.main()
