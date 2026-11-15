"""Team creation and owner-managed membership through SQLAlchemy ORM models."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from app.auth import UserResponse, get_current_user
from app.database import get_session
from app.models import Membership, Team, User

router = APIRouter(prefix="/teams", tags=["teams"])
CurrentUser = Annotated[UserResponse, Depends(get_current_user)]
TeamId = Annotated[int, Path(gt=0, le=2**63 - 1)]
UserId = Annotated[int, Path(gt=0, le=2**63 - 1)]


class CreateTeamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class TeamResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    role: Literal["owner", "member"]


class MemberResponse(BaseModel):
    user_id: int
    display_name: str
    role: Literal["owner", "member"]
    created_at: datetime


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


@router.post("", response_model=TeamResponse, status_code=201)
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


@router.get("", response_model=list[TeamResponse])
def list_teams(user: CurrentUser) -> list[TeamResponse]:
    with get_session() as session:
        teams = session.execute(
            select(Team.id, Team.name, Team.created_at, Membership.role)
            .join(Membership, Membership.team_id == Team.id)
            .where(Membership.user_id == user.id)
            .order_by(Team.id)
        ).mappings()
        return [TeamResponse.model_validate(team) for team in teams]


@router.get("/{team_id}/members", response_model=list[MemberResponse])
def list_members(team_id: TeamId, user: CurrentUser) -> list[MemberResponse]:
    caller = aliased(Membership)
    with get_session() as session:
        members = session.execute(
            select(
                Membership.user_id, User.display_name,
                Membership.role, Membership.created_at,
            )
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .join(
                caller,
                and_(caller.team_id == Membership.team_id, caller.user_id == user.id),
            )
            .where(Membership.team_id == team_id)
            .order_by(Membership.user_id)
        ).mappings().all()
        # An authorized caller always sees at least their own membership.
        if not members:
            raise HTTPException(status_code=404, detail="Team not found")
        return [MemberResponse.model_validate(member) for member in members]


@router.post("/{team_id}/members", response_model=MemberResponse, status_code=201)
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
        return MemberResponse.model_validate({
            "user_id": membership.user_id,
            "display_name": display_name,
            "role": membership.role,
            "created_at": membership.created_at,
        })


@router.delete("/{team_id}/members/{user_id}", status_code=204, response_class=Response)
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
                raise HTTPException(status_code=409, detail="Team owners cannot be removed")
            raise HTTPException(status_code=404, detail="Member not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
