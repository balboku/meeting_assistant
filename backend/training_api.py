"""Course endpoints using the existing read/write roles and media storage."""
import asyncio
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from backend import training_store as store
from backend.auth import actor_from_request, require_permission
from backend.local_speech import capabilities
from backend.processing_policy import cloud_enabled
from backend.seekable_media import ensure_seekable_playback_media, SeekableMediaError

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path(os.getenv("MEETING_OUTPUT_DIR") or ROOT / "output")
SOURCE_AUDIO_DIR = Path(os.getenv("MEETING_SOURCE_AUDIO_DIR") or OUTPUT_DIR / "source_audio")
PLAYBACK_MEDIA_DIR = OUTPUT_DIR / "playback_media"
router = APIRouter(prefix="/training", tags=["教育訓練"])


class NewCourse(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    processing_mode: Literal["cloud", "local"] = "cloud"


class NewPart(BaseModel):
    client_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=200)


class MeetingPart(BaseModel):
    meeting_id: int = Field(ge=1)


class PartOrder(BaseModel):
    part_ids: list[str] = Field(max_length=200)
    version: int = Field(ge=1)


class CourseStatus(BaseModel):
    status: Literal["open", "closed"]
    version: int = Field(ge=1)


def _run(request, permission, operation, *args, **kwargs):
    require_permission(actor_from_request(request), permission)
    try:
        return operation(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/courses")
def courses(request: Request):
    actor = actor_from_request(request)
    require_permission(actor, "meeting:read")
    return {"courses": store.list_courses(), "can_write": actor.can("meeting:write"),
            "cloud_enabled": cloud_enabled()}


@router.post("/courses", status_code=201)
def create(payload: NewCourse, request: Request):
    require_permission(actor_from_request(request), "meeting:write")
    mode = payload.processing_mode if cloud_enabled() else "local"
    if mode == "local" and not capabilities()["local_transcription"]:
        raise HTTPException(409, "本地轉錄模型尚未就緒，請管理員完成安裝後再建立機密課程。")
    course_id = _run(request, "meeting:write", store.create_course, payload.title, mode)
    return store.get_course(course_id)


@router.get("/courses/{course_id}")
def detail(course_id: int, request: Request):
    return _run(request, "meeting:read", store.get_course, course_id)


@router.post("/courses/{course_id}/parts", status_code=201)
def reserve(course_id: int, payload: NewPart, request: Request):
    return _run(request, "meeting:write", store.reserve_part, course_id, **payload.model_dump())


@router.post("/courses/{course_id}/meetings", status_code=201)
def attach(course_id: int, payload: MeetingPart, request: Request):
    part_id = _run(request, "meeting:write", store.attach_meeting, course_id, payload.meeting_id)
    return {"id": part_id}


@router.put("/courses/{course_id}/order")
def order(course_id: int, payload: PartOrder, request: Request):
    _run(request, "meeting:write", store.reorder, course_id, **payload.model_dump())
    return store.get_course(course_id)


@router.put("/courses/{course_id}/status")
def status(course_id: int, payload: CourseStatus, request: Request):
    _run(request, "meeting:write", store.set_course_status, course_id, **payload.model_dump())
    return store.get_course(course_id)


@router.get("/courses/{course_id}/notes.md")
def notes(course_id: int, request: Request):
    course = _run(request, "meeting:export", store.get_course, course_id)
    if not course["notes_markdown"]:
        raise HTTPException(409, "筆記尚未完成，請查看各段狀態並在收齊後結束課程。")
    return PlainTextResponse(course["notes_markdown"], headers={
        "Content-Disposition": f'attachment; filename="course-{course_id}-notes.md"'})


def _part(request, course_id, part_id):
    course = _run(request, "meeting:read", store.get_course, course_id, include_transcripts=True)
    part = next((p for p in course["parts"] if p["id"] == part_id), None)
    if not part:
        raise HTTPException(404, "找不到此課程的段落，請返回課程清單。")
    return part


@router.get("/courses/{course_id}/parts/{part_id}/transcript")
def transcript(course_id: int, part_id: str, request: Request):
    part = _part(request, course_id, part_id)
    return {"transcript": part["transcript"], "warnings": part["warnings"]}


@router.get("/courses/{course_id}/parts/{part_id}/media")
async def media(course_id: int, part_id: str, request: Request, download: bool = False):
    part = _part(request, course_id, part_id)
    name = part["source_audio"]
    if not name or Path(name).name != name:
        raise HTTPException(404, "此段沒有可播放的原始媒體。")
    source = (SOURCE_AUDIO_DIR / name).resolve()
    if source.parent != SOURCE_AUDIO_DIR.resolve() or not source.is_file():
        raise HTTPException(404, "原始檔目前不在保留資料夾，請聯絡管理員補回來源。")
    path = source
    if not download and source.suffix.lower() == ".webm":
        try:
            path = await asyncio.to_thread(ensure_seekable_playback_media, source, PLAYBACK_MEDIA_DIR, original_name=name)
        except SeekableMediaError:
            path = source
    return FileResponse(path, filename=name, content_disposition_type="attachment" if download else "inline",
                        headers={"Accept-Ranges": "bytes"})
