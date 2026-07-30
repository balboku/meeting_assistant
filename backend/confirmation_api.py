"""API router for the structured-minutes confirmation queue."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.auth import actor_from_request
from backend.confirmation_queue import (
    list_confirmation_tasks,
    update_confirmation_task,
)


router = APIRouter(prefix="/meetings/confirmation-tasks", tags=["會議確認"])


class ConfirmationTaskUpdate(BaseModel):
    status: Literal["resolved", "waived"]
    resolution_value: str | None = Field(default=None, max_length=1000)
    resolution_note: str | None = Field(default=None, max_length=2000)


@router.get("")
async def get_confirmation_tasks(
    status: Literal["pending", "resolved", "waived", "all"] = "pending",
    meeting_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return {
        "items": list_confirmation_tasks(
            status=status,
            meeting_id=meeting_id,
            limit=limit,
            offset=offset,
        )
    }


@router.patch("/{task_id}")
async def patch_confirmation_task(
    task_id: int,
    payload: ConfirmationTaskUpdate,
    request: Request,
):
    actor = actor_from_request(request)
    try:
        return update_confirmation_task(
            task_id,
            status=payload.status,
            resolution_value=payload.resolution_value,
            resolution_note=payload.resolution_note,
            actor_email=actor.email,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
