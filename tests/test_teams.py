"""Team HTTP integration tests using isolated PostgreSQL schemas."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, event, func, select, update
from sqlalchemy.orm import Mapper

from app.database import get_session
from app.main import app
from app.models import AuthSession, Membership, Team, User
from app.security import hash_token
from test_database import PostgreSQLTestCase


class TeamTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))
        self.alice, self.alice_headers = self.seed_account("Alice")
        self.bob, self.bob_headers = self.seed_account("Bob")
        self.carol, self.carol_headers = self.seed_account("Carol")

    def seed_account(self, name: str) -> tuple[int, dict[str, str]]:
        token = f"test-team-session-{name}"
        with get_session() as session:
            user = User(
                email=f"{name.lower()}@example.com",
                display_name=name,
                password_hash="unused-in-team-tests",
            )
            session.add(user)
            session.flush()
            user_id = user.id
            session.add(AuthSession(
                token_hash=hash_token(token),
                user_id=user_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ))
        return user_id, {"Authorization": f"Bearer {token}"}

    def create_team(
        self, name: str = "Engineering", headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        response = self.client.post(
            "/teams", json={"name": name}, headers=headers or self.alice_headers
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        return response.json()

    def add_member(
        self, team_id: int, user_id: int, headers: dict[str, str] | None = None
    ) -> Response:
        return self.client.post(
            f"/teams/{team_id}/members",
            json={"user_id": user_id},
            headers=headers or self.alice_headers,
        )

    def test_create_team_and_list_only_callers_teams(self) -> None:
        self.assertEqual(
            self.client.get("/teams", headers=self.alice_headers).json(), []
        )
        engineering = self.create_team("  Engineering  ")
        research = self.create_team("Research", self.bob_headers)
        personal = self.create_team("Personal")
        self.assertEqual(set(engineering), {"id", "name", "created_at", "role"})
        self.assertEqual(engineering["name"], "Engineering")
        self.assertEqual(engineering["role"], "owner")
        self.assertIsNotNone(datetime.fromisoformat(engineering["created_at"]).tzinfo)
        with get_session() as session:
            owner = session.scalars(
                select(Membership).where(Membership.team_id == engineering["id"])
            ).one()
            self.assertEqual((owner.user_id, owner.role), (self.alice, "owner"))
        self.assertEqual(self.add_member(engineering["id"], self.bob).status_code, 201)
        alice_teams = self.client.get("/teams", headers=self.alice_headers)
        self.assertEqual(alice_teams.status_code, 200, alice_teams.text)
        self.assertEqual(alice_teams.headers.get("Cache-Control"), "no-store")
        self.assertEqual(alice_teams.json(), [engineering, personal])
        bob_teams = self.client.get("/teams", headers=self.bob_headers)
        self.assertEqual(bob_teams.status_code, 200, bob_teams.text)
        self.assertEqual(bob_teams.headers.get("Cache-Control"), "no-store")
        self.assertEqual(bob_teams.json(), [{**engineering, "role": "member"}, research])
        self.assertEqual(
            self.client.get("/teams", headers=self.carol_headers).json(), []
        )

    def test_owner_adds_member_and_members_can_view_public_roster(self) -> None:
        team = self.create_team()
        response = self.add_member(team["id"], self.bob)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        membership = response.json()
        self.assertEqual(
            set(membership), {"user_id", "display_name", "role", "created_at"}
        )
        self.assertEqual(membership["user_id"], self.bob)
        self.assertEqual(membership["display_name"], "Bob")
        self.assertEqual(membership["role"], "member")
        self.assertIsNotNone(datetime.fromisoformat(membership["created_at"]).tzinfo)
        for headers in (self.alice_headers, self.bob_headers):
            response = self.client.get(f"/teams/{team['id']}/members", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(
                [(member["user_id"], member["role"]) for member in response.json()],
                [(self.alice, "owner"), (self.bob, "member")],
            )
            self.assertEqual(response.json()[1], membership)
            for member in response.json():
                self.assertEqual(
                    set(member), {"user_id", "display_name", "role", "created_at"}
                )

    def test_ordinary_members_cannot_change_membership(self) -> None:
        team_id = self.create_team()["id"]
        self.assertEqual(self.add_member(team_id, self.bob).status_code, 201)
        denied_add = self.add_member(team_id, self.carol, self.bob_headers)
        denied_remove = self.client.delete(
            f"/teams/{team_id}/members/{self.alice}", headers=self.bob_headers
        )
        denied_self_remove = self.client.delete(
            f"/teams/{team_id}/members/{self.bob}", headers=self.bob_headers
        )
        for response in (denied_add, denied_remove, denied_self_remove):
            self.assertEqual(response.status_code, 403, response.text)
        roster = self.client.get(
            f"/teams/{team_id}/members", headers=self.alice_headers
        ).json()
        self.assertEqual([member["user_id"] for member in roster], [self.alice, self.bob])

    def test_outsiders_cannot_discover_or_modify_another_teams_roster(self) -> None:
        team_id = self.create_team()["id"]
        self.create_team("Carol's team", self.carol_headers)
        for hidden_team in (team_id, 999999):
            with self.subTest(team_id=hidden_team):
                responses = [
                    self.client.get(
                        f"/teams/{hidden_team}/members", headers=self.carol_headers
                    ),
                    self.add_member(hidden_team, self.bob, self.carol_headers),
                    self.add_member(hidden_team, 999999, self.carol_headers),
                    self.client.delete(
                        f"/teams/{hidden_team}/members/{self.alice}",
                        headers=self.carol_headers,
                    ),
                ]
                for response in responses:
                    self.assertEqual(response.status_code, 404, response.text)
                self.assertTrue(all(response.json() == responses[0].json() for response in responses))
        roster = self.client.get(
            f"/teams/{team_id}/members", headers=self.alice_headers
        ).json()
        self.assertEqual([member["user_id"] for member in roster], [self.alice])

    def test_removed_member_immediately_loses_roster_and_team_visibility(self) -> None:
        team_id = self.create_team()["id"]
        self.assertEqual(self.add_member(team_id, self.bob).status_code, 201)
        removed = self.client.delete(
            f"/teams/{team_id}/members/{self.bob}", headers=self.alice_headers
        )
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(removed.headers.get("Cache-Control"), "no-store")
        self.assertEqual(removed.content, b"")
        self.assertEqual(self.client.get("/teams", headers=self.bob_headers).json(), [])
        self.assertEqual(
            self.client.get(f"/teams/{team_id}/members", headers=self.bob_headers).status_code,
            404,
        )
        self.assertEqual(self.add_member(team_id, self.carol, self.bob_headers).status_code, 404)
        repeated = self.client.delete(
            f"/teams/{team_id}/members/{self.bob}", headers=self.alice_headers
        )
        self.assertEqual(repeated.status_code, 404, repeated.text)
        self.assertEqual(self.add_member(team_id, self.bob).status_code, 201)

    def test_owners_cannot_be_removed_or_readded_as_members(self) -> None:
        team_id = self.create_team()["id"]
        with get_session() as session:
            session.add(Membership(team_id=team_id, user_id=self.bob, role="owner"))
        for owner in (self.alice, self.bob):
            with self.subTest(owner=owner):
                response = self.client.delete(
                    f"/teams/{team_id}/members/{owner}", headers=self.alice_headers
                )
                self.assertEqual(response.status_code, 409, response.text)
                duplicate = self.add_member(team_id, owner)
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
        with get_session() as session:
            roles = session.scalars(
                select(Membership.role)
                .where(Membership.team_id == team_id)
                .order_by(Membership.user_id)
            ).all()
        self.assertEqual(roles, ["owner", "owner"])

    def test_duplicate_and_unknown_members_fail_without_membership_changes(self) -> None:
        team_id = self.create_team()["id"]
        first = self.add_member(team_id, self.bob)
        self.assertEqual(first.status_code, 201, first.text)
        duplicate = self.add_member(team_id, self.bob)
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        missing = self.add_member(team_id, 999999)
        self.assertEqual(missing.status_code, 404, missing.text)
        with get_session() as session:
            members = session.scalars(
                select(Membership.user_id)
                .where(Membership.team_id == team_id)
                .order_by(Membership.user_id)
            ).all()
        self.assertEqual(members, [self.alice, self.bob])

    def test_all_team_routes_require_authentication(self) -> None:
        team_id = self.create_team()["id"]
        for headers in ({}, {"Authorization": "Bearer unknown-token"}):
            with self.subTest(headers=headers):
                responses = [
                    self.client.post("/teams", json={"name": "Unauthorized"}, headers=headers),
                    self.client.get("/teams", headers=headers),
                    self.client.get(f"/teams/{team_id}/members", headers=headers),
                    self.client.post(
                        f"/teams/{team_id}/members", json={"user_id": self.bob}, headers=headers
                    ),
                    self.client.delete(
                        f"/teams/{team_id}/members/{self.alice}", headers=headers
                    ),
                ]
                for response in responses:
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")

    def test_revoked_and_expired_sessions_cannot_access_teams(self) -> None:
        team_id = self.create_team()["id"]
        revoked = self.client.post("/auth/logout", headers=self.alice_headers)
        self.assertEqual(revoked.status_code, 204, revoked.text)
        now = datetime.now(UTC)
        with get_session() as session:
            session.execute(
                update(AuthSession)
                .where(AuthSession.user_id == self.bob)
                .values(created_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1))
            )
        for headers in (self.alice_headers, self.bob_headers):
            self.assertEqual(self.client.get("/teams", headers=headers).status_code, 401)
            self.assertEqual(
                self.client.post(
                    f"/teams/{team_id}/members", json={"user_id": self.carol}, headers=headers
                ).status_code,
                401,
            )

    def test_team_and_member_input_validation_preserves_data(self) -> None:
        for payload in ({}, {"name": " \t\n "}, {"name": "x" * 101},
                        {"name": None}, {"name": 123}, {"name": "Valid", "role": "owner"}):
            with self.subTest(payload=payload):
                response = self.client.post("/teams", json=payload, headers=self.alice_headers)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/teams", headers=self.alice_headers).json(), [])
        team_id = self.create_team("x" * 100)["id"]
        for payload in ({}, {"user_id": 0}, {"user_id": -1}, {"user_id": None},
                        {"user_id": 1.5}, {"user_id": True}, {"user_id": str(self.bob)},
                        {"user_id": 2**63}, {"user_id": self.bob, "role": "owner"}):
            with self.subTest(payload=payload):
                response = self.client.post(
                    f"/teams/{team_id}/members", json=payload, headers=self.alice_headers
                )
                self.assertEqual(response.status_code, 422, response.text)
        for bad_id in ("0", "-1", "invalid", str(2**63)):
            with self.subTest(team_id=bad_id):
                response = self.client.get(
                    f"/teams/{bad_id}/members", headers=self.alice_headers
                )
                self.assertEqual(response.status_code, 422, response.text)
                response = self.client.delete(
                    f"/teams/{team_id}/members/{bad_id}", headers=self.alice_headers
                )
                self.assertEqual(response.status_code, 422, response.text)
        roster = self.client.get(
            f"/teams/{team_id}/members", headers=self.alice_headers
        ).json()
        self.assertEqual([member["user_id"] for member in roster], [self.alice])

    def test_team_creation_rolls_back_if_owner_membership_cannot_be_saved(self) -> None:
        # Fail during the membership flush after a real team INSERT has occurred.
        observed_team_counts: list[int | None] = []

        def reject_membership(
            _mapper: Mapper[Membership], _connection: Connection, _membership: Membership
        ) -> None:
            observed_team_counts.append(
                _connection.scalar(select(func.count()).select_from(Team))
            )
            raise RuntimeError("simulated membership storage failure")

        event.listen(Membership, "before_insert", reject_membership)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/teams", json={"name": "Should roll back"}, headers=self.alice_headers
                )
        finally:
            event.remove(Membership, "before_insert", reject_membership)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(observed_team_counts, [1])
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Team)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(Membership)), 0)
