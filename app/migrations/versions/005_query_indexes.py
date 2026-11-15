"""Add ordered note-list indexes and trigram indexes for literal substring search.

The trusted pg_trgm extension lives outside application/test schemas. Dropping an
isolated application schema must not remove operator classes used by other ones.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import CreateSchema

from app.config import get_settings

revision = "005_query_indexes"
down_revision = "004_note_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    extension_schema_name = get_settings().db_trgm_schema
    quoted_schema = connection.dialect.identifier_preparer.quote(extension_schema_name)
    extensions = sa.table(
        "pg_extension",
        sa.column("extname", sa.Text),
        sa.column("extnamespace", sa.Integer),
        schema="pg_catalog",
    )
    namespaces = sa.table(
        "pg_namespace",
        sa.column("oid", sa.Integer),
        sa.column("nspname", sa.Text),
        schema="pg_catalog",
    )
    extension_schema = connection.scalar(
        sa.select(namespaces.c.nspname)
        .select_from(
            extensions.join(namespaces, extensions.c.extnamespace == namespaces.c.oid)
        )
        .where(extensions.c.extname == "pg_trgm")
    )
    if extension_schema is not None and extension_schema != extension_schema_name:
        raise RuntimeError(
            "pg_trgm is installed in a different schema. Set DB_TRIGRAM_SCHEMA "
            f"to {extension_schema!r} before starting the application or running "
            "migrations. This migration will not relocate an extension that "
            "another application may use."
        )
    if extension_schema is None:
        op.execute(CreateSchema(extension_schema_name, if_not_exists=True))
        # PostgreSQL-specific extension provisioning has no SQLAlchemy schema
        # primitive. This fixed migration DDL contains no application input.
        op.execute(sa.DDL(f"CREATE EXTENSION pg_trgm WITH SCHEMA {quoted_schema}"))

    op.create_index(
        "notes_created_id_idx",
        "notes",
        [sa.column("created_at").desc(), sa.column("id").desc()],
    )
    op.create_index(
        "notes_owner_created_id_idx",
        "notes",
        ["owner_id", sa.column("created_at").desc(), sa.column("id").desc()],
        postgresql_where=sa.column("team_id").is_(None),
    )
    op.create_index(
        "notes_team_created_id_idx",
        "notes",
        ["team_id", sa.column("created_at").desc(), sa.column("id").desc()],
        postgresql_where=sa.column("team_id").is_not(None),
    )
    for name in ("title", "content"):
        op.create_index(
            f"notes_{name}_trgm_idx",
            "notes",
            [name],
            postgresql_using="gin",
            postgresql_ops={name: f"{quoted_schema}.gin_trgm_ops"},
        )


def downgrade() -> None:
    for name in ("content", "title"):
        op.drop_index(f"notes_{name}_trgm_idx", table_name="notes")
    op.drop_index("notes_team_created_id_idx", table_name="notes")
    op.drop_index("notes_owner_created_id_idx", table_name="notes")
    op.drop_index("notes_created_id_idx", table_name="notes")
    # Keep the shared extension/schema: other application schemas may use them.
