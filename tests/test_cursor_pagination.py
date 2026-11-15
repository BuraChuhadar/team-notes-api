"""Cursor continuity, current authorization, and shared search-index infrastructure."""

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import get_engine, get_session, init_database
from app.main import app
from app.models import AuthSession, Membership, Note, NoteShare, Team, User
from app.security import hash_token
from test_database import PostgreSQLTestCase


class CursorPaginationTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))
        self.headers: dict[str, dict[str, str]] = {}
        self.users: dict[str, int] = {}
        self.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        with get_session() as session:
            for name in ("alice", "bob"):
                user = User(
                    email=f"cursor-{name}@example.com",
                    display_name=name,
                    password_hash="unused-test-hash",
                )
                session.add(user)
                session.flush()
                token = token_urlsafe(32)
                session.add(
                    AuthSession(
                        token_hash=hash_token(token),
                        user_id=user.id,
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
                self.users[name] = user.id
                self.headers[name] = {"Authorization": "Bearer " + token}
            team = Team(name="Cursor recipient team")
            session.add(team)
            session.flush()
            self.team_id = team.id
            session.add(
                Membership(team_id=team.id, user_id=self.users["bob"], role="owner")
            )

    def seed(
        self,
        title: str,
        *,
        owner: str = "alice",
        content: str = "",
        timestamp: datetime | None = None,
        share: bool = False,
    ) -> int:
        with get_session() as session:
            note = Note(
                owner_id=self.users[owner],
                title=title,
                content=content,
                created_at=timestamp or self.created_at,
                updated_at=timestamp or self.created_at,
            )
            session.add(note)
            session.flush()
            if share:
                session.add(NoteShare(note_id=note.id, team_id=self.team_id))
            return note.id

    def page(self, user: str = "alice", **params: str | int) -> Response:
        response = self.client.get("/notes", params=params, headers=self.headers[user])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        return response

    def next_page(self, previous: Response, user: str = "alice") -> Response:
        link = previous.headers["Link"]
        self.assertTrue(link.endswith('; rel="next"'))
        reference = link.split(">", 1)[0][1:]
        response = self.client.get(
            urljoin(str(previous.request.url), reference), headers=self.headers[user]
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_cursor_pages_preserve_timestamp_ties_without_duplicates_after_insert(
        self,
    ) -> None:
        original = [self.seed(f"Original {number}") for number in range(7)]
        first = self.page(limit=2)
        self.assertEqual([note["id"] for note in first.json()], original[::-1][:2])
        self.assertIn("X-Next-Cursor", first.headers)
        self.seed("Inserted at the head", timestamp=self.created_at + timedelta(days=1))
        seen = [note["id"] for note in first.json()]
        current = first
        while "X-Next-Cursor" in current.headers:
            current = self.next_page(current)
            seen.extend(note["id"] for note in current.json())
        self.assertEqual(seen, original[::-1])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertNotIn("Link", current.headers)

    def test_continuation_rechecks_membership_and_never_reveals_private_notes(
        self,
    ) -> None:
        own = self.seed(
            "Own note", owner="bob", timestamp=self.created_at - timedelta(days=1)
        )
        self.seed("Never shared")
        shared = [self.seed(f"Shared {number}", share=True) for number in range(5)]
        first = self.page("bob", limit=2)
        self.assertEqual([note["id"] for note in first.json()], shared[::-1][:2])
        with get_session() as session:
            session.execute(
                delete(Membership).where(
                    Membership.team_id == self.team_id,
                    Membership.user_id == self.users["bob"],
                )
            )
        following = self.next_page(first, "bob")
        self.assertEqual([note["id"] for note in following.json()], [own])
        self.assertNotIn("X-Next-Cursor", following.headers)

    def test_link_preserves_normalized_search_and_cursor_cannot_change_caller_or_query(
        self,
    ) -> None:
        self.seed("Does not match")
        expected = [self.seed(f"Needle {number}") for number in range(4)]
        first = self.page(limit=2, q="  needle  ")
        link_url = first.headers["Link"].split(">", 1)[0][1:]
        parameters = parse_qs(urlsplit(link_url).query)
        self.assertEqual(parameters["q"], ["needle"])
        self.assertEqual(parameters["limit"], ["2"])
        self.assertEqual(parameters["cursor"], [first.headers["X-Next-Cursor"]])
        second = self.next_page(first)
        self.assertEqual(
            [note["id"] for note in first.json() + second.json()], expected[::-1]
        )
        for user, query in (("bob", "needle"), ("alice", "different")):
            with self.subTest(user=user, query=query):
                response = self.client.get(
                    "/notes",
                    params={"cursor": first.headers["X-Next-Cursor"], "q": query},
                    headers=self.headers[user],
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    set(response.json()["detail"][0]), {"type", "loc", "msg"}
                )
                self.assertEqual(
                    response.json()["detail"][0]["loc"], ["query", "cursor"]
                )

    def test_malformed_cursors_and_offset_combination_are_sanitized_validation_errors(
        self,
    ) -> None:
        for number in range(3):
            self.seed(f"Cursor {number}")
        first = self.page(limit=1)
        malformed_json = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {"created_at": "not-a-date", "note_id": -1, "query_hash": "bad"}
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        for params in (
            {"cursor": ""},
            {"cursor": "invalid!"},
            {"cursor": "x" * 1025},
            {"cursor": "bm90LWpzb24"},
            {"cursor": malformed_json},
            {"cursor": first.headers["X-Next-Cursor"], "offset": 1},
        ):
            with self.subTest(params_keys=list(params)):
                response = self.client.get(
                    "/notes", params=params, headers=self.headers["alice"]
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                for issue in response.json()["detail"]:
                    self.assertEqual(set(issue), {"type", "loc", "msg"})
        next_page = self.page(cursor=first.headers["X-Next-Cursor"], offset=0, limit=1)
        self.assertEqual(len(next_page.json()), 1)

    def test_final_and_empty_pages_omit_continuation_headers(self) -> None:
        self.assertEqual(self.page(limit=2).json(), [])
        self.seed("Only one")
        result = self.page(limit=2)
        self.assertEqual(len(result.json()), 1)
        self.assertNotIn("X-Next-Cursor", result.headers)
        self.assertNotIn("Link", result.headers)

    def test_search_indexes_survive_removal_of_another_isolated_application_schema(
        self,
    ) -> None:
        note_id = self.seed("Shared extension infrastructure")
        other_schema = "test_index_isolation_" + uuid4().hex
        with self.admin_engine.begin() as connection:
            connection.execute(CreateSchema(other_schema))
        try:
            other_url = self.test_url.update_query_dict(
                {"options": f"-csearch_path={other_schema}"}
            )
            with patch.dict(
                os.environ,
                {"DATABASE_URL": other_url.render_as_string(hide_password=False)},
            ):
                init_database()
                get_engine().dispose()
        finally:
            with self.admin_engine.begin() as connection:
                connection.execute(DropSchema(other_schema, cascade=True))
        found = self.page(q="extension infrastructure")
        self.assertEqual([note["id"] for note in found.json()], [note_id])

    def test_openapi_describes_cursor_and_continuation_headers(self) -> None:
        operation = self.client.get("/openapi.json").json()["paths"]["/notes"]["get"]
        cursor = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "cursor"
        )
        self.assertFalse(cursor["required"])
        self.assertIn("same account", cursor["description"])
        for name in ("X-Next-Cursor", "Link"):
            self.assertIn(name, operation["responses"]["200"]["headers"])
