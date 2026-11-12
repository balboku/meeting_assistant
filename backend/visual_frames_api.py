"""Serve screenshots through the same meeting-read policy as the minutes."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from backend.database import get_meeting
from backend.visual_frames import FRAME_ID, read_frame
from backend.visual_notes import referenced_frames


router = APIRouter()


@router.get("/meetings/{meeting_id}/visual-frames/{digest}", tags=["會議記錄"])
def get_visual_frame(meeting_id: int, digest: str):
    if FRAME_ID.fullmatch(digest):
        record = get_meeting(meeting_id)
        if record and digest in referenced_frames(record.get("full_content") or ""):
            image = read_frame(Path(record["output_path"]), digest)
            if image is not None:
                return Response(content=image, media_type="image/jpeg",
                                headers={"Cache-Control": "private, no-store"})
    raise HTTPException(404, "此會議沒有可用的畫面截圖。")
