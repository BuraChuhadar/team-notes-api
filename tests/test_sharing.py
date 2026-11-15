"""Explicit read-only team grants and their authorization boundaries."""

from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import update

from app.database import get_session
from app.main import app
from app.models import AuthSession, Membership, NoteShare, Team, User
from app.security import hash_token
from test_database import PostgreSQLTestCase

MAX_BIGINT = 2**63 - 1


class SharingTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))
        self.users: dict[str, int] = {}
        self.headers: dict[str, dict[str, str]] = {}
        self.teams: dict[str, int] = {}
        with get_session() as session:
            for name in ("alice", "bob", "carol", "dave", "erin"):
                user = User(
                    email=f"{name}@example.com",
                    display_name=name.title(),
                    password_hash="unused-in-sharing-tests",
                )
                session.add(user)
                session.flush()
                token = token_urlsafe(32)
                session.add(AuthSession(
                    token_hash=hash_token(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ))
                self.users[name] = user.id
                self.headers[name] = {"Authorization": f"Bearer {token}"}
            for name in ("source", "recipient", "second_recipient", "foreign"):
                team = Team(name=name)
                session.add(team)
                session.flush()
                self.teams[name] = team.id
            members = (
                ("source", "alice", "owner"),
                ("source", "bob", "member"),
                ("recipient", "carol", "owner"),
                ("recipient", "alice", "member"),
                ("recipient", "bob", "member"),
                ("recipient", "dave", "member"),
                ("second_recipient", "carol", "owner"),
                ("second_recipient", "alice", "member"),
                ("second_recipient", "bob", "member"),
                ("second_recipient", "dave", "member"),
                ("foreign", "erin", "owner"),
            )
            session.add_all([
                Membership(team_id=self.teams[team], user_id=self.users[user], role=role)
                for team, user, role in members
            ])

    def assert_status(self, response: Response, expected: int) -> Response:
        self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        return response

    def create_note(self, user: str = "alice", team_id: int | None = None) -> dict[str, Any]:
        response = self.client.post(
            "/notes",
            json={"title": "Original", "content": "Keep this content", "team_id": team_id},
            headers=self.headers[user],
        )
        return self.assert_status(response, 201).json()

    def create_share(self, note_id: int, team_id: int, user: str = "alice") -> Response:
        response = self.client.post(
            f"/notes/{note_id}/shares",
            json={"team_id": team_id},
            headers=self.headers[user],
        )
        return self.assert_status(response, 201)

    def read_note(self, note_id: int, user: str = "alice") -> dict[str, Any]:
        response = self.client.get(f"/notes/{note_id}", headers=self.headers[user])
        return self.assert_status(response, 200).json()

    def list_note_ids(self, user: str, **params: int) -> list[int]:
        response = self.client.get("/notes", params=params, headers=self.headers[user])
        return [note["id"] for note in self.assert_status(response, 200).json()]

    def assert_recipient_cannot_write(self, note: dict[str, Any], user: str) -> None:
        for version in (note["version"], note["version"] + 1):
            with self.subTest(user=user, version=version):
                self.assert_status(self.client.put(
                    f"/notes/{note['id']}",
                    json={"title": "Unauthorized", "content": "Must not persist", "version": version},
                    headers=self.headers[user],
                ), 404)
                self.assert_status(self.client.delete(
                    f"/notes/{note['id']}", params={"version": version}, headers=self.headers[user]
                ), 404)

    def test_private_owner_shares_and_location_resolves_to_managed_grant(self) -> None:
        note = self.create_note()
        recipient = self.teams["recipient"]
        self.assert_status(self.client.get(
            f"/notes/{note['id']}", headers=self.headers["dave"]
        ), 404)
        created = self.create_share(note["id"], recipient)
        grant = created.json()
        self.assertEqual(set(grant), {"note_id", "team_id", "created_at"})
        self.assertEqual((grant["note_id"], grant["team_id"]), (note["id"], recipient))
        self.assertIsNotNone(datetime.fromisoformat(grant["created_at"]).tzinfo)
        self.assertEqual(created.headers.get("Location"), f"/notes/{note['id']}/shares/{recipient}")
        fetched = self.client.get(created.headers["Location"], headers=self.headers["alice"])
        self.assertEqual(self.assert_status(fetched, 200).json(), grant)
        listed = self.client.get(f"/notes/{note['id']}/shares", headers=self.headers["alice"])
        self.assertEqual(self.assert_status(listed, 200).json(), [grant])
        self.assertEqual(self.read_note(note["id"], "dave"), note)
        self.assertEqual(self.list_note_ids("dave"), [note["id"]])
        self.assertEqual(self.read_note(note["id"]), note)

    def test_only_current_source_owner_manages_team_note_shares(self) -> None:
        note = self.create_note("bob", self.teams["source"])
        recipient = self.teams["recipient"]
        self.create_share(note["id"], recipient)
        for user in ("bob", "carol", "dave", "erin"):
            with self.subTest(user=user):
                self.assert_status(self.client.get(
                    f"/notes/{note['id']}/shares", headers=self.headers[user]
                ), 404)
                self.assert_status(self.client.get(
                    f"/notes/{note['id']}/shares/{recipient}", headers=self.headers[user]
                ), 404)
                self.assert_status(self.client.post(
                    f"/notes/{note['id']}/shares",
                    json={"team_id": self.teams["second_recipient"]}, headers=self.headers[user]
                ), 404)
                self.assert_status(self.client.delete(
                    f"/notes/{note['id']}/shares/{recipient}", headers=self.headers[user]
                ), 404)
        with get_session() as session:
            session.execute(update(Membership).where(
                Membership.team_id == self.teams["source"], Membership.user_id == self.users["alice"]
            ).values(role="member"))
        for path in (f"/notes/{note['id']}/shares", f"/notes/{note['id']}/shares/{recipient}"):
            self.assert_status(self.client.get(path, headers=self.headers["alice"]), 404)
        self.assertEqual(self.read_note(note["id"]), note)

    def test_unknown_and_nonmember_destination_teams_are_hidden_without_grants(self) -> None:
        note = self.create_note()
        with get_session() as session:
            missing = Team(name="Deleted destination")
            session.add(missing)
            session.flush()
            missing_id = missing.id
            session.delete(missing)
        responses = []
        for team_id in (self.teams["foreign"], missing_id):
            response = self.client.post(
                f"/notes/{note['id']}/shares", json={"team_id": team_id}, headers=self.headers["alice"]
            )
            responses.append(self.assert_status(response, 404).json())
        self.assertEqual(responses[0], responses[1])
        listing = self.client.get(f"/notes/{note['id']}/shares", headers=self.headers["alice"])
        self.assertEqual(self.assert_status(listing, 200).json(), [])
        self.assertEqual(self.read_note(note["id"]), note)

    def test_duplicate_and_original_source_team_grants_conflict_without_note_changes(self) -> None:
        private = self.create_note()
        self.create_share(private["id"], self.teams["recipient"])
        duplicate = self.client.post(
            f"/notes/{private['id']}/shares", json={"team_id": self.teams["recipient"]},
            headers=self.headers["alice"],
        )
        self.assert_status(duplicate, 409)
        team_note = self.create_note("bob", self.teams["source"])
        source = self.client.post(
            f"/notes/{team_note['id']}/shares", json={"team_id": self.teams["source"]},
            headers=self.headers["alice"],
        )
        self.assert_status(source, 409)
        for note in (private, team_note):
            self.assertEqual(self.read_note(note["id"]), note)

    def test_recipient_grants_never_allow_current_or_stale_note_writes(self) -> None:
        for team_id in (None, self.teams["source"]):
            original = self.create_note(team_id=team_id)
            saved = self.client.put(
                f"/notes/{original['id']}",
                json={"title": "Saved", "content": "Authoritative content", "version": original["version"]},
                headers=self.headers["alice"],
            )
            note = self.assert_status(saved, 200).json()
            self.create_share(note["id"], self.teams["recipient"])
            self.assertEqual(self.read_note(note["id"], "dave"), note)
            for version in (note["version"], original["version"]):
                for method in ("PUT", "DELETE"):
                    with self.subTest(team_id=team_id, version=version, method=method):
                        if method == "PUT":
                            response = self.client.put(
                                f"/notes/{note['id']}",
                                json={"title": "Forbidden", "content": "Forbidden", "version": version},
                                headers=self.headers["dave"],
                            )
                        else:
                            response = self.client.delete(
                                f"/notes/{note['id']}", params={"version": version}, headers=self.headers["dave"]
                            )
                        self.assert_status(response, 404)
            self.assertEqual(self.read_note(note["id"]), note)

    def test_revoking_grant_immediately_removes_read_access_without_bumping_version(self) -> None:
        note = self.create_note()
        recipient = self.teams["recipient"]
        self.create_share(note["id"], recipient)
        self.assertEqual(self.read_note(note["id"], "dave"), note)
        path = f"/notes/{note['id']}/shares/{recipient}"
        removed = self.client.delete(path, headers=self.headers["alice"])
        self.assert_status(removed, 204)
        self.assertEqual(removed.content, b"")
        self.assert_status(self.client.get(path, headers=self.headers["alice"]), 404)
        self.assert_status(self.client.delete(path, headers=self.headers["alice"]), 404)
        self.assert_status(self.client.get(f"/notes/{note['id']}", headers=self.headers["dave"]), 404)
        self.assertEqual(self.list_note_ids("dave"), [])
        self.assertEqual(self.read_note(note["id"]), note)

    def test_recipient_membership_removal_revokes_access_while_preserving_grant(self) -> None:
        note = self.create_note()
        recipient = self.teams["recipient"]
        grant = self.create_share(note["id"], recipient).json()
        removed = self.client.delete(
            f"/teams/{recipient}/members/{self.users['dave']}", headers=self.headers["carol"]
        )
        self.assert_status(removed, 204)
        self.assert_status(self.client.get(f"/notes/{note['id']}", headers=self.headers["dave"]), 404)
        self.assertEqual(self.list_note_ids("dave"), [])
        persisted = self.client.get(f"/notes/{note['id']}/shares/{recipient}", headers=self.headers["alice"])
        self.assertEqual(self.assert_status(persisted, 200).json(), grant)
        restored = self.client.post(
            f"/teams/{recipient}/members", json={"user_id": self.users["dave"]}, headers=self.headers["carol"]
        )
        self.assert_status(restored, 201)
        self.assertEqual(self.read_note(note["id"], "dave"), note)

    def test_removed_source_author_retains_only_recipient_read_access(self) -> None:
        note = self.create_note("bob", self.teams["source"])
        self.create_share(note["id"], self.teams["recipient"])
        removed = self.client.delete(
            f"/teams/{self.teams['source']}/members/{self.users['bob']}", headers=self.headers["alice"]
        )
        self.assert_status(removed, 204)
        self.assertEqual(note["owner_id"], self.users["bob"])
        self.assertEqual(self.read_note(note["id"], "bob"), note)
        self.assertEqual(self.list_note_ids("bob"), [note["id"]])
        self.assert_recipient_cannot_write(note, "bob")
        self.assert_status(self.client.get(
            f"/notes/{note['id']}/shares", headers=self.headers["bob"]
        ), 404)
        self.assertEqual(self.read_note(note["id"]), note)

    def test_multiple_recipient_memberships_do_not_duplicate_or_distort_note_pages(self) -> None:
        first = self.create_note()
        for name in ("recipient", "second_recipient"):
            self.create_share(first["id"], self.teams[name])
        hidden = self.create_note()
        second = self.create_note()
        self.create_share(second["id"], self.teams["recipient"])
        self.assertNotIn(hidden["id"], self.list_note_ids("dave"))
        self.assertEqual(self.list_note_ids("dave"), [second["id"], first["id"]])
        self.assertEqual(self.list_note_ids("dave", limit=1, offset=1), [first["id"]])
        self.assertEqual(self.list_note_ids("dave", limit=1, offset=2), [])

    def test_sender_can_revoke_after_leaving_recipient_team(self) -> None:
        note = self.create_note()
        recipient = self.teams["recipient"]
        grant = self.create_share(note["id"], recipient).json()
        removed = self.client.delete(
            f"/teams/{recipient}/members/{self.users['alice']}", headers=self.headers["carol"]
        )
        self.assert_status(removed, 204)
        listed = self.client.get(f"/notes/{note['id']}/shares", headers=self.headers["alice"])
        self.assertEqual(self.assert_status(listed, 200).json(), [grant])
        fetched = self.client.get(f"/notes/{note['id']}/shares/{recipient}", headers=self.headers["alice"])
        self.assertEqual(self.assert_status(fetched, 200).json(), grant)
        self.assert_status(self.client.delete(
            f"/notes/{note['id']}/shares/{recipient}", headers=self.headers["alice"]
        ), 204)
        self.assertEqual(self.read_note(note["id"]), note)
        self.assert_status(self.client.get(f"/notes/{note['id']}", headers=self.headers["dave"]), 404)

    def test_sharing_routes_require_authentication_and_reject_invalid_payloads(self) -> None:
        note = self.create_note()
        collection = f"/notes/{note['id']}/shares"
        item = f"{collection}/{self.teams['recipient']}"
        for headers in ({}, {"Authorization": "Bearer invalid-token"}):
            for method, path, payload in (
                ("POST", collection, {"team_id": self.teams["recipient"]}),
                ("GET", collection, None), ("GET", item, None), ("DELETE", item, None),
            ):
                with self.subTest(method=method, headers=headers):
                    response = self.client.request(method, path, json=payload, headers=headers)
                    self.assert_status(response, 401)
                    self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        invalid: list[dict[str, Any]] = [
            {}, {"team_id": None}, {"team_id": 0}, {"team_id": -1},
            {"team_id": True}, {"team_id": 1.5}, {"team_id": str(self.teams["recipient"])},
            {"team_id": MAX_BIGINT + 1}, {"team_id": self.teams["recipient"], "role": "owner"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_status(self.client.post(collection, json=payload, headers=self.headers["alice"]), 422)
        listing = self.client.get(collection, headers=self.headers["alice"])
        self.assertEqual(self.assert_status(listing, 200).json(), [])

    def test_manager_share_pagination_defaults_bounds_and_path_validation(self) -> None:
        note = self.create_note()
        expected: set[int] = set()
        with get_session() as session:
            for index in range(55):
                team = Team(name=f"Destination {index}")
                session.add(team)
                session.flush()
                session.add_all([
                    Membership(team_id=team.id, user_id=self.users["alice"], role="owner"),
                    NoteShare(note_id=note["id"], team_id=team.id),
                ])
                expected.add(team.id)
        collection = f"/notes/{note['id']}/shares"
        all_response = self.client.get(collection, params={"limit": 100}, headers=self.headers["alice"])
        all_grants = self.assert_status(all_response, 200).json()
        self.assertEqual(len(all_grants), 55)
        self.assertEqual({grant["team_id"] for grant in all_grants}, expected)
        for params, page in (
            ({}, all_grants[:50]),
            ({"limit": 2, "offset": 1}, all_grants[1:3]),
            ({"offset": 55}, []),
            ({"offset": MAX_BIGINT}, []),
        ):
            with self.subTest(params=params):
                response = self.client.get(collection, params=params, headers=self.headers["alice"])
                self.assertEqual(self.assert_status(response, 200).json(), page)
        invalid_queries: list[dict[str, int | str]] = [
            {"limit": value} for value in (0, -1, 101, "invalid")
        ] + [{"offset": value} for value in (-1, MAX_BIGINT + 1, "invalid")]
        for params in invalid_queries:
            with self.subTest(params=params):
                self.assert_status(self.client.get(collection, params=params, headers=self.headers["alice"]), 422)
        for bad_id in (0, -1, "invalid", MAX_BIGINT + 1):
            for path in (f"/notes/{bad_id}/shares", f"{collection}/{bad_id}"):
                with self.subTest(path=path):
                    self.assert_status(self.client.get(path, headers=self.headers["alice"]), 422)
