"""Run the collector's browser JS against a real DOM, without the portal.

The pager logic is the part that silently under-collected before (DataTables
``pagingType: "numbers"`` renders no "Next" control), so it is verified here
against markup captured from the portal: real HTML, real selector engine.

The DOM is provided by jsdom through Node. When Node or jsdom are unavailable the
tests skip rather than fail, so the suite still runs on a bare Python install.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "portal_datatable.html"
TABS_FIXTURE = REPO / "tests" / "fixtures" / "portal_tabs.html"
COLLECTOR = REPO / "smp_extractor" / "collector.py"

NODE_SCRIPT = r'''
const fs = require('fs');
const jsdomPath = process.env.JSDOM_PATH;

const html = fs.readFileSync(process.env.FIXTURE, 'utf8');
const { JSDOM } = require(jsdomPath);
const dom = new JSDOM(html, { pretendToBeVisual: true, runScripts: 'outside-only' });
const window = dom.window;
const document = window.document;
const getComputedStyle = window.getComputedStyle.bind(window);

// jsdom does not do layout; give every element a box so the visibility guard
// behaves the way it does in a rendered page.
Object.defineProperty(window.HTMLElement.prototype, 'getBoundingClientRect', {
  value() {
    // jsdom has no layout; report no box for anything inside a display:none
    // panel so hidden tab content is treated the way a browser treats it.
    for (let node = this; node; node = node.parentElement) {
      if (node.style && node.style.display === 'none') {
        return { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
      }
    }
    return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 };
  },
});

const py = fs.readFileSync(process.env.COLLECTOR, 'utf8');
const extract = (name) => {
  const m = new RegExp(name + ' = r"""(.*?)"""', 's').exec(py);
  if (!m) throw new Error('missing ' + name);
  return m[1];
};

const out = {};
out.pagers = eval('(' + extract('PAGER_INFO_JS') + ')')();
out.snapshots = eval('(' + extract('TABLE_SNAPSHOT_JS') + ')')();

const stepSrc = /"""\(\[tableId, visited\]\) => \{(.*?)\}"""/s.exec(py);
if (!stepSrc) throw new Error('page-step JS not found');
out.step = eval('(([tableId, visited]) => {' + stepSrc[1] + '})');
out.stepVisited0 = out.step(['DataTables_Table_0', [0]]);
out.stepVisited01 = out.step(['DataTables_Table_0', [0, 1]]);
out.tabLike = eval('(' + extract('TAB_LIKE_CANDIDATES_JS') + ')')();
out.collapse = eval('(' + extract('COLLAPSE_CANDIDATES_JS') + ')')();
out.declaredTabs = eval('(' + extract('TAB_DESCRIPTORS_JS') + ')')();

process.stdout.write(JSON.stringify(out));
'''


def _node_env():
    node = shutil.which("node")
    if node is None:
        return None, "node is not installed"
    candidates = [
        Path("/tmp/jsdom/node_modules/jsdom"),
        REPO / "node_modules" / "jsdom",
    ]
    for candidate in candidates:
        if (candidate / "package.json").exists():
            return node, str(candidate)
    return node, None


class TabDomTests(unittest.TestCase):
    """The Milk/Biscuit buttons are plain <button> elements, not declared tabs."""

    @classmethod
    def setUpClass(cls):
        cls.node, cls.jsdom = _node_env()
        cls.payload = None
        if cls.node is None or cls.jsdom is None:
            return
        script = NODE_SCRIPT.replace(
            'fs.readFileSync(process.env.FIXTURE, \'utf8\')',
            'fs.readFileSync(process.env.TABS_FIXTURE, \'utf8\')',
        )
        env = {
            "FIXTURE": str(TABS_FIXTURE),
            "TABS_FIXTURE": str(TABS_FIXTURE),
            "COLLECTOR": str(COLLECTOR),
            "JSDOM_PATH": cls.jsdom,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        try:
            completed = subprocess.run(
                [cls.node, "-e", script],
                capture_output=True,
                text=True,
                timeout=90,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if completed.returncode == 0:
            try:
                cls.payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                cls.payload = None

    def setUp(self):
        if self.node is None:
            self.skipTest("node is not installed")
        if self.jsdom is None:
            self.skipTest("jsdom is not installed")
        if self.payload is None:
            self.fail("the collector JS could not be executed against the tabs fixture")

    def test_plain_button_tabs_are_discovered(self):
        labels = [item["text"] for item in self.payload["tabLike"]]
        self.assertEqual(labels, ["Milk", "Biscuit"])

    def test_active_state_is_reported(self):
        by_label = {item["text"]: item for item in self.payload["tabLike"]}
        self.assertTrue(by_label["Milk"]["active"])
        self.assertFalse(by_label["Biscuit"]["active"])

    def test_tab_signature_is_stable_for_clicking(self):
        signatures = [item["signature"] for item in self.payload["tabLike"]]
        self.assertEqual(signatures, ["tab-milk|Milk", "tab-biscuit|Biscuit"])

    def test_declared_tab_selector_finds_nothing_here(self):
        """This is why the old code never saw these buttons at all."""
        self.assertEqual(self.payload["declaredTabs"], [])

    def test_collapse_sweep_ignores_tab_buttons(self):
        """Regression: the collapse sweep used to click Milk/Biscuit and time out."""
        self.assertEqual(self.payload["collapse"], [])

    def test_hidden_panel_tables_are_not_reported_visible(self):
        snapshot = {item["id"]: item for item in self.payload["snapshots"]}
        self.assertTrue(snapshot["DataTables_Table_0"]["visible"])
        self.assertFalse(snapshot["DataTables_Table_1"]["visible"])


class PagerDomTests(unittest.TestCase):
    """Contract between the portal's real markup and the shipped JS."""

    @classmethod
    def setUpClass(cls):
        cls.node, cls.jsdom = _node_env()
        cls.payload = None
        if cls.node is None:
            return
        if cls.jsdom is None:
            return
        env = {
            "FIXTURE": str(FIXTURE),
            "COLLECTOR": str(COLLECTOR),
            "JSDOM_PATH": cls.jsdom,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        try:
            completed = subprocess.run(
                [cls.node, "-e", NODE_SCRIPT],
                capture_output=True,
                text=True,
                timeout=90,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if completed.returncode != 0:
            return
        try:
            cls.payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            cls.payload = None

    def setUp(self):
        if self.node is None:
            self.skipTest("node is not installed")
        if self.jsdom is None:
            self.skipTest("jsdom is not installed")
        if self.payload is None:
            self.fail("the pager JS could not be executed against the fixture")

    def test_fixture_exists(self):
        self.assertTrue(FIXTURE.is_file())

    def test_pager_records_are_produced_for_every_table(self):
        pagers = self.payload["pagers"]
        self.assertEqual(len(pagers), 2)
        self.assertEqual(pagers[0]["tableId"], "DataTables_Table_0")
        self.assertEqual(pagers[1]["tableId"], "enrollment-table")

    def test_info_line_is_read_from_the_right_element(self):
        pagers = self.payload["pagers"]
        self.assertEqual(pagers[0]["infoText"], "Showing 1 to 5 of 18 entries")
        # The second table has no info line; it must not inherit table 0's.
        self.assertEqual(pagers[1]["infoText"], "")

    def test_numbered_pager_structure_is_reported(self):
        pager = self.payload["pagers"][0]
        self.assertTrue(pager["hasPager"])
        self.assertEqual(pager["numericIdxs"], [0, 1, 2, 3])
        self.assertEqual(pager["activeIdx"], 0)

    def test_show_all_option_is_detected(self):
        pager = self.payload["pagers"][0]
        self.assertTrue(pager["lengthHasAll"])
        self.assertEqual(pager["lengthValue"], "5")

    def test_table_ids_and_classes_reach_the_snapshot(self):
        snapshots = self.payload["snapshots"]
        self.assertEqual(snapshots[0]["id"], "DataTables_Table_0")
        self.assertIn("consumption-summary-table", snapshots[0]["classes"])
        # 5 body rows + 1 header row
        self.assertEqual(len(snapshots[0]["rows"]), 6)

    def test_step_candidates_skip_the_active_page(self):
        steps = self.payload["stepVisited0"]
        self.assertEqual([step["pageIndex"] for step in steps], [1, 2, 3])
        self.assertEqual(steps[0]["signature"], "DataTables_Table_0|2")

    def test_step_candidates_skip_already_visited_pages(self):
        steps = self.payload["stepVisited01"]
        self.assertEqual([step["pageIndex"] for step in steps], [2, 3])

    def test_parsed_states_match_python_expectations(self):
        """The Python parser must agree with the DOM the JS just walked."""
        from smp_extractor.pagination import pager_states

        states = pager_states(self.payload["pagers"])
        self.assertEqual(sorted(states), [0])
        state = states[0]
        self.assertEqual(state.total_rows, 18)
        self.assertEqual(state.page_size, 5)
        self.assertEqual(state.page_count, 4)
        self.assertTrue(state.is_paginated)
        self.assertTrue(state.can_show_all)


if __name__ == "__main__":
    unittest.main()
