"""Persist shared login budgets without storing raw email or network addresses.

Revision ID: 006_login_throttle
Revises: 005_query_indexes
"""

from alembic import op
import sqlalchemy as sa

revision = "006_login_throttle"
down_revision = "005_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_throttles",
        sa.Column("subject_hash", sa.Text(), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            sa.column("attempts") > 0, name="login_throttles_attempts_check"
        ),
        sa.CheckConstraint(
            sa.column("subject_hash").regexp_match("^[0-9a-f]{64}$"),
            name="login_throttles_subject_hash_check",
        ),
    )
    op.create_index(
        "login_throttles_window_idx", "login_throttles", ["window_started_at"]
    )


def downgrade() -> None:
    op.drop_table("login_throttles")
