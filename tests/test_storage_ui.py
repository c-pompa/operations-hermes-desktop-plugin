"""The metrics-storage section's rules, run under node against the same plugin.js slice the page runs.

The section lives between "// --- metrics store" and "// --- alerts"; its pure helpers decide what the
page says about the store, the heartbeat, the metrics dashboard and a failing collector source. The
whole file is also parsed by ci/js_syntax.sh, the gate the Desktop load uses.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "plugins" / "operations" / "desktop" / "plugin.js").read_text()
SECTION = JS[JS.index("// --- metrics store"):JS.index("// --- alerts")]

REPORT = {
    "store": "/home/me/.hermes/metrics/operations.db",
    "mb": 55.4, "estimated": False, "metrics_source": "operations", "retention_days": 60,
    "options": [{"days": 30, "label": "30 days"}, {"days": 60, "label": "60 days"},
                {"days": 90, "label": "90 days"}, {"days": 0, "label": "forever"}],
    "tables": [{"table": "records", "rows": 100, "mb": 50.0},
               {"table": "conformance_results", "rows": 10, "mb": 3.0},
               {"table": "health_findings", "rows": 5, "mb": 0.4}],
    "projections": [{"days": 30, "label": "30 days", "projected_mb": 51.4},
                    {"days": 60, "label": "60 days", "projected_mb": 99.4},
                    {"days": 90, "label": "90 days", "projected_mb": 147.4},
                    {"days": 0, "label": "forever", "projected_mb": 55.4, "monthly_mb": 48.0}],
}

HARNESS = """
const jsx = (t, p) => ({ type: typeof t === 'function' ? t.name : t, props: p })
const jsxs = jsx
const Tag = p => ({ tone: p.tone, label: p.children })
const MONO = 'mono'
const TONE = { good: 'good', warn: 'warn', bad: 'bad' }
const ID = 'operations'
const RETRY = {}
const api = {}
const openOut = () => {}
const Section = () => null
const Button = () => null
const useQuery = () => ({})
%s
const report = %s
const out = {
  labels: ['operations', 'metricsdash', 'none', 'wat'].map(metricsSourceLabel),
  heartbeat: [{ mode: 'own-timer' }, { mode: 'tick' }, { mode: 'none' }, {}].map(heartbeatLabel),
  tones: [{ mode: 'own-timer' }, { mode: 'tick' }, { mode: 'none' }].map(heartbeatTone),
  link: metricsDashLink({ present: true, link: 'http://127.0.0.1:8899/', revision: 'abc1234' }),
  noLink: metricsDashLink({ present: false }),
  mark: HeartbeatMark({ hb: { mode: 'own-timer' } }),
  markTone: HeartbeatMark({ hb: { mode: 'tick' } }),
  rows: storageRows(report),
  chosen: storageRows(report).filter(r => r.chosen).map(r => r.label),
  failing: collectorRows({ enabled: true, sources: { requests: { fails: 3, since: 1000, last_error: 'boom' } } }),
  recovered: collectorRows({ enabled: true, sources: { requests: { fails: 0, recovered_at: Date.now() / 1000 } } }),
  quiet: collectorRows({ enabled: false, sources: { requests: { fails: 3 } } })
}
console.log(JSON.stringify(out))
"""


def node_out():
    js = HARNESS % (SECTION, json.dumps(REPORT))
    done = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StorageSectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = node_out()

    def test_the_page_names_which_store_served_the_numbers(self):
        self.assertEqual(self.out["labels"],
                         ["own store", "metrics dashboard db (fallback)", "no store yet", "unknown source"])

    def test_the_heartbeat_says_tick_or_own_timer(self):
        self.assertEqual(self.out["heartbeat"], ["heartbeat: own timer", "heartbeat: dispatcher tick",
                                                 "heartbeat: none", "heartbeat: unknown"])
        self.assertEqual(self.out["tones"], ["warn", "good", "bad"])
        # the badge the page renders, not just the helper's string
        self.assertEqual(self.out["mark"]["props"]["children"]["props"]["children"], "heartbeat: own timer")
        self.assertEqual(self.out["mark"]["props"]["children"]["props"]["tone"], "warn")
        self.assertEqual(self.out["markTone"]["props"]["children"]["props"]["tone"], "good")

    def test_the_metrics_dashboard_is_linked_only_when_it_answers(self):
        self.assertEqual(self.out["link"]["url"], "http://127.0.0.1:8899/")
        self.assertIn("abc1234", self.out["link"]["label"])
        self.assertIn("Metrics Dashboard", self.out["link"]["label"])
        self.assertIsNone(self.out["noLink"])

    def test_the_storage_rows_are_size_per_table_and_each_retention(self):
        rows = self.out["rows"]
        self.assertEqual(rows[0], {"label": "store", "value": "55.4 MB",
                                   "note": "/home/me/.hermes/metrics/operations.db"})
        self.assertEqual([r["label"] for r in rows[1:4]], ["records", "conformance_results", "health_findings"])
        self.assertEqual(rows[1]["value"], "50 MB")
        self.assertEqual(rows[1]["note"], "100 rows")
        self.assertEqual([r["label"] for r in rows[4:]], ["keep 30 days", "keep 60 days",
                                                          "keep 90 days", "keep forever"])
        self.assertEqual(rows[7]["value"], "~55.4 MB")
        self.assertEqual(rows[7]["note"], "grows ~48 MB/month")
        # the retention in force is the highlighted row
        self.assertEqual(self.out["chosen"], ["keep 60 days"])

    def test_a_failing_source_and_its_recovery_are_both_shown(self):
        failing = self.out["failing"]
        self.assertEqual(len(failing), 1)
        self.assertTrue(failing[0]["bad"])
        self.assertIn("requests failed 3×", failing[0]["text"])
        self.assertIn("boom", failing[0]["text"])
        recovered = self.out["recovered"]
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0]["good"])
        self.assertIn("is reading again", recovered[0]["text"])
        self.assertEqual(self.out["quiet"], [])  # collection off: nothing to say

    def test_the_pages_carry_the_section_and_the_heartbeat_mark(self):
        # static wiring: Notifications renders the storage section; Overview renders the heartbeat and
        # the metrics dashboard link. Rendering those pages needs the Desktop shell, which this test
        # does not stand up; ci/js_syntax.sh parses the whole file.
        self.assertIn("jsx(StorageSection, {})", JS)
        self.assertIn("jsx(HeartbeatMark, { hb: setup?.heartbeat })", JS)
        self.assertIn("jsx(MetricsDashLink, { md: setup?.metrics_dash })", JS)


if __name__ == "__main__":
    unittest.main()