"""Embed only intact, already-cited screenshots in an exported Word document."""
from io import BytesIO
from pathlib import Path
from struct import error as StructError

from docx.image.exceptions import InvalidImageStreamError, UnexpectedEndOfFileError, UnrecognizedImageError
from docx.image.image import Image
from docx.shared import Inches, Pt

from backend.visual_frames import read_frame


class WordVisualFrames:
    def __init__(self, output_path: str | None, width: int):
        self.markdown = Path(output_path) if output_path else None
        self.width = max(0, min(Inches(6), width - Inches(0.3)))
        self.cache: dict[str, tuple[bytes, int, int] | None] = {}

    def _picture(self, digest: str) -> tuple[bytes, int, int] | None:
        if not self.markdown or not self.width or not (data := read_frame(self.markdown, digest)):
            return None
        try:
            image = Image.from_blob(data)
            if image.px_width <= 0 or image.px_height <= 0 or not image.horz_dpi or not image.vert_dpi:
                return None
            # Fit within both the text column and a reasonable page height.
            width = int(min(self.width, Inches(image.px_width / 120),
                            Inches(3) * image.px_width / image.px_height))
            height = int(width * image.px_height / image.px_width)
            return (data, width, height) if width > 0 and height > 0 else None
        except (InvalidImageStreamError, UnexpectedEndOfFileError, UnrecognizedImageError, StructError, ValueError):
            return None

    def append(self, cell, caption, note: dict) -> None:
        digest = note["frame_sha256"]
        if digest not in self.cache:
            self.cache[digest] = self._picture(digest)
        if not self.cache[digest]:
            return
        data, width, height = self.cache[digest]
        paragraph = cell.add_paragraph()
        fmt = paragraph.paragraph_format
        fmt.space_before = Pt(2)
        fmt.space_after = Pt(6)
        fmt.keep_together = True
        fmt.keep_with_next = False
        fmt.line_spacing = 1.0
        shape = paragraph.add_run().add_picture(BytesIO(data), width=width, height=height)
        shape._inline.docPr.set("descr", f"畫面來源：{note['source']}；錄影時間：{note['timecode']}")
        caption.paragraph_format.keep_with_next = True
