"""Behavioral contracts for modules extracted from the legacy bundles."""

import ast
import shutil
import subprocess
import unittest
from unittest import mock

from backend.transcript_diagnostics import (
    transcript_repeated_turn_diagnostic,
    transcript_repeated_turn_warning,
)
from tests.http_helpers import ROOT


def repeated_turns(minute=31, count=5):
    return "\n".join(
        f"[{minute:02d}:{index:02d}] **[發言者 A]**：這段發言連續重複需要複核。"
        for index in range(count)
    )


class TranscriptDiagnosticsExtractionTests(unittest.TestCase):
    def test_diagnostics_imports_only_standard_library(self):
        source = (ROOT / "backend/transcript_diagnostics.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertEqual(imports, {"re", "typing"})

    def test_nonrepeated_transcript_has_no_warning(self):
        transcript = "[00:00] **[A]**：第一項討論測試時程。\n[00:30] **[B]**：第二項討論測試結果。"
        self.assertIsNone(transcript_repeated_turn_warning(transcript, segment_seconds=600))

    def test_threshold_is_preserved(self):
        self.assertIsNone(transcript_repeated_turn_diagnostic(repeated_turns(count=3), segment_seconds=600))
        diagnostic = transcript_repeated_turn_diagnostic(repeated_turns(count=3), limit=2, segment_seconds=600)
        self.assertEqual(diagnostic["segment_indices"], [3])
        self.assertIn("同一句連續重複 3 次", diagnostic["warning"])

    def test_api_wrapper_uses_current_segment_duration(self):
        from backend import main

        with mock.patch.object(main, "SEGMENT_TARGET_SECONDS", 300):
            diagnostic = main._detail_transcript_repeated_turn_diagnostic(repeated_turns())
            warning = main._detail_transcript_repeated_turn_warning(repeated_turns())
        self.assertEqual(diagnostic["segment_indices"], [6])
        self.assertIn("第 7 段｜30:00-35:00", warning)
        self.assertEqual(warning, diagnostic["warning"])

    def test_english_heading_keeps_nonuniform_segment_boundaries(self):
        transcript = "[Segment 3/8 | 29:59 – 40:04]\n" + repeated_turns()
        diagnostic = transcript_repeated_turn_diagnostic(
            transcript,
            segment_seconds=600,
            segments=[{"index": 2, "start_seconds": 1799, "end_seconds": 2404}],
        )
        self.assertEqual(diagnostic["segment_indices"], [2])
        self.assertIn("第 3 段｜29:59-40:04", diagnostic["warning"])

    def test_malformed_metadata_falls_back_to_time_based_location(self):
        diagnostic = transcript_repeated_turn_diagnostic(
            repeated_turns(), segment_seconds=600,
            segments=[None, {"index": "bad"}, {"index": 0}],
        )
        self.assertEqual(diagnostic["segment_indices"], [3])
        self.assertIn("31:00-31:04", diagnostic["warning"])

    def test_multiple_repeated_runs_retain_separate_locations(self):
        transcript = repeated_turns() + "\n[35:00] **[B]**：另一項正常討論。\n" + repeated_turns(minute=41)
        diagnostic = transcript_repeated_turn_diagnostic(transcript, segment_seconds=600)
        self.assertEqual(diagnostic["segment_indices"], [3, 4])
        self.assertIn("第 4 段｜30:00-40:00", diagnostic["warning"])
        self.assertIn("第 5 段｜40:00-50:00", diagnostic["warning"])


class ModalAccessibilityExtractionTests(unittest.TestCase):
    def test_classic_script_loads_before_inline_callers(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        tag = '<script src="/static/modal-accessibility.js"></script>'
        self.assertEqual(html.count(tag), 1)
        self.assertLess(html.index(tag), html.index("<script>"))
        self.assertNotIn("function openAccessibleModal", html)

    def test_modal_keyboard_focus_and_escape_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
class Element {
  constructor() { this.disabled = false; this.offsetParent = {}; this.focuses = 0; }
  focus(options) { this.focuses++; this.focusOptions = options; document.activeElement = this; }
}
const opener = new Element();
const first = new Element();
const last = new Element();
const hidden = new Element(); hidden.offsetParent = null;
const disabled = new Element(); disabled.disabled = true;
const classes = new Set();
const rootClasses = new Set();
const body = { scrollTop: 200 };
const modal = {
  id: 'upload-modal',
  classList: { add: v => classes.add(v), remove: v => classes.delete(v) },
  querySelectorAll: () => [first, hidden, disabled, last],
  querySelector: selector => selector === '.modal-body' ? body : null,
  contains: el => [first, last].includes(el)
};
let visibleModals = [];
let listener;
let uploadClosed = 0;
let recordClosed = 0;
const document = {
  activeElement: opener,
  documentElement: { classList: { toggle: (value, enabled) => enabled ? rootClasses.add(value) : rootClasses.delete(value) } },
  body: { contains: el => el === opener },
  querySelectorAll: () => classes.has('show') ? [modal, ...visibleModals.filter(el => el !== modal)] : visibleModals,
  addEventListener: (type, cb) => { assert.equal(type, 'keydown'); listener = cb; }
};
const context = {
  document, HTMLElement: Element, window: { setTimeout: cb => cb() },
  closeUploadModal: () => uploadClosed++,
  closeRecordModal: () => recordClosed++,
  closeSummaryEditor() {}, closeTranscriptEditor() {}, closeRevisionHistory() {}, closeSourceStorageInventory() {}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert.equal(typeof listener, 'function');
assert.equal(context.getFocusableModalElements(modal).length, 2);
context.openAccessibleModal(modal);
assert(classes.has('show'));
assert(rootClasses.has('modal-open'));
assert.equal(body.scrollTop, 0);
assert.equal(document.activeElement, first);
const event = (key, shiftKey = false) => ({ key, shiftKey, prevented: false, preventDefault() { this.prevented = true; } });
document.activeElement = last;
let e = event('Tab'); assert(context.trapModalFocus(e, modal)); assert(e.prevented); assert.equal(document.activeElement, first);
e = event('Tab', true); assert(context.trapModalFocus(e, modal)); assert.equal(document.activeElement, last);
document.activeElement = opener;
e = event('Tab'); assert(context.trapModalFocus(e, modal)); assert.equal(document.activeElement, first);
assert.equal(context.trapModalFocus(event('Enter'), modal), false);
context.closeAccessibleModal(modal);
assert.equal(document.activeElement, opener);
assert.equal(opener.focusOptions.preventScroll, true);
assert(!classes.has('show'));
assert(!rootClasses.has('modal-open'));
context.openAccessibleModal(modal, last);
assert.equal(document.activeElement, last);
context.closeAccessibleModal(modal, false);
assert.equal(document.activeElement, last);
visibleModals = [{ id: 'record-modal' }];
context.openAccessibleModal(modal);
context.closeAccessibleModal(modal, false);
assert(rootClasses.has('modal-open'));
visibleModals = [modal, { id: 'record-modal' }];
e = event('Escape'); listener(e); assert(e.prevented); assert.equal(recordClosed, 1); assert.equal(uploadClosed, 0);
visibleModals = [modal]; listener(event('Escape')); assert.equal(uploadClosed, 1);
visibleModals = []; listener(event('Escape')); assert.equal(uploadClosed, 1);
visibleModals = [{ id: 'unknown-modal' }]; e = event('Escape'); listener(e); assert(!e.prevented);
"""
        result = subprocess.run(
            [node, "-e", script, str(ROOT / "static/modal-accessibility.js")],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
