"""Add explicit read-only note grants to recipient teams."""

from alembic import op
import sqlalchemy as sa

revision = "004_note_shares"
down_revision = "003_note_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "note_shares",
        sa.Column("note_id", sa.BigInteger(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("note_id", "team_id"),
    )
    op.create_index("note_shares_team_id_idx", "note_shares", ["team_id"])


def downgrade() -> None:
    op.drop_index("note_shares_team_id_idx", table_name="note_shares")
    op.drop_table("note_shares")
