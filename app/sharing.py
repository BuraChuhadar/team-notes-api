"""Manage read-only note shares to teams the manager belongs to."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api_contract import api_responses
from app.auth import UserResponse, get_current_user
from app.database import get_session
from app.models import Membership, Note, NoteShare

router = APIRouter(prefix="/notes", tags=["sharing"])
MAX_BIGINT = 2**63 - 1


class ShareCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"team_id": 2}]}
    )

    team_id: Annotated[
        int,
        Field(
            strict=True,
            gt=0,
            le=MAX_BIGINT,
            description="Recipient team identifier. The manager must currently belong to this team.",
        ),
    ]


class ShareResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {"note_id": 1, "team_id": 2, "created_at": "2026-11-15T12:00:00Z"}
            ]
        },
    )

    note_id: int = Field(description="Shared note identifier.", examples=[1])
    team_id: int = Field(description="Recipient team identifier.", examples=[2])
    created_at: datetime = Field(
        description="Share creation timestamp with a UTC offset."
    )


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
            membership_query = select(Membership).where(
                Membership.team_id == note.team_id,
                Membership.user_id == user_id,
                Membership.role == "owner",
            )
            if lock:
                membership_query = membership_query.with_for_update(read=True)
            membership = session.scalar(membership_query)
            if membership is not None:
                return note
    raise HTTPException(status_code=404, detail="Note not found")


@router.post(
    "/{note_id}/shares",
    response_model=ShareResponse,
    status_code=201,
    operation_id="create_note_share",
    summary="Share a note with a team",
    description=(
        "Create read-only access for the recipient team's current members. The caller must "
        "own a private note or be an owner of the note's source team, and must also belong "
        "to the recipient team. A share grants no editing, deletion, or share-management "
        "permission. Location identifies the new /notes/{note_id}/shares/{team_id} resource."
    ),
    responses=api_responses(
        201,
        errors={
            401: "A valid bearer session is required.",
            404: "The note cannot be managed by the caller, or the recipient team is inaccessible.",
            409: "The note already belongs to or is already shared with the recipient team.",
        },
        validation=True,
        location=True,
    ),
)
def create_share(
    note_id: Annotated[
        int,
        Path(
            gt=0,
            le=MAX_BIGINT,
            description="Positive identifier of the note to share.",
            examples=[1],
        ),
    ],
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


@router.get(
    "/{note_id}/shares",
    response_model=list[ShareResponse],
    operation_id="list_note_shares",
    summary="List a note's team shares",
    description=(
        "Only the private-note owner or an owner of its source team may list shares. "
        "Recipient-team membership alone does not permit share management. Results are "
        "ordered by creation time descending, then recipient team ID descending. "
        "An authorized caller receives an empty array for an empty or exhausted page."
    ),
    responses=api_responses(
        errors={
            401: "A valid bearer session is required.",
            404: "The note does not exist or cannot be managed by the caller.",
        },
        validation=True,
    ),
)
def list_shares(
    note_id: Annotated[
        int,
        Path(
            gt=0, le=MAX_BIGINT, description="Positive note identifier.", examples=[1]
        ),
    ],
    user: Annotated[UserResponse, Depends(get_current_user)],
    limit: Annotated[
        int, Query(ge=1, le=100, description="Maximum shares per page, from 1 to 100.")
    ] = 50,
    offset: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_BIGINT,
            description="Shares to skip in the documented ordering.",
        ),
    ] = 0,
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


@router.get(
    "/{note_id}/shares/{team_id}",
    response_model=ShareResponse,
    operation_id="get_note_share",
    summary="Read a note's team share",
    description=(
        "Read one share as the private-note owner or an owner of the note's source team. "
        "This management resource is not available solely through recipient-team membership. "
        "Missing notes, inaccessible notes, and missing shares return 404."
    ),
    responses=api_responses(
        errors={
            401: "A valid bearer session is required.",
            404: "The note is missing or inaccessible for management, or the share does not exist.",
        },
        validation=True,
    ),
)
def get_share(
    note_id: Annotated[
        int,
        Path(
            gt=0, le=MAX_BIGINT, description="Positive note identifier.", examples=[1]
        ),
    ],
    team_id: Annotated[
        int,
        Path(
            gt=0,
            le=MAX_BIGINT,
            description="Positive recipient team identifier.",
            examples=[2],
        ),
    ],
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> ShareResponse:
    with get_session() as session:
        managed_note(session, note_id, user.id, lock=False)
        share = session.get(NoteShare, {"note_id": note_id, "team_id": team_id})
        if share is None:
            raise HTTPException(status_code=404, detail="Share not found")
        return ShareResponse.model_validate(share)


@router.delete(
    "/{note_id}/shares/{team_id}",
    status_code=204,
    response_class=Response,
    operation_id="delete_note_share",
    summary="Revoke a note's team share",
    description=(
        "Revoke a recipient team's read-only share as the private-note owner or an owner "
        "of the source team. Recipients lose access through this share on subsequent reads; "
        "other independent access remains valid. Success returns no response body."
    ),
    responses=api_responses(
        204,
        errors={
            401: "A valid bearer session is required.",
            404: "The note is missing or inaccessible for management, or the share does not exist.",
        },
        validation=True,
    ),
)
def delete_share(
    note_id: Annotated[
        int,
        Path(
            gt=0, le=MAX_BIGINT, description="Positive note identifier.", examples=[1]
        ),
    ],
    team_id: Annotated[
        int,
        Path(
            gt=0,
            le=MAX_BIGINT,
            description="Positive recipient team identifier.",
            examples=[2],
        ),
    ],
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> Response:
    with get_session() as session:
        managed_note(session, note_id, user.id)
        share = session.get(NoteShare, {"note_id": note_id, "team_id": team_id})
        if share is None:
            raise HTTPException(status_code=404, detail="Share not found")
        session.delete(share)
    return Response(status_code=204)
