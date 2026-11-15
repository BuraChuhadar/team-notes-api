"""Personal and team notes with current access checks and versioned writes."""

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)
from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.api_contract import api_responses
from app.auth import UserResponse, get_current_user
from app.database import get_session
from app.models import Membership, Note, NoteShare
from app.validation import PostgresText

router = APIRouter(prefix="/notes", tags=["notes"])
MAX_BIGINT = 2**63 - 1
SearchText = Annotated[
    PostgresText, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class NoteCursor(BaseModel):
    """A position, not an authorization token; permissions are checked on every page."""

    model_config = ConfigDict(extra="forbid", strict=True)

    created_at: AwareDatetime
    note_id: Annotated[int, Field(gt=0, le=MAX_BIGINT)]
    query_hash: Annotated[str, StringConstraints(pattern="^[0-9a-f]{64}$")]


def cursor_query_hash(user_id: int, q: str | None) -> str:
    # Bind a continuation to its normalized search and caller, while allowing
    # page-size changes and evaluating current membership on every request.
    query = json.dumps([user_id, q], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def invalid_cursor(message: str = "Invalid note cursor") -> RequestValidationError:
    return RequestValidationError(
        [{"type": "value_error", "loc": ("query", "cursor"), "msg": message}]
    )


def decode_note_cursor(value: str, user_id: int, q: str | None) -> NoteCursor:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        position = NoteCursor.model_validate_json(decoded)
    except (ValueError, binascii.Error, ValidationError):
        raise invalid_cursor() from None
    if position.query_hash != cursor_query_hash(user_id, q):
        raise invalid_cursor("Use the same account and search query with this cursor")
    return position


def encode_note_cursor(note: "NoteResponse", user_id: int, q: str | None) -> str:
    position = NoteCursor(
        created_at=note.created_at,
        note_id=note.id,
        query_hash=cursor_query_hash(user_id, q),
    )
    return (
        base64.urlsafe_b64encode(position.model_dump_json().encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


NOTE_LIST_RESPONSES = api_responses(
    errors={401: "A valid bearer session is required."}, validation=True
)
NOTE_LIST_RESPONSES[200]["headers"].update(
    {
        "X-Next-Cursor": {
            "description": "Opaque continuation position for the next visible page; absent when there is no next page. Reuse the same account and q value.",
            "schema": {"type": "string"},
        },
        "Link": {
            "description": "A URI reference with rel=next, preserving limit and the normalized search query. Absent on the final page.",
            "schema": {"type": "string"},
        },
    }
)


def writable_to(user_id: int) -> ColumnElement[bool]:
    membership_exists = (
        select(Membership.team_id)
        .where(Membership.team_id == Note.team_id, Membership.user_id == user_id)
        .exists()
    )
    return or_(
        and_(Note.team_id.is_(None), Note.owner_id == user_id), membership_exists
    )


def visible_to(user_id: int) -> ColumnElement[bool]:
    shared_with_team = (
        select(NoteShare.note_id)
        .join(Membership, Membership.team_id == NoteShare.team_id)
        .where(NoteShare.note_id == Note.id, Membership.user_id == user_id)
        .exists()
    )
    return or_(writable_to(user_id), shared_with_team)


class NoteCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "title": "Meeting notes",
                    "content": "Agree on next steps.",
                    "team_id": None,
                }
            ]
        },
    )

    title: Annotated[
        PostgresText,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
        Field(description="Note title; leading and trailing whitespace is removed."),
    ]
    content: PostgresText = Field(
        default="",
        max_length=100_000,
        description="Note content; whitespace and Unicode are preserved.",
    )
    team_id: (
        Annotated[
            int,
            Field(
                strict=True,
                gt=0,
                le=MAX_BIGINT,
                description="Source team ID; omit or use null for a private note.",
            ),
        ]
        | None
    ) = None


class NoteUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "title": "Updated meeting notes",
                    "content": "Decisions and action items.",
                    "version": 1,
                }
            ]
        },
    )

    title: Annotated[
        PostgresText,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
        Field(
            description="Replacement title; leading and trailing whitespace is removed."
        ),
    ]
    content: PostgresText = Field(
        max_length=100_000,
        description="Replacement content, including an empty string.",
    )
    version: Annotated[
        int,
        Field(
            strict=True,
            gt=0,
            le=MAX_BIGINT,
            description="Version from the latest read; a stale version returns 409.",
        ),
    ]


class NoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Server-assigned note ID.", examples=[1])
    owner_id: int = Field(
        description="Account that created the note; assigned from the authenticated user."
    )
    team_id: int | None = Field(
        description="Source team ID, or null for a private note."
    )
    title: str = Field(description="Trimmed note title.")
    content: str = Field(
        description="Note content with whitespace and Unicode preserved."
    )
    created_at: datetime = Field(description="Creation time, with a UTC offset.")
    updated_at: datetime = Field(
        description="Last content update time, with a UTC offset."
    )
    version: int = Field(
        description="Current optimistic version; supply it when replacing or deleting the note."
    )


@router.post(
    "",
    response_model=NoteResponse,
    status_code=201,
    operation_id="create_note",
    summary="Create a private or team note",
    description=(
        "Create a note owned by the authenticated account. A source team requires "
        "current membership. Omit team_id or use null for a private note. The response "
        "includes the canonical note URI in Location and an initial version of 1."
    ),
    responses=api_responses(
        201,
        errors={
            401: "A valid bearer session is required.",
            404: "The team does not exist or is inaccessible.",
        },
        validation=True,
        location=True,
    ),
)
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


