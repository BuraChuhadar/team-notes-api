"""Measure real PostgreSQL plans for the API's visibility, search, and page queries.

Select a disposable benchmark database through DATABASE_URL. This command reads
application data and optionally refreshes table statistics; it does not seed or
change application rows. Connection URLs and credentials are never printed.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Table, column, func, inspect, or_, select, table, tuple_
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ExecutableDDLElement
from sqlalchemy.sql import Executable, Select
from sqlalchemy.sql.compiler import DDLCompiler, SQLCompiler
from sqlalchemy.sql.elements import ClauseElement

from app.config import get_settings
from app.database import get_engine
from app.models import Base, Note, User
from app.notes import visible_to

EXPECTED_INDEXES = {
    "notes_created_id_idx",
    "notes_owner_created_id_idx",
    "notes_team_created_id_idx",
    "notes_title_trgm_idx",
    "notes_content_trgm_idx",
}


class Explain(Executable, ClauseElement):
    """Keep the measured SELECT and its bound values under SQLAlchemy compilation."""

    inherit_cache = False

    def __init__(self, statement: Select[Any]) -> None:
        self.statement = statement


@compiles(Explain, "postgresql")
def compile_explain(element: Explain, compiler: SQLCompiler, **kw: Any) -> str:
    # EXPLAIN is a PostgreSQL diagnostic command, not an application query.
    # The SELECT is compiled normally, preserving typed bound parameters.
    return "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + compiler.process(
        element.statement, **kw
    )


class AnalyzeTable(ExecutableDDLElement):
    """Quote the selected table name through the dialect's DDL compiler."""

    inherit_cache = False

    def __init__(self, selected_table: Table) -> None:
        self.selected_table = selected_table


@compiles(AnalyzeTable, "postgresql")
def compile_analyze(element: AnalyzeTable, compiler: DDLCompiler, **kw: Any) -> str:
    return "ANALYZE " + compiler.preparer.format_table(element.selected_table)


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        nodes.append(
            {
                key: node[key]
                for key in (
                    "Node Type",
                    "Relation Name",
                    "Index Name",
                    "Actual Rows",
                    "Actual Loops",
                )
                if key in node
            }
        )
        for child in node.get("Plans", []):
            visit(child)

    visit(plan["Plan"])
    return {
        "planning_ms": plan.get("Planning Time"),
        "execution_ms": plan.get("Execution Time"),
        "returned_rows": plan["Plan"].get("Actual Rows"),
        "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
        "shared_read_blocks": plan["Plan"].get("Shared Read Blocks", 0),
        "indexes_used": sorted(
            {node["Index Name"] for node in nodes if "Index Name" in node}
        ),
        "nodes": nodes,
        "full_plan": plan,
    }


def measured_plan(connection: Connection, statement: Select[Any]) -> dict[str, Any]:
    result = connection.execute(Explain(statement)).scalar_one()
    if (
        not isinstance(result, list)
        or len(result) != 1
        or not isinstance(result[0], dict)
    ):
        raise RuntimeError("PostgreSQL returned an unexpected JSON query plan.")
    return summarize_plan(result[0])


