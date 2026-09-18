"""Structural regression tests; real-browser geometry is verified separately."""

import unittest
from html.parser import HTMLParser

from tests.http_helpers import ROOT


class PageTree(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.root = {"tag": "root", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.ids = {}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": [], "parent": self.stack[-1]}
        self.stack[-1]["children"].append(node)
        if "id" in node["attrs"]:
            self.ids[node["attrs"]["id"]] = node
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if self.stack[-1]["tag"] != tag:
            raise AssertionError(f"Unbalanced HTML: expected {self.stack[-1]['tag']}, got {tag}")
        self.stack.pop()


class ResponsiveDialogTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        self.css = (ROOT / "static/responsive-layout.css").read_text(encoding="utf-8")
        self.tree = PageTree(self.html)

    def test_all_dialogs_have_fixed_header_and_one_scroll_body(self):
        for modal_id in (
            "upload-modal", "record-modal", "summary-editor-modal",
            "transcript-editor-modal", "revision-modal", "source-storage-modal",
        ):
            with self.subTest(modal=modal_id):
                card = self.tree.ids[modal_id]["children"][0]
                classes = [node["attrs"].get("class", "") for node in card["children"]]
                self.assertEqual(classes[0], "modal-header")
                self.assertEqual(classes[1], "modal-body")
                self.assertEqual(classes.count("modal-body"), 1)
                if modal_id != "revision-modal":
                    self.assertIn(classes[-1], {"modal-footer", "rec-controls"})
        self.assertEqual(self.tree.stack, [self.tree.root])

    def test_recording_controls_stay_outside_scroll_body(self):
        controls = self.tree.ids["btn-rec-start"]["parent"]
        self.assertEqual(controls["attrs"]["class"], "rec-controls")
        self.assertIn("modal", controls["parent"]["attrs"]["class"])
        result = self.tree.ids["rec-result"]
        self.assertEqual(result["parent"]["attrs"]["class"], "modal-body")
        for element_id in ("rec-progress-box", "rec-title", "source-storage-list", "revision-list"):
            current = self.tree.ids[element_id]
            while current.get("parent") and current["attrs"].get("class") != "modal-body":
                current = current["parent"]
            self.assertEqual(current["attrs"].get("class"), "modal-body")

    def test_mobile_hides_only_counters_not_recording_or_upload(self):
        self.assertNotIn(".stats { display: none; }", self.html)
        self.assertIn(".stats .stat { display: none; }", self.html)
        for element_id, handler in (("btn-open-record", "openRecordModal()"), ("btn-open-upload", "openUploadModal()")):
            button = self.tree.ids[element_id]
            self.assertEqual(button["tag"], "button")
            self.assertEqual(button["attrs"]["onclick"], handler)
            self.assertEqual(button["parent"]["attrs"]["class"], "stats")
        self.assertIn("min-height: 44px", self.css)

    def test_viewport_styles_load_after_legacy_inline_styles(self):
        link = '<link rel="stylesheet" href="/static/responsive-layout.css" />'
        self.assertEqual(self.html.count(link), 1)
        self.assertLess(self.html.index("</style>"), self.html.index(link))
        self.assertIn("max-height: calc(100dvh - 32px)", self.css)
        self.assertIn("max-height: calc(100vh - 32px)", self.css)
        self.assertIn("max-height: calc(100dvh - 16px)", self.css)
        self.assertIn("min-height: 0", self.css)
        self.assertIn("overscroll-behavior: contain", self.css)
        self.assertIn("html.modal-open body", self.css)
        self.assertIn("#summary-editor-modal .modal-body", self.css)
        self.assertIn("#transcript-editor-modal .modal-body", self.css)
        self.assertNotIn("min-height: 160px", self.css)


if __name__ == "__main__":
    unittest.main()
