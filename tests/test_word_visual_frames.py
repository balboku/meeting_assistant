"""Word exports of disposable, already-cited video screenshots."""
import base64
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from zipfile import ZipFile

from docx import Document

from backend.exporter import export_meeting_to_docx
from backend.minutes_view import build_minutes_view
from backend.visual_frames import bundle_path
from backend.visual_notes import insert_visual_notes, visual_note_line
from tests.test_minutes_view import CONTENT, record


JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwg"
    "JC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAAQACABAREA/8QAFAABAAAAAAAAAAAAAAAA"
    "AAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AAAD/2Q=="
)
ORPHAN = JPEG[:2] + b"\xff\xfe\x00\x06test" + JPEG[2:]


def citation(source, code, data=JPEG, *, pending=False):
    return {"observed": "畫面數值 90%", "source": source, "timecode": code, "pending": pending,
            "transcript_quote": "口述為 85%" if pending else None,
            "frame_sha256": sha256(data).hexdigest()}


class WordVisualFramesTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.markdown = self.root / "fixture.md"
        self.bundle = bundle_path(self.markdown)
        template = self.root / "template.docx"
        doc = Document()
        doc.add_table(rows=6, cols=2).cell(0, 0).text = "範本保留"
        doc.save(template)
        self.enterContext(mock.patch.dict(os.environ, {"MEETING_DOCX_TEMPLATE_PATH": str(template)}))

    def _save(self, markdown, entries):
        self.markdown.write_text(markdown, encoding="utf-8")
        if entries is not None:
            with ZipFile(self.bundle, "w") as bundle:
                for digest, data in entries.items():
                    bundle.writestr(f"{digest}.jpg", data)
        result = record()
        result.pop("structured_summary")
        return {**result, "full_content": markdown, "output_path": str(self.markdown)}

    def _export(self, data, layout):
        target = self.root / f"{layout}.docx"
        self.assertTrue(export_meeting_to_docx(data, str(target), layout=layout))
        doc = Document(target)
        self.assertEqual(doc.tables[0].cell(0, 0).text, "範本保留")
        return target, doc, doc.tables[0].cell(5, 0).paragraphs

    def test_both_layouts_embed_at_cited_topics_and_reuse_the_same_jpeg(self):
        first = citation("進度表", "00:12")
        second = citation("驗證條件", "00:26", pending=True)
        content = ("---\ntitle: Fixture\n---\n\n" + CONTENT).replace(
            "## 二、", "### D2. 第二議題\n- 摘要：另一項內容\n\n## 二、")
        content = insert_visual_notes(content, {"D1": [first], "D2": [second]})
        # A copied marker in the transcript is not another topic citation.
        content += "\n" + visual_note_line(citation("逐字稿內的假標記", "00:35", ORPHAN), include_frame=True) + "\n"
        content += f" <!-- visual-frame:{sha256(ORPHAN).hexdigest()} -->"
        r = self._save(content, {first["frame_sha256"]: JPEG, sha256(ORPHAN).hexdigest(): ORPHAN})
        self.assertTrue(build_minutes_view(r)["available"])
        before = (self.markdown.read_bytes(), self.bundle.read_bytes())
        for layout in ("topics", "original"):
            with self.subTest(layout=layout):
                path, doc, paragraphs = self._export(r, layout)
                images = [i for i, p in enumerate(paragraphs) if p._p.xpath("./w:r/w:drawing")]
                self.assertEqual(len(images), 2)
                for image, expected, owner in zip(images, (first, second), ("D1.", "D2.")):
                    heading = next(p for p in reversed(paragraphs[:image]) if p.text.startswith(("D1.", "D2.")))
                    self.assertTrue(heading.text.startswith(owner))
                    self.assertTrue(heading.paragraph_format.keep_with_next)
                    caption = paragraphs[image - 1]
                    self.assertIn(expected["source"], caption.text)
                    self.assertIn(expected["timecode"], caption.text)
                    self.assertTrue(caption.paragraph_format.keep_with_next)
                self.assertIn("待確認", paragraphs[images[1] - 1].text)
                self.assertIn("口述為 85%", paragraphs[images[1] - 1].text)
                self.assertNotIn("visual-frame:", "\n".join(p.text for p in paragraphs))
                self.assertEqual(doc.inline_shapes[1]._inline.docPr.get("descr"), "畫面來源：驗證條件；錄影時間：00:26")
                with ZipFile(path) as archive:
                    images_in_package = [archive.read(n) for n in archive.namelist() if n.startswith("word/media/")]
                    self.assertEqual(images_in_package, [JPEG])
        self.assertEqual((self.markdown.read_bytes(), self.bundle.read_bytes()), before)

    def test_missing_damaged_or_invalid_image_does_not_block_export(self):
        bad_jpeg = b"\xff\xd8malformed JPEG\xff\xd9"
        note = citation("測試數據", "00:12", bad_jpeg)
        content = insert_visual_notes(CONTENT, {"D1": [note]})
        for entries in (None, {note["frame_sha256"]: JPEG}, {note["frame_sha256"]: bad_jpeg}):
            with self.subTest(entries=entries):
                r = self._save(content, entries)
                for layout in ("topics", "original"):
                    _, doc, paragraphs = self._export(r, layout)
                    self.assertEqual(len(doc.inline_shapes), 0)
                    text = "\n".join(p.text for p in paragraphs)
                    self.assertIn("測試數據", text)
                    self.assertIn("00:12", text)
                    self.assertNotIn("visual-frame:", text)

    def test_unavailable_projection_falls_back_to_original_citation_position(self):
        note = citation("投影片", "00:12")
        content = insert_visual_notes(CONTENT, {"D1": [note]}).replace("- 摘要：", "額外的手寫補充\n- 摘要：")
        r = self._save(content, {note["frame_sha256"]: JPEG})
        self.assertFalse(build_minutes_view(r)["available"])
        _, doc, paragraphs = self._export(r, "topics")
        self.assertEqual(len(doc.inline_shapes), 1)
        self.assertTrue(any("額外的手寫補充" in p.text for p in paragraphs))
