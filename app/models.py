"""Typed SQLAlchemy mappings for accounts, teams, notes, and sessions."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    column,
    func,
    literal,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            (column("email") == func.btrim(column("email"))) & (column("email") != ""),
            name="users_email_check",
        ),
        CheckConstraint(
            func.length(func.btrim(column("display_name"))) > 0,
            name="users_display_name_check",
        ),
        CheckConstraint(
            func.length(func.btrim(column("password_hash"))) > 0,
            name="users_password_hash_check",
        ),
        Index("users_email_key", func.lower(column("email")), unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint(
            func.length(func.btrim(column("name"))) > 0,
            name="teams_name_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            column("role").in_(("owner", "member")), name="memberships_role_check"
        ),
        Index("memberships_user_id_idx", "user_id"),
    )

    team_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint(
            func.length(func.btrim(column("title"))) > 0, name="notes_title_check"
        ),
        Index("notes_owner_id_idx", "owner_id"),
        Index("notes_team_id_idx", "team_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT")
    )
    team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, server_default=literal(""))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class AuthSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            column("token_hash").regexp_match("^[0-9a-f]{64}$"),
            name="sessions_token_hash_check",
        ),
        CheckConstraint(
            column("expires_at") > column("created_at"), name="sessions_check"
        ),
        Index("sessions_user_id_idx", "user_id"),
        Index("sessions_expires_at_idx", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
