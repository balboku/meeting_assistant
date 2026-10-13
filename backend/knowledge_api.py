"""Authenticated knowledge and shared-terminology routes."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.auth import actor_from_request, require_permission
from backend import company_vocabulary as vocabulary
from backend.local_speech import capabilities
from backend.meeting_knowledge import answer
from backend.processing_policy import cloud_enabled

router = APIRouter(prefix="/knowledge", tags=["會議知識庫"])


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    meeting_ids: list[int] = Field(default_factory=list, max_length=100)
    approved_only: bool = False
    use_cloud: bool = False


class Term(BaseModel):
    canonical: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=1000)
    meeting_id: int | None = Field(default=None, ge=1)


class TermReview(BaseModel):
    status: Literal["approved", "disabled"]
    version: int = Field(ge=1)


@router.get("/capabilities")
def get_capabilities(request: Request):
    actor = actor_from_request(request)
    require_permission(actor, "meeting:read")
    return {**capabilities(), "cloud_enabled": cloud_enabled(),
            "can_propose": actor.can("meeting:write"), "can_approve": actor.can("vocabulary:manage")}


@router.post("/ask")
def ask(payload: Question, request: Request):
    require_permission(actor_from_request(request), "meeting:read")
    return answer(**payload.model_dump())


@router.get("/vocabulary")
def terms(request: Request):
    require_permission(actor_from_request(request), "meeting:read")
    return {"items": vocabulary.list_terms()}


@router.post("/vocabulary", status_code=201)
def propose_term(payload: Term, request: Request):
    actor = actor_from_request(request)
    require_permission(actor, "meeting:write")
    try:
        return {"id": vocabulary.propose(**payload.model_dump(), actor=actor.email), "status": "proposed"}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/vocabulary/{term_id}")
def review_term(term_id: int, payload: TermReview, request: Request):
    actor = actor_from_request(request)
    require_permission(actor, "vocabulary:manage")
    try:
        vocabulary.review(term_id, **payload.model_dump(), actor=actor.email)
        return {"status": payload.status}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
