"""Literal note search, current permissions, and deterministic pagination."""

from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, func, select

from app.database import get_session
from app.main import app
from app.models import AuthSession, Membership, Note, NoteShare, Team, User
from app.security import hash_token
from test_database import PostgreSQLTestCase

MAX_BIGINT = 2**63 - 1


class SearchTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))
        self.users: dict[str, int] = {}
        self.headers: dict[str, dict[str, str]] = {}
        with get_session() as session:
            for name in ("alice", "bob", "carol", "dave"):
                user = User(
                    email=f"{name}@example.com",
                    display_name=name.title(),
                    password_hash="unused-in-search-tests",
                )
                session.add(user)
                session.flush()
                token = token_urlsafe(32)
                session.add(AuthSession(
                    token_hash=hash_token(token), user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ))
                self.users[name] = user.id
                self.headers[name] = {"Authorization": f"Bearer {token}"}
            source = Team(name="Source")
            recipient1 = Team(name="Recipient one")
            recipient2 = Team(name="Recipient two")
            hidden = Team(name="Hidden")
            session.add_all([source, recipient1, recipient2, hidden])
            session.flush()
            self.source = source.id
            self.recipient1 = recipient1.id
            self.recipient2 = recipient2.id
            self.hidden = hidden.id
            for team_id, name, role in (
                (self.source, "alice", "owner"), (self.source, "bob", "member"),
                (self.recipient1, "carol", "owner"), (self.recipient1, "bob", "member"),
                (self.recipient2, "carol", "owner"), (self.recipient2, "bob", "member"),
                (self.hidden, "dave", "owner"),
            ):
                session.add(Membership(team_id=team_id, user_id=self.users[name], role=role))

    def assert_no_store(self, response: Response) -> None:
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def seed_note(
        self, title: str, *, content: str = "", owner: str = "carol", team_id: int | None = None
    ) -> int:
        with get_session() as session:
            note = Note(owner_id=self.users[owner], team_id=team_id, title=title, content=content)
            session.add(note)
            session.flush()
            return note.id

    def share(self, note_id: int, *team_ids: int) -> None:
        with get_session() as session:
            session.add_all([NoteShare(note_id=note_id, team_id=team_id) for team_id in team_ids])

    def search(self, q: str | None = None, user: str = "carol", **pagination: int) -> Response:
        params: dict[str, str | int] = {**pagination}
        if q is not None:
            params["q"] = q
        return self.client.get("/notes", params=params, headers=self.headers[user])

    def results(self, q: str | None = None, user: str = "carol", **pagination: int) -> list[dict[str, Any]]:
        response = self.search(q, user, **pagination)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_store(response)
        self.assertIsInstance(response.json(), list)
        return response.json()

    def test_case_insensitive_title_or_content_substrings_trim_the_query(self) -> None:
        title_match = self.seed_note("Planning ALPHA launch")
        content_match = self.seed_note("Daily notes", content="Prefix alpha suffix")
        self.seed_note("Unrelated", content="Other text")
        for query in ("alpha", "AlPhA", "  aLpHa \t"):
            with self.subTest(query=query):
                self.assertEqual(
                    {note["id"] for note in self.results(query)}, {title_match, content_match}
                )
        self.assertEqual(self.results("No matching substring"), [])

    def test_sql_wildcards_and_the_escape_character_are_literal_text(self) -> None:
        percent = self.seed_note("Budget 100% today")
        self.seed_note("Budget 1000 today")
        underscore = self.seed_note("project_notes")
        self.seed_note("projectXnotes")
        slash = self.seed_note("Folder", content="root/folder")
        self.seed_note("Decoy", content="rootXfolder")
        for query, expected in (
            ("%", percent), ("100%", percent), ("_", underscore),
            ("project_notes", underscore), ("/", slash), ("root/folder", slash),
        ):
            with self.subTest(query=query):
                self.assertEqual([note["id"] for note in self.results(query)], [expected])
        combined = self.seed_note("Literal 100%_/ marker")
        self.assertEqual([note["id"] for note in self.results("100%_/")], [combined])

    def test_sql_looking_queries_are_data_and_do_not_modify_storage(self) -> None:
        self.seed_note("Ordinary visible note")
        self.seed_note("Hidden secret", owner="alice", content="Confidential data")
        with get_session() as session:
            before = session.scalar(select(func.count()).select_from(Note))
        for query in ("' OR 1=1 --", "'; DROP TABLE notes; --"):
            with self.subTest(query=query):
                self.assertEqual(self.results(query), [])
        literal = self.seed_note("Literal ' OR 1=1 -- text")
        self.assertEqual([note["id"] for note in self.results("' OR 1=1 --")], [literal])
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Note)), (before or 0) + 1)

    def test_search_applies_private_team_and_shared_permissions_without_leaks(self) -> None:
        personal = self.seed_note("Match personal")
        shared = self.seed_note("Match shared", owner="alice", team_id=self.source)
        self.share(shared, self.recipient1)
        self.seed_note("Match source-only secret", owner="bob", team_id=self.source)
        hidden_team_note = self.seed_note("Match hidden-team secret", owner="dave", team_id=self.hidden)
        self.seed_note("Match private secret", owner="alice", content="Sensitive match body")
        unrelated = self.seed_note("Different text", owner="alice", team_id=self.source)
        self.share(unrelated, self.recipient1)
        response = self.search("match")
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_store(response)
        self.assertEqual({note["id"] for note in response.json()}, {personal, shared})
        for forbidden in ("source-only secret", "hidden-team secret", "private secret", "Sensitive match body"):
            self.assertNotIn(forbidden, response.text)
        self.assertEqual(self.results("secret"), [])
        self.assertEqual([note["id"] for note in self.results("match", "dave")], [hidden_team_note])

    def test_multiple_grants_and_source_membership_do_not_duplicate_notes(self) -> None:
        note_id = self.seed_note("Match once", owner="alice", team_id=self.source)
        self.share(note_id, self.recipient1, self.recipient2)
        for user in ("bob", "carol"):
            with self.subTest(user=user):
                self.assertEqual([note["id"] for note in self.results("match", user)], [note_id])
                self.assertEqual(
                    [note["id"] for note in self.results("match", user, limit=1, offset=0)], [note_id]
                )
                self.assertEqual(self.results("match", user, limit=1, offset=1), [])

    def test_search_filters_visibility_and_matches_before_pagination(self) -> None:
        with get_session() as session:
            first = Note(owner_id=self.users["carol"], title="Match first")
            second = Note(owner_id=self.users["carol"], title="Match second")
            shared = Note(owner_id=self.users["alice"], team_id=self.source, title="Match shared")
            session.add_all([first, second, shared])
            session.flush()
            expected = [shared.id, second.id, first.id]
            self.assertEqual(len({note.created_at for note in (first, second, shared)}), 1)
            session.add(NoteShare(note_id=shared.id, team_id=self.recipient1))
            # Newer hidden/mismatched rows must not consume matching visible slots.
            session.add_all([
                Note(owner_id=self.users["alice"], title="Match private secret"),
                Note(owner_id=self.users["dave"], team_id=self.hidden, title="Match hidden team"),
                Note(owner_id=self.users["carol"], title="Does not contain the keyword"),
            ])
        self.assertEqual([note["id"] for note in self.results("match", limit=2)], expected[:2])
        self.assertEqual([note["id"] for note in self.results("match", limit=2, offset=1)], expected[1:])
        self.assertEqual(self.results("match", offset=3), [])

    def test_default_limit_and_timestamp_ties_remain_deterministic(self) -> None:
        with get_session() as session:
            notes = [Note(owner_id=self.users["carol"], title=f"Match {index}") for index in range(55)]
            session.add_all(notes)
            session.flush()
            self.assertEqual(len({note.created_at for note in notes}), 1)
            expected = sorted((note.id for note in notes), reverse=True)
        self.assertEqual([note["id"] for note in self.results("match")], expected[:50])
        self.assertEqual([note["id"] for note in self.results("match", limit=100)], expected)
        self.assertEqual([note["id"] for note in self.results("match", limit=2, offset=1)], expected[1:3])
        self.assertEqual(self.results("match", offset=55), [])
        self.assertEqual(self.results("match", offset=MAX_BIGINT), [])

    def test_revoked_grants_and_removed_memberships_remove_search_access(self) -> None:
        grant_note = self.seed_note("Match revoked grant", owner="alice", team_id=self.source)
        membership_note = self.seed_note("Match removed member", owner="alice", team_id=self.source)
        self.share(grant_note, self.recipient1, self.recipient2)
        self.share(membership_note, self.recipient1, self.recipient2)
        self.assertEqual(len(self.results("match")), 2)
        for recipient in (self.recipient1, self.recipient2):
            with get_session() as session:
                session.execute(delete(NoteShare).where(NoteShare.note_id == grant_note, NoteShare.team_id == recipient))
            expected = {grant_note, membership_note} if recipient == self.recipient1 else {membership_note}
            self.assertEqual({note["id"] for note in self.results("match")}, expected)
        for recipient in (self.recipient1, self.recipient2):
            with get_session() as session:
                session.execute(delete(Membership).where(
                    Membership.team_id == recipient, Membership.user_id == self.users["carol"]
                ))
            expected = {membership_note} if recipient == self.recipient1 else set()
            self.assertEqual({note["id"] for note in self.results("match")}, expected)
        self.assertEqual({note["id"] for note in self.results("match", "alice")}, {grant_note, membership_note})

    def test_query_omission_preserves_listing_and_search_does_not_change_notes(self) -> None:
        personal = self.seed_note("Match personal", content="Keep the original content")
        shared = self.seed_note("Other shared", owner="alice", team_id=self.source)
        self.share(shared, self.recipient1)
        self.seed_note("Hidden", owner="alice")
        unfiltered = self.results()
        self.assertEqual({note["id"] for note in unfiltered}, {personal, shared})
        self.assertEqual([note["id"] for note in self.results("match")], [personal])
        self.assertEqual(self.results(), unfiltered)
        fetched = self.client.get(f"/notes/{personal}", headers=self.headers["carol"])
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assert_no_store(fetched)
        self.assertEqual(fetched.json(), next(note for note in unfiltered if note["id"] == personal))

    def test_query_and_pagination_validation_are_bounded_and_not_cached(self) -> None:
        maximum = self.seed_note("x" * 200)
        for query in ("", " ", "\t\n ", "x" * 201):
            with self.subTest(query_length=len(query)):
                response = self.search(query)
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_store(response)
        for query in ("x" * 200, "  " + "x" * 200 + "  "):
            self.assertEqual([note["id"] for note in self.results(query)], [maximum])
        invalid: list[dict[str, str | int]] = [
            {"limit": value} for value in (0, -1, 101, "invalid")
        ] + [{"offset": value} for value in (-1, MAX_BIGINT + 1, "invalid")]
        for pagination in invalid:
            with self.subTest(pagination=pagination):
                response = self.client.get(
                    "/notes", params={"q": "x", **pagination}, headers=self.headers["carol"]
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_store(response)

    def test_search_requires_authentication_and_sets_the_bearer_challenge(self) -> None:
        self.seed_note("Match visible after authentication")
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            with self.subTest(headers=headers):
                response = self.client.get("/notes", params={"q": "match"}, headers=headers)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
                self.assert_no_store(response)

    def test_openapi_documents_optional_bounded_search_query(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        parameters = response.json()["paths"]["/notes"]["get"]["parameters"]
        query = next(parameter for parameter in parameters if parameter["name"] == "q")
        self.assertEqual(query["in"], "query")
        self.assertFalse(query.get("required", False))
        schema = query["schema"]
        string_schema = next(
            candidate for candidate in schema.get("anyOf", [schema]) if candidate.get("type") == "string"
        )
        self.assertEqual(string_schema["minLength"], 1)
        self.assertEqual(string_schema["maxLength"], 200)
