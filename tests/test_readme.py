"""The catalog page renders plugins/operations/README.md (the listing's subdir); GitHub renders the
root one. Keep them the same file."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReadmeCopyTest(unittest.TestCase):
    def test_subdir_readme_matches_root(self):
        self.assertEqual((ROOT / "plugins" / "operations" / "README.md").read_text(), (ROOT / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
