"""Create and read personal or team notes using current team membership."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.auth import UserResponse, get_current_user
from app.database import get_session
from app.models import Membership, Note

router = APIRouter(prefix="/notes", tags=["notes"])
MAX_BIGINT = 2**63 - 1


def visible_to(user_id: int) -> ColumnElement[bool]:
    membership_exists = (
        select(Membership.team_id)
        .where(Membership.team_id == Note.team_id, Membership.user_id == user_id)
        .exists()
    )
    return or_(and_(Note.team_id.is_(None), Note.owner_id == user_id), membership_exists)


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    content: str = Field(default="", max_length=100_000)
    team_id: Annotated[int, Field(strict=True, gt=0, le=MAX_BIGINT)] | None = None


class NoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    team_id: int | None
    title: str
    content: str
    created_at: datetime
    updated_at: datetime


@router.post("", response_model=NoteResponse, status_code=201)
def create_note(
    payload: NoteCreate,
    response: Response,
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> NoteResponse:
    with get_session() as session:
        if payload.team_id is not None:
            # Serialize note creation with removal of the author's membership.
            membership = session.scalar(
                select(Membership)
                .where(
                    Membership.team_id == payload.team_id, Membership.user_id == user.id
                )
                .with_for_update(read=True)
            )
            if membership is None:
                raise HTTPException(status_code=404, detail="Team not found")
        note = Note(
            owner_id=user.id,
            team_id=payload.team_id,
            title=payload.title,
            content=payload.content,
        )
        session.add(note)
        session.flush()
        result = NoteResponse.model_validate(note)
    response.headers["Location"] = f"/notes/{result.id}"
    return result


@router.get("", response_model=list[NoteResponse])
def list_notes(
    user: Annotated[UserResponse, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
) -> list[NoteResponse]:
    with get_session() as session:
        notes = session.scalars(
            select(Note)
            .where(visible_to(user.id))
            .order_by(Note.created_at.desc(), Note.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [NoteResponse.model_validate(note) for note in notes]


@router.get("/{note_id}", response_model=NoteResponse)
def get_note(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT)],
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> NoteResponse:
    with get_session() as session:
        note = session.scalar(select(Note).where(Note.id == note_id, visible_to(user.id)))
        if note is None:
            raise HTTPException(status_code=404, detail="Note not found")
        return NoteResponse.model_validate(note)
