from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, UTC
from hashlib import sha256
from typing import Any
from secrets import token_urlsafe

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, func, select

from app.database import get_session
from app.models import AuthSession, Membership, Note, Team, User
from app.main import app
from test_database import PostgreSQLTestCase

MAX_BIGINT = 2**63 - 1


class NoteTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))
        self.users: dict[str, int] = {}
        self.headers: dict[str, dict[str, str]] = {}
        with get_session() as session:
            for name in ("alice", "bob", "carol"):
                user = User(email=f"{name}@example.com", display_name=name.title(), password_hash="test-password-hash")
                session.add(user)
                session.flush()
                token = token_urlsafe(32)
                session.add(AuthSession(token_hash=sha256(token.encode()).hexdigest(), user_id=user.id,
                                        expires_at=datetime.now(UTC) + timedelta(hours=1)))
                self.users[name] = user.id
                self.headers[name] = {"Authorization": f"Bearer {token}"}
            first = Team(name="First team")
            second = Team(name="Second team")
            session.add_all([first, second])
            session.flush()
            self.team1, self.team2 = first.id, second.id
            for team_id, name, role in (
                (self.team1, "alice", "owner"), (self.team1, "bob", "member"),
                (self.team2, "alice", "member"), (self.team2, "carol", "owner"),
            ):
                session.add(Membership(team_id=team_id, user_id=self.users[name], role=role))

    def assert_no_store(self, response: Response) -> None:
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def create_note(self, user: str = "alice", **fields: Any) -> Response:
        response = self.client.post(
            "/notes", json={"title": "Note", **fields}, headers=self.headers[user]
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assert_no_store(response)
        return response

    def list_notes(self, user: str = "alice", **params: int) -> list[dict[str, Any]]:
        response = self.client.get("/notes", params=params, headers=self.headers[user])
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_store(response)
        self.assertIsInstance(response.json(), list)
        return response.json()

    def test_create_private_note_returns_public_fields_and_preserves_content(self) -> None:
        content = "  # Meeting notes\n\nKeep whitespace and Unicode: café.  "
        response = self.create_note(title="  Meeting notes  ", content=content)
        note = response.json()
        self.assertEqual(set(note), {"id", "owner_id", "team_id", "title", "content", "created_at", "updated_at"})
        self.assertEqual(note["owner_id"], self.users["alice"])
        self.assertIsNone(note["team_id"])
        self.assertEqual(note["title"], "Meeting notes")
        self.assertEqual(note["content"], content)
        self.assertEqual(note["created_at"], note["updated_at"])
        self.assertEqual(response.headers.get("Location"), f"/notes/{note['id']}")
        fetched = self.client.get(f"/notes/{note['id']}", headers=self.headers["alice"])
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), note)
        self.assert_no_store(fetched)
        self.assertEqual(self.create_note(title="Empty draft").json()["content"], "")

    def test_team_owners_and_members_can_create_and_read_team_notes(self) -> None:
        notes = [self.create_note(user, team_id=self.team1).json() for user in ("alice", "bob")]
        self.assertEqual([note["owner_id"] for note in notes], [self.users["alice"], self.users["bob"]])
        self.assertTrue(all(note["team_id"] == self.team1 for note in notes))
        for user in ("alice", "bob"):
            for note in notes:
                response = self.client.get(f"/notes/{note['id']}", headers=self.headers[user])
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), note)
                self.assert_no_store(response)
        member_note = self.create_note("alice", team_id=self.team2).json()
        response = self.client.get(f"/notes/{member_note['id']}", headers=self.headers["carol"])
        self.assertEqual(response.status_code, 200, response.text)

    def test_private_notes_are_visible_only_to_their_owner(self) -> None:
        note = self.create_note("alice", title="Private idea").json()
        for other in ("bob", "carol"):
            response = self.client.get(f"/notes/{note['id']}", headers=self.headers[other])
            self.assertEqual(response.status_code, 404, response.text)
            self.assert_no_store(response)
            self.assertEqual(self.list_notes(other), [])

    def test_list_filters_private_notes_and_other_teams(self) -> None:
        private = {user: self.create_note(user).json()["id"] for user in self.users}
        team1 = self.create_note("bob", team_id=self.team1).json()["id"]
        team2 = self.create_note("carol", team_id=self.team2).json()["id"]
        expected = {"alice": {private["alice"], team1, team2},
                    "bob": {private["bob"], team1}, "carol": {private["carol"], team2}}
        for user, ids in expected.items():
            with self.subTest(user=user):
                self.assertEqual({note["id"] for note in self.list_notes(user)}, ids)

    def test_removed_creator_loses_team_access_but_keeps_private_notes(self) -> None:
        team_note = self.create_note(team_id=self.team1).json()
        private = self.create_note(title="Personal note").json()
        with get_session() as session:
            session.execute(delete(Membership).where(Membership.team_id == self.team1, Membership.user_id == self.users["alice"]))
        denied = self.client.get(f"/notes/{team_note['id']}", headers=self.headers["alice"])
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assert_no_store(denied)
        self.assertEqual([note["id"] for note in self.list_notes()], [private["id"]])
        self.assertEqual(self.client.get(
            f"/notes/{team_note['id']}", headers=self.headers["bob"]
        ).status_code, 200)

    def test_unknown_or_inaccessible_team_creation_has_no_side_effects(self) -> None:
        responses = [self.client.post(
            "/notes", json={"title": "Denied", "team_id": team}, headers=self.headers["bob"]
        ) for team in (self.team2, MAX_BIGINT)]
        for response in responses:
            self.assertEqual(response.status_code, 404, response.text)
            self.assert_no_store(response)
        self.assertEqual(responses[0].json(), responses[1].json())
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Note)), 0)

    def test_unknown_and_forbidden_notes_return_the_same_not_found_response(self) -> None:
        private = self.create_note("bob", title="Bob's secret").json()
        forbidden = self.client.get(f"/notes/{private['id']}", headers=self.headers["alice"])
        missing = self.client.get(f"/notes/{MAX_BIGINT}", headers=self.headers["alice"])
        for response in (forbidden, missing):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertNotIn("Bob's secret", response.text)
            self.assert_no_store(response)
        self.assertEqual(forbidden.json(), missing.json())

    def test_invalid_note_payloads_are_rejected(self) -> None:
        invalid = [
            {}, {"title": " \t\n "}, {"title": "x" * 201}, {"title": None},
            {"title": 123}, {"title": "Note", "content": None},
            {"title": "Note", "content": "x" * 100001},
            {"title": "Note", "owner_id": self.users["bob"]},
        ] + [{"title": "Note", "team_id": value}
             for value in (-1, 0, True, str(self.team1), 1.5, MAX_BIGINT + 1)]
        for payload in invalid:
            with self.subTest(keys=list(payload)):
                response = self.client.post("/notes", json=payload, headers=self.headers["alice"])
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_store(response)
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Note)), 0)

    def test_path_ids_and_pagination_parameters_are_bounded(self) -> None:
        paths = [f"/notes/{value}" for value in (0, -1, "invalid", MAX_BIGINT + 1)]
        queries = [{"limit": value} for value in (0, -1, 101, "invalid")] + [
            {"offset": value} for value in (-1, MAX_BIGINT + 1, "invalid")
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers["alice"])
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_store(response)
        for query in queries:
            with self.subTest(query=query):
                response = self.client.get("/notes", params=query, headers=self.headers["alice"])
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_store(response)

    def test_all_note_routes_require_authentication(self) -> None:
        for method, path, payload in (("POST", "/notes", {"title": "Unauthenticated"}),
                                      ("GET", "/notes", None), ("GET", "/notes/1", None)):
            for headers in ({}, {"Authorization": "Bearer invalid"}):
                with self.subTest(method=method, path=path, headers=headers):
                    response = self.client.request(method, path, json=payload, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
                    self.assert_no_store(response)

    def test_pagination_has_a_default_limit_and_deterministic_timestamp_ties(self) -> None:
        with get_session() as session:
            inserted = [Note(owner_id=self.users["alice"], title=f"Note {index}") for index in range(55)]
            session.add_all(inserted)
            session.flush()
            self.assertEqual(len({note.created_at for note in inserted}), 1)
            expected = sorted((note.id for note in inserted), reverse=True)
        self.assertEqual([note["id"] for note in self.list_notes()], expected[:50])
        self.assertEqual([note["id"] for note in self.list_notes(limit=100)], expected)
        self.assertEqual([note["id"] for note in self.list_notes(limit=2, offset=1)], expected[1:3])
        self.assertEqual(self.list_notes(offset=55), [])
        self.assertEqual(self.list_notes(offset=MAX_BIGINT), [])

    def test_maximum_title_and_content_lengths_are_accepted(self) -> None:
        response = self.create_note(title="x" * 200, content="x" * 100000, team_id=None)
        self.assertEqual(len(response.json()["title"]), 200)
        self.assertEqual(len(response.json()["content"]), 100000)

    def test_team_creation_waits_for_membership_removal_and_rechecks_access(self) -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with get_session() as session:
                session.execute(delete(Membership).where(
                    Membership.team_id == self.team1, Membership.user_id == self.users["alice"]
                ))
                pending = executor.submit(self.client.post, "/notes", json={"title": "Racing note", "team_id": self.team1},
                                          headers=self.headers["alice"])
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=0.5)
            response = pending.result(timeout=5)
        self.assertEqual(response.status_code, 404, response.text)
        self.assert_no_store(response)
        self.assertEqual(self.list_notes(), [])
