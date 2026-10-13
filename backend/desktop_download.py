"""Serve an explicitly built, credential-free desktop recording package."""
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from backend.auth import actor_from_request, require_permission

router = APIRouter(tags=["錄製工具"])


@router.get("/downloads/desktop-recorder")
def desktop_recorder(request: Request):
    require_permission(actor_from_request(request), "meeting:read")
    archive = Path(__file__).resolve().parents[1] / "dist" / "MeetingRecorder-Windows.zip"
    if not archive.is_file():
        raise HTTPException(409, "桌面錄音套件尚未建置，請管理員執行 scripts/build_desktop_recorder.py；暫可使用瀏覽器錄製或上傳會議平台原始錄音。")
    return FileResponse(archive, media_type="application/zip", filename=archive.name)