@router.get(
    "",
    response_model=list[NoteResponse],
    operation_id="list_notes",
    summary="List visible notes",
    description=(
        "Return private notes owned by the caller, source-team notes for current "
        "members, and notes shared read-only with a team the caller currently belongs "
        "to. Apply literal case-insensitive title/content search and access filters before "
        "pagination. Order by created_at descending, then ID "
        "descending. Follow Link or X-Next-Cursor for efficient continuation without "
        "deep offsets. Cursor pagination rechecks current access and omits newly "
        "inserted notes ahead of the current position. Empty or exhausted pages return "
        "an empty array. Cursors cannot be combined with a nonzero offset."
    ),
    responses=NOTE_LIST_RESPONSES,
)
def list_notes(
    response: Response,
    user: Annotated[UserResponse, Depends(get_current_user)],
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Maximum number of visible notes to return."),
    ] = 50,
    offset: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_BIGINT,
            description="Number of visible notes to skip after access filtering.",
        ),
    ] = 0,
    q: Annotated[
        SearchText | None,
        Query(
            description=(
                "Optional case-insensitive literal substring of the title or content. "
                "Leading/trailing whitespace is removed; the remaining query must "
                "contain 1–200 characters. SQL wildcard characters are treated literally."
            ),
        ),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=1024,
            pattern="^[A-Za-z0-9_-]+$",
            description="Opaque continuation from X-Next-Cursor or Link. Reuse the same account and q; do not combine with a nonzero offset.",
        ),
    ] = None,
) -> list[NoteResponse]:
    statement = select(Note).where(visible_to(user.id))
    if cursor is not None:
        if offset != 0:
            raise invalid_cursor("Do not combine cursor with a nonzero offset")
        position = decode_note_cursor(cursor, user.id, q)
        statement = statement.where(
            tuple_(Note.created_at, Note.id)
            < tuple_(position.created_at, position.note_id)
        )
    if q is not None:
        statement = statement.where(
            or_(
                Note.title.icontains(q, autoescape=True),
                Note.content.icontains(q, autoescape=True),
            )
        )
    with get_session() as session:
        notes = list(
            session.scalars(
                statement.order_by(Note.created_at.desc(), Note.id.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        )
        page = [NoteResponse.model_validate(note) for note in notes[:limit]]
        if len(notes) > limit:
            next_cursor = encode_note_cursor(page[-1], user.id, q)
            parameters: dict[str, str | int] = {"limit": limit, "cursor": next_cursor}
            if q is not None:
                parameters["q"] = q
            response.headers["X-Next-Cursor"] = next_cursor
            response.headers["Link"] = f'<?{urlencode(parameters)}>; rel="next"'
        return page


@router.get(
    "/{note_id}",
    response_model=NoteResponse,
    operation_id="get_note",
    summary="Read a visible note",
    description=(
        "Return a note and its current version if the caller owns a personal note, is a "
        "current source-team member, or has a current recipient-team grant. Missing and "
        "inaccessible notes return the same 404 response. Read-only sharing does not "
        "grant update, delete, or share-management permissions."
    ),
    responses=api_responses(
        errors={
            401: "A valid bearer session is required.",
            404: "The note does not exist or is inaccessible.",
        },
        validation=True,
    ),
)
def get_note(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT, description="Note ID.")],
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> NoteResponse:
    with get_session() as session:
        note = session.scalar(
            select(Note).where(Note.id == note_id, visible_to(user.id))
        )
        if note is None:
            raise HTTPException(status_code=404, detail="Note not found")
        return NoteResponse.model_validate(note)


def note_for_write(
    session: Session, note_id: int, user_id: int, expected_version: int
) -> Note:
    # Lock the note before checking its version so concurrent saves serialize.
    note = session.scalar(
        select(Note)
        .where(Note.id == note_id, writable_to(user_id))
        .with_for_update(of=Note)
    )
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.team_id is not None:
        # Recheck and hold membership through the write: removal may have
        # committed while this request waited for the note lock.
        membership = session.scalar(
            select(Membership.user_id)
            .where(Membership.team_id == note.team_id, Membership.user_id == user_id)
            .with_for_update(read=True)
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="Note not found")
    if note.version != expected_version:
        raise HTTPException(
            status_code=409, detail="Note version conflict; reload and retry"
        )
    return note


@router.put(
    "/{note_id}",
    response_model=NoteResponse,
    operation_id="update_note",
    summary="Replace a note's editable content",
    description=(
        "Replace both title and content; both fields and the version from the latest "
        "read are required. Ownership and source team remain unchanged. The private "
        "owner or a current source-team member may update; read-only recipients may "
        "not. A successful write increments version and updated_at. Stale versions "
        "return 409 without changes; read the note again before retrying."
    ),
    responses=api_responses(
        errors={
            401: "A valid bearer session is required.",
            404: "The note does not exist or is not writable by the caller.",
            409: "The version is stale or the version limit has been reached. Reload before retrying.",
        },
        validation=True,
    ),
)
def update_note(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT, description="Note ID.")],
    payload: NoteUpdate,
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> NoteResponse:
    with get_session() as session:
        note = note_for_write(session, note_id, user.id, payload.version)
        if note.version == MAX_BIGINT:
            raise HTTPException(status_code=409, detail="Note version limit reached")
        note.title = payload.title
        note.content = payload.content
        note.version += 1
        note.updated_at = session.execute(select(func.clock_timestamp())).scalar_one()
        session.flush()
        return NoteResponse.model_validate(note)


@router.delete(
    "/{note_id}",
    status_code=204,
    response_class=Response,
    operation_id="delete_note",
    summary="Delete a note at its current version",
    description=(
        "Delete a note only when the required version matches the current version. "
        "The private owner or a current source-team member may delete; read-only "
        "recipients may not. Success returns an empty 204 response. Missing or "
        "inaccessible notes return 404, including on a repeated deletion."
    ),
    responses=api_responses(
        204,
        errors={
            401: "A valid bearer session is required.",
            404: "The note does not exist or is not writable by the caller.",
            409: "The supplied version is stale. Read the current note before retrying.",
        },
        validation=True,
    ),
)
def delete_note(
    note_id: Annotated[int, Path(gt=0, le=MAX_BIGINT, description="Note ID.")],
    version: Annotated[
        int,
        Query(
            gt=0,
            le=MAX_BIGINT,
            description="Version from the latest read, required to avoid deleting a newer edit.",
        ),
    ],
    user: Annotated[UserResponse, Depends(get_current_user)],
) -> Response:
    with get_session() as session:
        note = note_for_write(session, note_id, user.id, version)
        session.delete(note)
    return Response(status_code=204)
