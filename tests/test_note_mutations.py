"""Note mutation permissions, optimistic versions, and transaction races."""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from threading import Barrier
from typing import Any, Literal

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, select, update

from app.database import get_session
from app.main import app
from app.models import AuthSession, Membership, Note, Team, User
from app.security import hash_token
from test_database import PostgreSQLTestCase

MAX_BIGINT = 2**63 - 1


class NoteMutationTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))
        self.users: dict[str, int] = {}
        self.headers: dict[str, dict[str, str]] = {}
        with get_session() as session:
            for name in ("alice", "bob", "carol"):
                user = User(
                    email=f"{name}@example.com",
                    display_name=name.title(),
                    password_hash="unused-in-mutation-tests",
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
            team = Team(name="Engineering")
            session.add(team)
            session.flush()
            self.team_id = team.id
            session.add_all([
                Membership(team_id=team.id, user_id=self.users["alice"], role="owner"),
                Membership(team_id=team.id, user_id=self.users["bob"], role="member"),
            ])

    def assert_no_store(self, response: Response) -> None:
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def create_note(self, user: str = "alice", **fields: Any) -> dict[str, Any]:
        response = self.client.post(
            "/notes",
            json={"title": "Original", "content": "Original content", **fields},
            headers=self.headers[user],
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assert_no_store(response)
        self.assertEqual(response.json()["version"], 1)
        return response.json()

    def get_note(self, note_id: int, user: str = "alice") -> dict[str, Any]:
        response = self.client.get(f"/notes/{note_id}", headers=self.headers[user])
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_store(response)
        return response.json()

    def put_note(
        self,
        note_id: int,
        user: str = "alice",
        *,
        title: str = "Updated",
        content: str = "Updated content",
        version: int = 1,
    ) -> Response:
        return self.client.put(
            f"/notes/{note_id}",
            json={"title": title, "content": content, "version": version},
            headers=self.headers[user],
        )

    def delete_note(self, note_id: int, user: str = "alice", version: int = 1) -> Response:
        return self.client.delete(
            f"/notes/{note_id}", params={"version": version}, headers=self.headers[user]
        )

    def test_update_replaces_fields_increments_version_and_preserves_identity(self) -> None:
        original = self.create_note()
        untouched = self.create_note(title="Unrelated")
        content = "  # Revised\n\nKeep Unicode and whitespace: café.  "
        response = self.put_note(original["id"], title="  Revised title  ", content=content)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_store(response)
        saved = response.json()
        self.assertEqual(set(saved), set(original))
        for field in ("id", "owner_id", "team_id", "created_at"):
            self.assertEqual(saved[field], original[field])
        self.assertEqual(saved["title"], "Revised title")
        self.assertEqual(saved["content"], content)
        self.assertEqual(saved["version"], 2)
        self.assertGreater(
            datetime.fromisoformat(saved["updated_at"]),
            datetime.fromisoformat(original["updated_at"]),
        )
        self.assertEqual(self.get_note(original["id"]), saved)
        self.assertEqual(self.get_note(untouched["id"]), untouched)
        listed = self.client.get("/notes", headers=self.headers["alice"])
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assert_no_store(listed)
        self.assertEqual(
            next(note for note in listed.json() if note["id"] == saved["id"]), saved
        )

    def test_empty_content_and_maximum_lengths_are_valid_replacements(self) -> None:
        note_id = self.create_note()["id"]
        maximum = self.put_note(note_id, title="x" * 200, content="y" * 100000)
        self.assertEqual(maximum.status_code, 200, maximum.text)
        self.assert_no_store(maximum)
        self.assertEqual(len(maximum.json()["title"]), 200)
        self.assertEqual(len(maximum.json()["content"]), 100000)
        empty = self.put_note(note_id, title="Empty draft", content="", version=2)
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assert_no_store(empty)
        self.assertEqual(empty.json()["content"], "")
        self.assertEqual(empty.json()["version"], 3)
        self.assertEqual(self.get_note(note_id), empty.json())

    def test_stale_update_changes_neither_content_nor_version_nor_timestamps(self) -> None:
        note_id = self.create_note()["id"]
        winner = self.put_note(note_id, title="First writer")
        self.assertEqual(winner.status_code, 200, winner.text)
        stale = self.put_note(note_id, title="Lost update", content="Must not be saved")
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assert_no_store(stale)
        self.assertEqual(self.get_note(note_id), winner.json())
        retry = self.put_note(note_id, title="Reloaded and retried", version=2)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["version"], 3)

    def test_stale_delete_preserves_note_then_current_version_deletes_it(self) -> None:
        note_id = self.create_note()["id"]
        saved = self.put_note(note_id)
        self.assertEqual(saved.status_code, 200, saved.text)
        stale = self.delete_note(note_id)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assert_no_store(stale)
        self.assertEqual(self.get_note(note_id), saved.json())
        deleted = self.delete_note(note_id, version=2)
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(deleted.content, b"")
        self.assert_no_store(deleted)
        with get_session() as session:
            self.assertIsNone(session.get(Note, note_id))
        missing = self.client.get(f"/notes/{note_id}", headers=self.headers["alice"])
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assert_no_store(missing)
        repeated = self.delete_note(note_id, version=2)
        self.assertEqual(repeated.status_code, 404, repeated.text)
        self.assert_no_store(repeated)

    def test_maximum_version_blocks_update_without_overflow_but_allows_delete(self) -> None:
        note_id = self.create_note()["id"]
        with get_session() as session:
            session.execute(update(Note).where(Note.id == note_id).values(version=MAX_BIGINT))
        original = self.get_note(note_id)
        self.assertEqual(original["version"], MAX_BIGINT)
        blocked = self.put_note(note_id, title="Must not overflow", version=MAX_BIGINT)
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assert_no_store(blocked)
        self.assertEqual(self.get_note(note_id), original)
        deleted = self.delete_note(note_id, version=MAX_BIGINT)
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(deleted.content, b"")
        self.assert_no_store(deleted)
        with get_session() as session:
            self.assertIsNone(session.get(Note, note_id))

    def test_all_current_team_members_can_edit_and_delete_each_others_notes(self) -> None:
        owner_note = self.create_note("alice", team_id=self.team_id)
        member_edit = self.put_note(owner_note["id"], "bob", title="Member's edit")
        self.assertEqual(member_edit.status_code, 200, member_edit.text)
        self.assert_no_store(member_edit)
        self.assertEqual(member_edit.json()["owner_id"], self.users["alice"])
        self.assertEqual(member_edit.json()["team_id"], self.team_id)
        member_delete = self.delete_note(owner_note["id"], "bob", version=2)
        self.assertEqual(member_delete.status_code, 204, member_delete.text)
        self.assert_no_store(member_delete)
        member_note = self.create_note("bob", team_id=self.team_id)
        owner_edit = self.put_note(member_note["id"], title="Owner's edit")
        self.assertEqual(owner_edit.status_code, 200, owner_edit.text)
        self.assert_no_store(owner_edit)
        self.assertEqual(owner_edit.json()["owner_id"], self.users["bob"])
        owner_delete = self.delete_note(member_note["id"], version=2)
        self.assertEqual(owner_delete.status_code, 204, owner_delete.text)
        self.assert_no_store(owner_delete)

    def test_unknown_and_forbidden_mutations_have_identical_not_found_responses(self) -> None:
        private = self.create_note("alice", title="Private secret")
        shared = self.create_note("bob", team_id=self.team_id, title="Team secret")
        for operation in ("PUT", "DELETE"):
            responses = []
            for note_id in (private["id"], shared["id"], MAX_BIGINT):
                if operation == "PUT":
                    response = self.put_note(note_id, "carol", version=777)
                else:
                    response = self.delete_note(note_id, "carol", version=777)
                responses.append(response)
                self.assertEqual(response.status_code, 404, response.text)
                self.assert_no_store(response)
                self.assertNotIn("secret", response.text)
            self.assertTrue(all(response.json() == responses[0].json() for response in responses))
        teammate = self.put_note(private["id"], "bob")
        self.assertEqual(teammate.status_code, 404, teammate.text)
        self.assert_no_store(teammate)
        teammate_delete = self.delete_note(private["id"], "bob")
        self.assertEqual(teammate_delete.status_code, 404, teammate_delete.text)
        self.assert_no_store(teammate_delete)
        self.assertEqual(self.get_note(private["id"]), private)
        self.assertEqual(self.get_note(shared["id"]), shared)

    def test_removed_creator_loses_team_mutation_access_but_keeps_private_access(self) -> None:
        shared = self.create_note("bob", team_id=self.team_id)
        private = self.create_note("bob")
        removed = self.client.delete(
            f"/teams/{self.team_id}/members/{self.users['bob']}", headers=self.headers["alice"]
        )
        self.assertEqual(removed.status_code, 204, removed.text)
        for denied in (self.put_note(shared["id"], "bob"), self.delete_note(shared["id"], "bob")):
            self.assertEqual(denied.status_code, 404, denied.text)
            self.assert_no_store(denied)
        self.assertEqual(self.get_note(shared["id"]), shared)
        current_member = self.put_note(shared["id"], title="Still editable by the team")
        self.assertEqual(current_member.status_code, 200, current_member.text)
        personal = self.put_note(private["id"], "bob")
        self.assertEqual(personal.status_code, 200, personal.text)
        self.assertEqual(self.delete_note(private["id"], "bob", version=2).status_code, 204)

    def test_invalid_update_payloads_leave_the_note_unchanged(self) -> None:
        original = self.create_note()
        valid: dict[str, Any] = {"title": "Valid", "content": "Valid", "version": 1}
        invalid: list[dict[str, Any]] = [
            {},
            {key: value for key, value in valid.items() if key != "title"},
            {key: value for key, value in valid.items() if key != "content"},
            {key: value for key, value in valid.items() if key != "version"},
            {**valid, "title": " \t\n "}, {**valid, "title": "x" * 201},
            {**valid, "title": 123}, {**valid, "title": None},
            {**valid, "content": "x" * 100001}, {**valid, "content": None},
            {**valid, "content": 123}, {**valid, "owner_id": self.users["bob"]},
            {**valid, "team_id": self.team_id}, {**valid, "unexpected": "value"},
        ] + [{**valid, "version": version} for version in (0, -1, True, "1", 1.5, None, MAX_BIGINT + 1)]
        for payload in invalid:
            with self.subTest(payload_keys=list(payload), version=payload.get("version")):
                response = self.client.put(
                    f"/notes/{original['id']}", json=payload, headers=self.headers["alice"]
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_store(response)
                self.assertEqual(self.get_note(original["id"]), original)

    def test_delete_requires_a_bounded_version_and_both_routes_bound_note_ids(self) -> None:
        original = self.create_note()
        queries: list[dict[str, str | int]] = [{}, *[
            {"version": version} for version in (0, -1, "invalid", "true", "1.5", MAX_BIGINT + 1)
        ]]
        for params in queries:
            with self.subTest(params=params):
                response = self.client.delete(
                    f"/notes/{original['id']}", params=params, headers=self.headers["alice"]
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_store(response)
        for note_id in (0, -1, "invalid", MAX_BIGINT + 1):
            for method in ("PUT", "DELETE"):
                with self.subTest(note_id=note_id, method=method):
                    response = self.client.request(
                        method,
                        f"/notes/{note_id}",
                        json={"title": "Valid", "content": "Valid", "version": 1} if method == "PUT" else None,
                        params={"version": 1} if method == "DELETE" else None,
                        headers=self.headers["alice"],
                    )
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assert_no_store(response)
        self.assertEqual(self.get_note(original["id"]), original)

    def test_missing_invalid_expired_and_revoked_sessions_cannot_mutate(self) -> None:
        note = self.create_note()
        self.assertEqual(self.client.post("/auth/logout", headers=self.headers["bob"]).status_code, 204)
        now = datetime.now(UTC)
        with get_session() as session:
            session.execute(update(AuthSession).where(AuthSession.user_id == self.users["carol"]).values(
                created_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1)
            ))
        for headers in ({}, {"Authorization": "Bearer invalid"}, self.headers["bob"], self.headers["carol"]):
            for method in ("PUT", "DELETE"):
                with self.subTest(headers=headers, method=method):
                    response = self.client.request(
                        method,
                        f"/notes/{note['id']}",
                        json={"title": "Unauthorized", "content": "Unauthorized", "version": 1} if method == "PUT" else None,
                        params={"version": 1} if method == "DELETE" else None,
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
                    self.assert_no_store(response)
        self.assertEqual(self.get_note(note["id"]), note)

    def simultaneous_mutation(
        self, note_id: int, barrier: Barrier, operation: Literal["PUT", "DELETE"], title: str
    ) -> Response:
        barrier.wait(timeout=5)
        if operation == "PUT":
            return self.put_note(note_id, title=title)
        return self.delete_note(note_id)

    def run_race(
        self, note_id: int, operations: tuple[Literal["PUT", "DELETE"], Literal["PUT", "DELETE"]]
    ) -> list[Response]:
        barrier = Barrier(3)
        with ThreadPoolExecutor(max_workers=2) as executor:
            with get_session() as session:
                # Hold the row so both HTTP requests reach the same version before release.
                session.scalar(select(Note).where(Note.id == note_id).with_for_update())
                pending: list[Future[Response]] = [
                    executor.submit(self.simultaneous_mutation, note_id, barrier, operation, f"Writer {index}")
                    for index, operation in enumerate(operations)
                ]
                barrier.wait(timeout=5)
                for result in pending:
                    with self.assertRaises(TimeoutError):
                        result.result(timeout=0.3)
            responses = [result.result(timeout=10) for result in pending]
        for response in responses:
            self.assert_no_store(response)
        return responses

    def test_simultaneous_updates_with_same_version_have_exactly_one_winner(self) -> None:
        original = self.create_note()
        responses = self.run_race(original["id"], ("PUT", "PUT"))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = next(response.json() for response in responses if response.status_code == 200)
        self.assertEqual(winner["version"], 2)
        self.assertEqual(self.get_note(original["id"]), winner)

    def test_simultaneous_update_and_delete_with_same_version_have_one_winner(self) -> None:
        original = self.create_note()
        put_response, delete_response = self.run_race(original["id"], ("PUT", "DELETE"))
        if put_response.status_code == 200:
            self.assertEqual(delete_response.status_code, 409, delete_response.text)
            self.assertEqual(put_response.json()["version"], 2)
            self.assertEqual(self.get_note(original["id"]), put_response.json())
        else:
            self.assertEqual(delete_response.status_code, 204, delete_response.text)
            self.assertEqual(delete_response.content, b"")
            self.assertIn(put_response.status_code, (404, 409), put_response.text)
            with get_session() as session:
                self.assertIsNone(session.get(Note, original["id"]))

    def test_membership_removal_winning_transaction_prevents_update_and_delete(self) -> None:
        for operation in ("PUT", "DELETE"):
            with self.subTest(operation=operation):
                original = self.create_note("bob", team_id=self.team_id)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with get_session() as session:
                        session.execute(delete(Membership).where(
                            Membership.team_id == self.team_id, Membership.user_id == self.users["bob"]
                        ))
                        if operation == "PUT":
                            pending = executor.submit(self.put_note, original["id"], "bob")
                        else:
                            pending = executor.submit(self.delete_note, original["id"], "bob")
                        with self.assertRaises(TimeoutError):
                            pending.result(timeout=0.3)
                    response = pending.result(timeout=10)
                self.assertEqual(response.status_code, 404, response.text)
                self.assert_no_store(response)
                self.assertEqual(self.get_note(original["id"]), original)
                with get_session() as session:
                    session.add(Membership(team_id=self.team_id, user_id=self.users["bob"], role="member"))
