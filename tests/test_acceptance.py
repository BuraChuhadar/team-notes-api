"""End-to-end HTTP acceptance scenarios for collaboration and persistence."""

from contextlib import ExitStack
from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response

from app.main import app
from test_database import PostgreSQLTestCase

PASSWORD = "correct-horse-battery"


class AcceptanceTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.lifespans = self.enterContext(ExitStack())
        self.restart_application()
        self.users: dict[str, dict[str, Any]] = {}
        self.headers: dict[str, dict[str, str]] = {}
        for name in ("alice", "bob", "carol", "dan"):
            email = f"{name}@example.com"
            registered = self.client.post(
                "/auth/register",
                json={
                    "email": email,
                    "display_name": name.title(),
                    "password": PASSWORD,
                },
            )
            self.check_response(registered, 201)
            self.users[name] = registered.json()
            logged_in = self.client.post(
                "/auth/login", json={"email": email, "password": PASSWORD}
            )
            self.check_response(logged_in, 200)
            self.headers[name] = {
                "Authorization": f"Bearer {logged_in.json()['access_token']}"
            }
        self.source = self.request(
            "alice", "POST", "/teams", 201, json={"name": "Source team"}
        ).json()["id"]
        self.recipient = self.request(
            "carol", "POST", "/teams", 201, json={"name": "Recipient team"}
        ).json()["id"]
        self.request(
            "alice",
            "POST",
            f"/teams/{self.source}/members",
            201,
            json={"user_id": self.users["bob"]["id"]},
        )
        self.request(
            "carol",
            "POST",
            f"/teams/{self.recipient}/members",
            201,
            json={"user_id": self.users["alice"]["id"]},
        )

    def restart_application(self) -> None:
        # Exit and re-enter the complete ASGI lifespan, reapplying startup checks.
        self.lifespans.close()
        self.client = self.lifespans.enter_context(TestClient(app))

    def check_response(self, response: Response, status: int) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        if status == 204:
            self.assertEqual(response.content, b"")

    def request(
        self,
        user: str,
        method: str,
        path: str,
        status: int,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> Response:
        response = self.client.request(
            method, path, json=json, params=params, headers=self.headers[user]
        )
        self.check_response(response, status)
        return response

    def create_note(
        self, user: str, title: str, *, team_id: int | None = None
    ) -> dict[str, Any]:
        return self.request(
            user,
            "POST",
            "/notes",
            201,
            json={
                "title": title,
                "content": f"Content for {title}",
                "team_id": team_id,
            },
        ).json()

    def note_ids(self, user: str, **params: str | int) -> set[int]:
        return {
            note["id"]
            for note in self.request(user, "GET", "/notes", 200, params=params).json()
        }

    def test_collaboration_conflict_sharing_revocation_and_deletion(self) -> None:
        private = self.create_note("bob", "Bob's private draft")
        original = self.create_note("bob", "Team decision", team_id=self.source)
        path = f"/notes/{original['id']}"
        shares = f"{path}/shares"
        grant_path = f"{shares}/{self.recipient}"
        self.request("alice", "GET", f"/notes/{private['id']}", 404)
        self.request("dan", "GET", path, 404)

        current = self.request("alice", "GET", path, 200).json()
        saved = self.request(
            "alice",
            "PUT",
            path,
            200,
            json={
                "title": current["title"],
                "content": "Alice's accepted edit",
                "version": current["version"],
            },
        ).json()
        self.assertGreater(saved["version"], current["version"])
        self.assertEqual(
            (saved["owner_id"], saved["team_id"]), (original["owner_id"], self.source)
        )
        self.request(
            "bob",
            "PUT",
            path,
            409,
            json={
                "title": current["title"],
                "content": "Bob's stale edit",
                "version": current["version"],
            },
        )
        self.request("bob", "DELETE", path, 409, params={"version": current["version"]})
        self.assertEqual(self.request("bob", "GET", path, 200).json(), saved)

        grant = self.request(
            "alice", "POST", shares, 201, json={"team_id": self.recipient}
        )
        self.assertEqual(grant.headers.get("Location"), grant_path)
        self.assertEqual(
            self.request("alice", "GET", grant_path, 200).json(), grant.json()
        )
        self.assertEqual(
            self.request("alice", "GET", shares, 200).json(), [grant.json()]
        )
        self.assertEqual(self.request("carol", "GET", path, 200).json(), saved)
        self.assertIn(original["id"], self.note_ids("carol"))
        self.request(
            "carol",
            "PUT",
            path,
            404,
            json={
                "title": saved["title"],
                "content": "Recipient edit",
                "version": current["version"],
            },
        )
        self.request(
            "carol", "DELETE", path, 404, params={"version": current["version"]}
        )
        self.request("carol", "POST", shares, 404, json={"team_id": self.recipient})
        self.request("bob", "GET", shares, 404)
        self.request("carol", "DELETE", grant_path, 404)
        self.request("dan", "GET", grant_path, 404)

        self.request("alice", "DELETE", grant_path, 204)
        self.request("carol", "GET", path, 404)
        self.assertNotIn(original["id"], self.note_ids("carol"))
        self.request("alice", "GET", grant_path, 404)
        self.request("alice", "POST", shares, 201, json={"team_id": self.recipient})
        self.request(
            "alice",
            "DELETE",
            f"/teams/{self.source}/members/{self.users['bob']['id']}",
            204,
        )
        self.request("bob", "GET", path, 404)
        self.request(
            "bob",
            "PUT",
            path,
            404,
            json={
                "title": saved["title"],
                "content": "Former member edit",
                "version": saved["version"],
            },
        )
        self.assertNotIn(original["id"], self.note_ids("bob"))
        self.assertIn(private["id"], self.note_ids("bob"))
        self.request("bob", "GET", f"/notes/{private['id']}", 200)
        self.request("carol", "GET", path, 200)

        latest = self.request("alice", "GET", path, 200).json()
        self.request(
            "alice", "DELETE", path, 204, params={"version": latest["version"]}
        )
        for user in self.users:
            self.request(user, "GET", path, 404)
            self.assertNotIn(original["id"], self.note_ids(user))
        self.request("alice", "GET", shares, 404)

    def test_lifespan_restart_preserves_data_sessions_and_revocations(self) -> None:
        original = self.create_note("alice", "Persistent private note")
        path = f"/notes/{original['id']}"
        shares = f"{path}/shares"
        grant_path = f"{shares}/{self.recipient}"
        saved = self.request(
            "alice",
            "PUT",
            path,
            200,
            json={
                "title": original["title"],
                "content": "Saved before restart",
                "version": original["version"],
            },
        ).json()
        grant = self.request(
            "alice", "POST", shares, 201, json={"team_id": self.recipient}
        ).json()
        self.request("bob", "GET", path, 404)

        self.restart_application()
        for user, account in self.users.items():
            self.assertEqual(self.request(user, "GET", "/auth/me", 200).json(), account)
        self.assertEqual(self.request("alice", "GET", path, 200).json(), saved)
        self.assertEqual(self.request("carol", "GET", path, 200).json(), saved)
        self.assertEqual(self.request("alice", "GET", grant_path, 200).json(), grant)
        self.assertIn(original["id"], self.note_ids("carol"))

        self.request("bob", "POST", "/auth/logout", 204)
        self.request("alice", "DELETE", grant_path, 204)
        self.request("carol", "GET", path, 404)
        latest = self.request("alice", "GET", path, 200).json()
        saved_again = self.request(
            "alice",
            "PUT",
            path,
            200,
            json={
                "title": latest["title"],
                "content": "Saved after restart",
                "version": latest["version"],
            },
        ).json()

        self.restart_application()
        self.request("bob", "GET", "/auth/me", 401)
        self.request("bob", "GET", path, 401)
        self.request("carol", "GET", path, 404)
        self.assertNotIn(original["id"], self.note_ids("carol"))
        self.assertEqual(self.request("alice", "GET", path, 200).json(), saved_again)
        self.assertEqual(self.request("alice", "GET", shares, 200).json(), [])

    def test_search_pagination_tracks_current_memberships_and_private_shares(self) -> None:
        bob_private = self.create_note("bob", "Atlas private draft")
        shared_private = self.create_note("alice", "Atlas private research")
        source_note = self.create_note("bob", "Atlas source decision", team_id=self.source)
        content_match = self.request(
            "alice", "POST", "/notes", 201,
            json={
                "title": "Body match without the keyword",
                "content": "Investigate the aTlAs project in this paragraph.",
                "team_id": self.source,
            },
        ).json()
        recipient_note = self.create_note("carol", "Atlas recipient plan", team_id=self.recipient)
        self.create_note("alice", "Unrelated private note")
        self.create_note("bob", "Unrelated source note", team_id=self.source)
        # These newer matches must not consume Bob's pages or reveal private data.
        dan_private = self.create_note("dan", "Atlas outsider notes")
        carol_private = self.create_note("carol", "Atlas newest private note")
        grant_path = f"/notes/{shared_private['id']}/shares/{self.recipient}"
        self.request(
            "alice", "POST", f"/notes/{shared_private['id']}/shares", 201,
            json={"team_id": self.recipient},
        )

        def assert_search_pages(user: str, visible: list[dict[str, Any]]) -> None:
            ordered = sorted(
                visible,
                key=lambda note: (datetime.fromisoformat(note["created_at"]), note["id"]),
                reverse=True,
            )
            expected = [note["id"] for note in ordered]
            results = self.request(
                user, "GET", "/notes", 200, params={"q": " aTlAs ", "limit": 100}
            ).json()
            self.assertEqual([note["id"] for note in results], expected)
            for offset in range(len(expected) + 1):
                with self.subTest(user=user, offset=offset, visible=expected):
                    page = self.request(
                        user, "GET", "/notes", 200,
                        params={"q": " aTlAs ", "limit": 1, "offset": offset},
                    ).json()
                    self.assertEqual([note["id"] for note in page], expected[offset:offset + 1])

        assert_search_pages("alice", [shared_private, source_note, content_match, recipient_note])
        assert_search_pages("bob", [bob_private, source_note, content_match])
        assert_search_pages("carol", [shared_private, recipient_note, carol_private])
        assert_search_pages("dan", [dan_private])

        self.request("alice", "DELETE", grant_path, 204)
        self.request("carol", "GET", f"/notes/{shared_private['id']}", 404)
        assert_search_pages("carol", [recipient_note, carol_private])
        self.request(
            "alice", "DELETE", f"/teams/{self.source}/members/{self.users['bob']['id']}", 204
        )
        self.request("bob", "GET", f"/notes/{source_note['id']}", 404)
        assert_search_pages("bob", [bob_private])