def run_diagnostics(
    user_id: int, term: str, page_size: int, requested_offset: int, analyze: bool
) -> dict[str, Any]:
    engine = get_engine()
    try:
        with engine.begin() as connection:
            if connection.scalar(select(User.id).where(User.id == user_id)) is None:
                raise ValueError("The selected benchmark account does not exist.")
            indexes = inspect(connection).get_indexes("notes")
            missing = EXPECTED_INDEXES - {index["name"] for index in indexes}
            if missing:
                raise RuntimeError(
                    "Apply all migrations before measuring query plans; indexes are missing."
                )
            if analyze:
                for selected_table in Base.metadata.sorted_tables:
                    connection.execute(AnalyzeTable(selected_table))

            base = select(Note).where(visible_to(user_id))
            visible_count = connection.scalar(
                select(func.count()).select_from(Note).where(visible_to(user_id))
            )
            assert visible_count is not None
            effective_offset = min(requested_offset, max(visible_count - page_size, 0))
            ordered = base.order_by(Note.created_at.desc(), Note.id.desc())
            plans = {
                "first_page": measured_plan(connection, ordered.limit(page_size + 1)),
                "deep_offset": measured_plan(
                    connection, ordered.offset(effective_offset).limit(page_size + 1)
                ),
            }
            if effective_offset > 0:
                boundary = connection.execute(
                    select(Note.created_at, Note.id)
                    .where(visible_to(user_id))
                    .order_by(Note.created_at.desc(), Note.id.desc())
                    .offset(effective_offset - 1)
                    .limit(1)
                ).one()
                keyset = (
                    base.where(
                        tuple_(Note.created_at, Note.id)
                        < tuple_(boundary.created_at, boundary.id)
                    )
                    .order_by(Note.created_at.desc(), Note.id.desc())
                    .limit(page_size + 1)
                )
                plans["keyset_at_same_position"] = measured_plan(connection, keyset)

            for name, query in (
                ("substring_search", term),
                ("one_character_search", term[0]),
            ):
                statement = (
                    base.where(
                        or_(
                            Note.title.icontains(query, autoescape=True),
                            Note.content.icontains(query, autoescape=True),
                        )
                    )
                    .order_by(Note.created_at.desc(), Note.id.desc())
                    .limit(page_size + 1)
                )
                plans[name] = measured_plan(connection, statement)

            extensions = table(
                "pg_extension",
                column("extname"),
                column("extnamespace"),
                schema="pg_catalog",
            )
            namespaces = table(
                "pg_namespace", column("oid"), column("nspname"), schema="pg_catalog"
            )
            extension_schema = connection.scalar(
                select(namespaces.c.nspname)
                .select_from(
                    extensions.join(
                        namespaces, extensions.c.extnamespace == namespaces.c.oid
                    )
                )
                .where(extensions.c.extname == "pg_trgm")
            )
            return {
                "measured_at": datetime.now(UTC).isoformat(),
                "scope": "Local diagnostic observations for this dataset, account, database, and configuration; not a production capacity claim.",
                "user_id": user_id,
                "visible_notes": visible_count,
                "page_size": page_size,
                "requested_offset": requested_offset,
                "effective_offset": effective_offset,
                "search_term": term,
                "statistics_refreshed": analyze,
                "trigram_schema": extension_schema,
                "configured_trigram_schema": get_settings().db_trgm_schema,
                "indexes": indexes,
                "plans": plans,
                "interpretation": [
                    "The optimizer is allowed to choose a sequential scan; this tool does not disable it or force index selection.",
                    "The cursor boundary lookup is diagnostic setup and is excluded from keyset timing; API clients carry this boundary from the previous page.",
                    "One-character or otherwise unselective substring searches may not benefit from trigram indexes and can remain expensive.",
                    "Trigram and ordered indexes add storage and write-maintenance costs; compare these read plans with the mixed load-test results.",
                ],
            }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Existing account in the disposable benchmark database.",
    )
    parser.add_argument(
        "--term",
        default="rarekeyword",
        help="A selective literal search term of 3–200 characters.",
    )
    parser.add_argument("--limit", type=int, default=50, help="Page size, 1–100.")
    parser.add_argument(
        "--offset",
        type=int,
        default=5_000,
        help="Requested comparison position; capped to available visible rows.",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Refresh application-table statistics before measuring plans.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON report to this path instead of stdout.",
    )
    arguments = parser.parse_args()
    term = arguments.term.strip()
    if (
        arguments.user_id <= 0
        or not 1 <= arguments.limit <= 100
        or arguments.offset < 0
        or not 3 <= len(term) <= 200
    ):
        parser.error(
            "Use a positive user ID, limit 1–100, nonnegative offset, and a search term of 3–200 characters."
        )
    try:
        report = run_diagnostics(
            arguments.user_id,
            term,
            arguments.limit,
            arguments.offset,
            arguments.analyze,
        )
    except (SQLAlchemyError, RuntimeError, ValueError) as error:
        print(
            f"Query diagnostics failed ({type(error).__name__}). Check the selected database, account, migration state, and permissions.",
            file=sys.stderr,
        )
        return 1
    encoded = json.dumps(report, indent=2, default=str) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded)
        print(f"Query-plan report written to {arguments.output}")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
