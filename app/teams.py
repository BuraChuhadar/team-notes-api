"""Team creation and owner-managed membership through SQLAlchemy ORM models."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api_contract import api_responses
from app.auth import UserResponse, get_current_user
from app.database import get_session
from app.models import Membership, Team, User
from app.validation import PostgresText

router = APIRouter(prefix="/teams", tags=["teams"])
CurrentUser = Annotated[UserResponse, Depends(get_current_user)]
TeamId = Annotated[
    int, Path(gt=0, le=2**63 - 1, description="Positive team identifier.", examples=[1])
]
UserId = Annotated[
    int,
    Path(
        gt=0,
        le=2**63 - 1,
        description="Positive registered-user identifier.",
        examples=[2],
    ),
]


class CreateTeamRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"name": "Engineering"}]}
    )

    name: Annotated[
        PostgresText,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        Field(
            description="Team name, trimmed before validation. Duplicate names are allowed."
        ),
    ]


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"user_id": 2}]}
    )

    user_id: Annotated[
        int,
        Field(
            strict=True,
            gt=0,
            le=2**63 - 1,
            description="Existing user's identifier. The server assigns the member role.",
        ),
    ]


class TeamResponse(BaseModel):
    id: int = Field(description="Team identifier.", examples=[1])
    name: str = Field(description="Team name.", examples=["Engineering"])
    created_at: datetime = Field(
        description="Team creation timestamp with a UTC offset."
    )
    role: Literal["owner", "member"] = Field(
        description="The authenticated caller's team role."
    )


class MemberResponse(BaseModel):
    user_id: int = Field(
        description="Member's registered-user identifier.", examples=[2]
    )
    display_name: str = Field(
        description="Public display name.", examples=["Sample User"]
    )
    role: Literal["owner", "member"] = Field(
        description="Member's role within this team."
    )
    created_at: datetime = Field(
        description="Membership creation timestamp with a UTC offset."
    )


def require_team_owner(session: Session, team_id: int, user_id: int) -> None:
    # Keep authorization stable until the membership mutation commits.
    role = session.scalar(
        select(Membership.role)
        .where(Membership.team_id == team_id, Membership.user_id == user_id)
        .with_for_update()
    )
    if role is None:
        # Outsiders receive the same response as for a missing team.
        raise HTTPException(status_code=404, detail="Team not found")
    if role != "owner":
        raise HTTPException(status_code=403, detail="Team owner permission required")


@router.post(
    "",
    response_model=TeamResponse,
    status_code=201,
    operation_id="create_team",
    summary="Create a team",
    description=(
        "Create a team and its authenticated creator's owner membership in one transaction. "
        "Clients cannot choose an owner or supply a role."
    ),
    responses=api_responses(
        201, errors={401: "A valid bearer session is required."}, validation=True
    ),
)
def create_team(payload: CreateTeamRequest, user: CurrentUser) -> TeamResponse:
    with get_session() as session:
        team = Team(name=payload.name)
        session.add(team)
        session.flush()
        session.add(Membership(team_id=team.id, user_id=user.id, role="owner"))
        session.flush()
        return TeamResponse(
            id=team.id, name=team.name, created_at=team.created_at, role="owner"
        )


@router.get(
    "",
    response_model=list[TeamResponse],
    operation_id="list_teams",
    summary="List your teams",
    description=(
        "Return only teams in which the caller currently has membership, ordered by team ID "
        "ascending. Pagination applies after membership filtering. An empty or exhausted page "
        "returns an empty array."
    ),
    responses=api_responses(
        errors={401: "A valid bearer session is required."}, validation=True
    ),
)
def list_teams(
    user: CurrentUser,
    limit: Annotated[
        int, Query(ge=1, le=100, description="Maximum results per page, from 1 to 100.")
    ] = 50,
    offset: Annotated[
        int,
        Query(
            ge=0,
            le=2**63 - 1,
            description="Results to skip after membership filtering.",
        ),
    ] = 0,
) -> list[TeamResponse]:
    with get_session() as session:
        teams = session.execute(
            select(Team.id, Team.name, Team.created_at, Membership.role)
            .join(Membership, Membership.team_id == Team.id)
            .where(Membership.user_id == user.id)
            .order_by(Team.id)
            .limit(limit)
            .offset(offset)
        ).mappings()
        return [TeamResponse.model_validate(team) for team in teams]


@router.get(
    "/{team_id}/members",
    response_model=list[MemberResponse],
    operation_id="list_team_members",
    summary="List a team's members",
    description=(
        "Any current team member can read the public roster, ordered by user ID ascending. "
        "Email addresses and credentials are excluded. Missing teams and inaccessible teams "
        "both return 404; an authorized caller receives an empty array for an exhausted page."
    ),
    responses=api_responses(
        errors={
            401: "A valid bearer session is required.",
            404: "Team not found or inaccessible.",
        },
        validation=True,
    ),
)
def list_members(
    team_id: TeamId,
    user: CurrentUser,
    limit: Annotated[
        int,
        Query(
            ge=1, le=100, description="Maximum roster entries per page, from 1 to 100."
        ),
    ] = 50,
    offset: Annotated[
        int,
        Query(
            ge=0, le=2**63 - 1, description="Roster entries to skip in user-ID order."
        ),
    ] = 0,
) -> list[MemberResponse]:
    with get_session() as session:
        # Hold membership stable while reading the page, including an empty page.
        membership = session.scalar(
            select(Membership.team_id)
            .where(Membership.team_id == team_id, Membership.user_id == user.id)
            .with_for_update(read=True)
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="Team not found")
        members = (
            session.execute(
                select(
                    Membership.user_id,
                    User.display_name,
                    Membership.role,
                    Membership.created_at,
                )
                .select_from(Membership)
                .join(User, User.id == Membership.user_id)
                .where(Membership.team_id == team_id)
                .order_by(Membership.user_id)
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
        return [MemberResponse.model_validate(member) for member in members]


@router.post(
    "/{team_id}/members",
    response_model=MemberResponse,
    status_code=201,
    operation_id="add_team_member",
    summary="Add a team member",
    description=(
        "A current owner can add an existing registered user with the member role. "
        "Client-supplied roles are rejected. Outsiders receive 404 before user lookup."
    ),
    responses=api_responses(
        201,
        errors={
            401: "A valid bearer session is required.",
            403: "The caller is a member but is not a team owner.",
            404: "Team not found or inaccessible, or the specified user does not exist.",
            409: "The user already belongs to this team, including an existing owner.",
        },
        validation=True,
    ),
)
def add_member(
    team_id: TeamId, payload: AddMemberRequest, user: CurrentUser
) -> MemberResponse:
    with get_session() as session:
        require_team_owner(session, team_id, user.id)
        display_name = session.scalar(
            select(User.display_name).where(User.id == payload.user_id)
        )
        if display_name is None:
            raise HTTPException(status_code=404, detail="User not found")
        membership = session.scalar(
            insert(Membership)
            .values(team_id=team_id, user_id=payload.user_id, role="member")
            .on_conflict_do_nothing(
                index_elements=[Membership.team_id, Membership.user_id]
            )
            .returning(Membership)
        )
        if membership is None:
            raise HTTPException(status_code=409, detail="User is already a team member")
        return MemberResponse.model_validate(
            {
                "user_id": membership.user_id,
                "display_name": display_name,
                "role": membership.role,
                "created_at": membership.created_at,
            }
        )


@router.delete(
    "/{team_id}/members/{user_id}",
    status_code=204,
    response_class=Response,
    operation_id="remove_team_member",
    summary="Remove a team member",
    description=(
        "A current owner can remove a member. Owners cannot be removed or demoted. "
        "Successful removal returns no body and revokes subsequent team access immediately. "
        "Missing teams and inaccessible teams share the same 404 response."
    ),
    responses=api_responses(
        204,
        errors={
            401: "A valid bearer session is required.",
            403: "The caller is a member but is not a team owner.",
            404: "Team not found or inaccessible, or the specified member does not exist.",
            409: "The specified user is an owner and cannot be removed.",
        },
        validation=True,
    ),
)
def remove_member(team_id: TeamId, user_id: UserId, user: CurrentUser) -> Response:
    with get_session() as session:
        require_team_owner(session, team_id, user.id)
        removed = session.scalar(
            delete(Membership)
            .where(
                Membership.team_id == team_id,
                Membership.user_id == user_id,
                Membership.role == "member",
            )
            .returning(Membership.user_id)
        )
        if removed is None:
            role = session.scalar(
                select(Membership.role).where(
                    Membership.team_id == team_id, Membership.user_id == user_id
                )
            )
            if role == "owner":
                raise HTTPException(
                    status_code=409, detail="Team owners cannot be removed"
                )
            raise HTTPException(status_code=404, detail="Member not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
