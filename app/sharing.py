"""Manage read-only note shares to teams the manager belongs to."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import UserResponse, get_current_user
from app.database import get_session
from app.models import Membership, Note, NoteShare

router = APIRouter(prefix="/notes", tags=["sharing"])
MAX_BIGINT = 2**63 - 1


class ShareCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: Annotated[int, Field(strict=True, gt=0, le=MAX_BIGINT)]


class ShareResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    note_id: int
    team_id: int
    created_at: datetime


def managed_note(
    session: Session, note_id: int, user_id: int, *, lock: bool = True
) -> Note:
    """Authorize management, locking write requests until they commit."""
    statement = select(Note).where(Note.id == note_id)
    if lock:
        statement = statement.with_for_update()
    note = session.scalar(statement)
    if note is not None:
        if note.team_id is None and note.owner_id == user_id:
            return note
        if note.team_id is not None:
            membership_query = (
                select(Membership)
                .where(
                    Membership.team_id == note.team_id,
                    Membership.user_id == user_id,
                    Membership.role == "owner",
                )
            )
            if lock:
                membership_query = membership_query.with_for_update(read=True)
            membership = session.scalar(membership_query)
            if membership is not None:
                return note
    raise HTTPException(status_code=404, detail="Note not found")


@router.post("/{note_id}/shares", response_model=ShareResponse, status_code=201)
def create_share(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT)],
    payload: ShareCreate,
    response: Response,
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> ShareResponse:
    with get_session() as session:
        note = managed_note(session, note_id, user.id)
        if note.team_id == payload.team_id:
            raise HTTPException(
                status_code=409, detail="Note already belongs to this team"
            )
        recipient_membership = session.scalar(
            select(Membership)
            .where(Membership.team_id == payload.team_id, Membership.user_id == user.id)
            .with_for_update(read=True)
        )
        if recipient_membership is None:
            raise HTTPException(status_code=404, detail="Team not found")
        share = session.scalar(
            insert(NoteShare)
            .values(note_id=note.id, team_id=payload.team_id)
            .on_conflict_do_nothing(
                index_elements=[NoteShare.note_id, NoteShare.team_id]
            )
            .returning(NoteShare)
        )
        if share is None:
            raise HTTPException(
                status_code=409, detail="Note is already shared with this team"
            )
        result = ShareResponse.model_validate(share)
    response.headers["Location"] = f"/notes/{note_id}/shares/{result.team_id}"
    return result


@router.get("/{note_id}/shares", response_model=list[ShareResponse])
def list_shares(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT)],
    user: Annotated[UserResponse, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_BIGINT)] = 0,
) -> list[ShareResponse]:
    with get_session() as session:
        managed_note(session, note_id, user.id, lock=False)
        shares = session.scalars(
            select(NoteShare)
            .where(NoteShare.note_id == note_id)
            .order_by(NoteShare.created_at.desc(), NoteShare.team_id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [ShareResponse.model_validate(share) for share in shares]


@router.get("/{note_id}/shares/{team_id}", response_model=ShareResponse)
def get_share(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT)],
    team_id: Annotated[int, Path(gt=0, le=MAX_BIGINT)],
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> ShareResponse:
    with get_session() as session:
        managed_note(session, note_id, user.id, lock=False)
        share = session.get(NoteShare, {"note_id": note_id, "team_id": team_id})
        if share is None:
            raise HTTPException(status_code=404, detail="Share not found")
        return ShareResponse.model_validate(share)


@router.delete("/{note_id}/shares/{team_id}", status_code=204, response_class=Response)
def delete_share(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT)],
    team_id: Annotated[int, Path(gt=0, le=MAX_BIGINT)],
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> Response:
    with get_session() as session:
        managed_note(session, note_id, user.id)
        share = session.get(NoteShare, {"note_id": note_id, "team_id": team_id})
        if share is None:
            raise HTTPException(status_code=404, detail="Share not found")
        session.delete(share)
    return Response(status_code=204)
