import shutil
import subprocess
import unittest
from tests.http_helpers import ROOT


class StorageCapacityUiTests(unittest.TestCase):
    def test_units_states_and_dialog(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for capacity UI contracts")
        result = subprocess.run([node, "tests/storage_capacity_test.js"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_asset_precedes_callers_and_scopes_are_visible(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/static/storage-capacity.js"'), html.index("<script>"))
        self.assertIn("本機備份容量", html)
        self.assertIn("影音容量", html)
        self.assertIn("StorageCapacity.render(null)", html)
