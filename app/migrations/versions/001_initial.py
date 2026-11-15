"""Create accounts, teams, memberships, and notes.

Revision ID: 001_initial
Revises: None
"""

from alembic import op
import sqlalchemy as sa

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            (sa.column("email") == sa.func.btrim(sa.column("email")))
            & (sa.column("email") != ""),
            name="users_email_check",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.func.btrim(sa.column("display_name"))) > 0,
            name="users_display_name_check",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.func.btrim(sa.column("password_hash"))) > 0,
            name="users_password_hash_check",
        ),
    )
    op.create_index(
        "users_email_key", "users", [sa.func.lower(sa.column("email"))], unique=True
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            sa.func.length(sa.func.btrim(sa.column("name"))) > 0,
            name="teams_name_check",
        ),
    )
    op.create_table(
        "memberships",
        sa.Column(
            "team_id",
            sa.BigInteger(),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            sa.column("role").in_(("owner", "member")), name="memberships_role_check"
        ),
    )
    op.create_index("memberships_user_id_idx", "memberships", ["user_id"])
    op.create_table(
        "notes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "owner_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "team_id", sa.BigInteger(), sa.ForeignKey("teams.id", ondelete="RESTRICT")
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.literal("")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            sa.func.length(sa.func.btrim(sa.column("title"))) > 0,
            name="notes_title_check",
        ),
    )
    op.create_index("notes_owner_id_idx", "notes", ["owner_id"])
    op.create_index("notes_team_id_idx", "notes", ["team_id"])


def downgrade() -> None:
    op.drop_table("notes")
    op.drop_table("memberships")
    op.drop_table("teams")
    op.drop_table("users")
