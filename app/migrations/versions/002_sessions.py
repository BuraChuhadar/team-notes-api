"""Create revocable authentication sessions.

Revision ID: 002_sessions
Revises: 001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "002_sessions"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            sa.column("token_hash").regexp_match("^[0-9a-f]{64}$"),
            name="sessions_token_hash_check",
        ),
        sa.CheckConstraint(
            sa.column("expires_at") > sa.column("created_at"), name="sessions_check"
        ),
    )
    op.create_index("sessions_user_id_idx", "sessions", ["user_id"])
    op.create_index("sessions_expires_at_idx", "sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("sessions")
