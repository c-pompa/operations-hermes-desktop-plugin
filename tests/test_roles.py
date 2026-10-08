import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "operations_api", ROOT / "plugins" / "operations" / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class RolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "plugin-data" / "operations" / "roles.json"
        patch = mock.patch.object(api, "_ROLES", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_built_in_roles_until_saved(self):
        out = api.roles_get()
        self.assertFalse(out["saved"])
        self.assertEqual([r["id"] for r in out["roles"]], ["all", "sre", "devops", "mlops", "cloud"])
        for r in out["roles"]:
            self.assertEqual(api._clean_role(r), r)
            self.assertTrue(r["summary"] and r["layout"] in api.LAYOUTS)

    def test_edit_add_and_reset(self):
        roles = [dict(r) for r in api.ROLE_DEFAULTS]
        roles[1] = {**roles[1], "summary": "  Keeps it up.  ", "skills": ["Paging", " "], "layout": "burst"}
        roles.append({"id": "security", "name": "Security", "watch": ["conformance", "incident"], "layout": "area"})
        api.roles_put(roles)
        out = api.roles_get()
        self.assertTrue(out["saved"])
        sre = out["roles"][1]
        self.assertEqual((sre["summary"], sre["skills"], sre["layout"]), ("Keeps it up.", ["Paging"], "burst"))
        self.assertEqual(out["roles"][-1]["watch"], ["incident", "conformance"])
        api.roles_reset()
        self.assertFalse(api.roles_get()["saved"])

    def test_rejects_bad_roles(self):
        good = dict(api.ROLE_DEFAULTS[0])
        for bad in [{**good, "id": "Has Space"}, {**good, "layout": "sphere"}, {**good, "watch": ["mail"]}, {**good, "watch": [[1]]},
                    {**good, "layout": []},
                    {**good, "skills": "one string"}, {**good, "name": ""}, "not a role"]:
            with self.assertRaises(HTTPException):
                api.roles_put([bad])
        with self.assertRaises(HTTPException):
            api.roles_put([good, good])
        self.assertFalse(self.path.exists())

    def test_unreadable_file_falls_back_and_says_so(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        out = api.roles_get()
        self.assertEqual(out["roles"], api.ROLE_DEFAULTS)
        self.assertIn("could not be read", out["errors"]["roles"])


if __name__ == "__main__":
    unittest.main()
