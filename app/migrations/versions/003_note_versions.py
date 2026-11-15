"""Add optimistic versions to existing and new notes.

Revision ID: 003_note_versions
Revises: 002_sessions
"""

from alembic import op
import sqlalchemy as sa

revision = "003_note_versions"
down_revision = "002_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notes",
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.literal(1)),
    )
    op.create_check_constraint("notes_version_check", "notes", sa.column("version") > 0)


def downgrade() -> None:
    op.drop_constraint("notes_version_check", "notes", type_="check")
    op.drop_column("notes", "version")
