"""Reject PostgreSQL-incompatible text before writes or password processing."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select

from app.database import get_session
from app.main import app
from app.models import AuthSession, LoginThrottle, Note, Team, User
from app.security import hash_token
from test_database import PostgreSQLTestCase

PASSWORD = "text-validation-passphrase"
TEXT_PATTERN = r"^[^\x00]*$"


class TextValidationTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))
        token = token_urlsafe(32)
        with get_session() as session:
            user = User(
                email="text-owner@example.com",
                display_name="Text owner",
                password_hash="unused-in-text-validation-tests",
            )
            session.add(user)
            session.flush()
            session.add(AuthSession(
                token_hash=hash_token(token), user_id=user.id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ))
        self.headers = {"Authorization": "Bearer " + token}

    def assert_validation_error(self, response: Response) -> None:
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertIsInstance(response.json()["detail"], list)
        for issue in response.json()["detail"]:
            self.assertEqual(set(issue), {"type", "loc", "msg"})
        self.assertNotIn(PASSWORD, response.text)

    def raw_json_request(self, path: str, payload: dict[str, Any]) -> Response:
        # Escape JSON Unicode so lone surrogates reach server-side validation.
        return self.client.post(
            path,
            content=json.dumps(payload, ensure_ascii=True).encode("ascii"),
            headers={**self.headers, "Content-Type": "application/json"},
        )

    def test_nul_display_name_is_rejected_before_hashing_or_account_creation(self) -> None:
        with patch("app.auth.hash_password", side_effect=AssertionError("Password work must not run")):
            response = self.client.post("/auth/register", json={
                "email": "nul-name@example.com", "display_name": "Before\x00After", "password": PASSWORD,
            })
        self.assert_validation_error(response)
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(User)), 1)

    def test_nul_team_names_and_note_fields_create_no_rows(self) -> None:
        for path, payload in (
            ("/teams", {"name": "Before\x00After"}),
            ("/notes", {"title": "Before\x00After", "content": "Valid"}),
            ("/notes", {"title": "Valid", "content": "Before\x00After"}),
        ):
            with self.subTest(path=path, fields=list(payload)):
                self.assert_validation_error(self.client.post(path, json=payload, headers=self.headers))
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Team)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(Note)), 0)

    def test_nul_replacements_preserve_the_entire_stored_note(self) -> None:
        created = self.client.post(
            "/notes", json={"title": "Original", "content": "Original content"}, headers=self.headers
        )
        self.assertEqual(created.status_code, 201, created.text)
        original = created.json()
        path = f"/notes/{original['id']}"
        for payload in (
            {"title": "Before\x00After", "content": "Valid", "version": 1},
            {"title": "Valid", "content": "Before\x00After", "version": 1},
        ):
            with self.subTest(fields=list(payload)):
                self.assert_validation_error(self.client.put(path, json=payload, headers=self.headers))
                saved = self.client.get(path, headers=self.headers)
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json(), original)

    def test_nul_search_is_a_sanitized_validation_error(self) -> None:
        for query in ("\x00", "  before\x00after  "):
            with self.subTest(query_length=len(query)):
                response = self.client.get("/notes", params={"q": query}, headers=self.headers)
                self.assert_validation_error(response)
                self.assertEqual(response.json()["detail"][0]["loc"], ["query", "q"])

    def test_lone_surrogate_passwords_are_rejected_before_authentication_work(self) -> None:
        malformed = "malformed-secret-\ud800-passphrase"
        with (
            patch("app.auth.hash_password", side_effect=AssertionError("Hashing must not run")),
            patch("app.auth.verify_password", side_effect=AssertionError("Verification must not run")),
            patch("app.auth.try_consume_login_attempt", side_effect=AssertionError("Throttle must not run")),
        ):
            for path, payload in (
                ("/auth/register", {"email": "surrogate@example.com", "display_name": "Reader", "password": malformed}),
                ("/auth/login", {"email": "unknown@example.com", "password": malformed}),
            ):
                with self.subTest(path=path):
                    response = self.raw_json_request(path, payload)
                    self.assert_validation_error(response)
                    self.assertNotIn("malformed-secret-", response.text)
                    self.assertEqual(response.json()["detail"][0]["loc"], ["body", "password"])
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(User)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(AuthSession)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(LoginThrottle)), 0)

    def test_lone_surrogates_in_other_json_text_still_return_422(self) -> None:
        for path, payload in (
            ("/auth/register", {"email": "bad-name@example.com", "display_name": "Name\ud800", "password": PASSWORD}),
            ("/teams", {"name": "Name\ud800"}),
            ("/notes", {"title": "Title\ud800"}),
            ("/notes", {"title": "Valid", "content": "Content\ud800"}),
            ("/notes", {"title": "Valid", "\ud800": "Unknown field"}),
        ):
            with self.subTest(path=path, fields=len(payload)):
                self.assert_validation_error(self.raw_json_request(path, payload))

    def test_nul_unicode_and_password_whitespace_round_trip_through_argon2(self) -> None:
        password = "  correct\x00password-🥭  "
        registered = self.client.post("/auth/register", json={
            "email": "nul-password@example.com", "display_name": "NUL password", "password": password,
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertNotIn("password", registered.json())
        logged_in = self.client.post("/auth/login", json={
            "email": "nul-password@example.com", "password": password,
        })
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        current = self.client.get("/auth/me", headers={
            "Authorization": "Bearer " + logged_in.json()["access_token"],
        })
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json(), registered.json())
        wrong_whitespace = self.client.post("/auth/login", json={
            "email": "nul-password@example.com", "password": password.strip(),
        })
        self.assertEqual(wrong_whitespace.status_code, 401, wrong_whitespace.text)
        for response in (registered, logged_in, current, wrong_whitespace):
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn(password, response.text)

    def test_valid_unicode_and_whitespace_semantics_are_preserved(self) -> None:
        registered = self.client.post("/auth/register", json={
            "email": "unicode@example.com", "display_name": "  Zoë 🌿  ", "password": PASSWORD,
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertEqual(registered.json()["display_name"], "Zoë 🌿")
        team = self.client.post("/teams", json={"name": "  Team 🌿  "}, headers=self.headers)
        self.assertEqual(team.status_code, 201, team.text)
        self.assertEqual(team.json()["name"], "Team 🌿")
        title = "  Meeting 🌿\nAgenda  "
        content = "  \n\tDraft 🥭\nOther control: \x1f. Literal %_/.  "
        note = self.client.post("/notes", json={"title": title, "content": content}, headers=self.headers)
        self.assertEqual(note.status_code, 201, note.text)
        self.assertEqual(note.json()["title"], title.strip())
        self.assertEqual(note.json()["content"], content)
        query = self.client.get("/notes", params={"q": "  🌿\nAgenda  "}, headers=self.headers)
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json(), [note.json()])


class TextValidationSchemaTests(unittest.TestCase):
    def test_persisted_and_search_fields_publish_the_nul_constraint(self) -> None:
        document = app.openapi()
        schemas = document["components"]["schemas"]
        for model, fields in (
            ("RegisterRequest", ("display_name",)),
            ("CreateTeamRequest", ("name",)),
            ("NoteCreate", ("title", "content")),
            ("NoteUpdate", ("title", "content")),
        ):
            for name in fields:
                with self.subTest(model=model, field=name):
                    self.assertEqual(schemas[model]["properties"][name]["pattern"], TEXT_PATTERN)
        parameters = document["paths"]["/notes"]["get"]["parameters"]
        query = next(parameter for parameter in parameters if parameter["name"] == "q")
        text = next(candidate for candidate in query["schema"]["anyOf"] if candidate.get("type") == "string")
        self.assertEqual(text["pattern"], TEXT_PATTERN)
        self.assertEqual((text["minLength"], text["maxLength"]), (1, 200))
        for model, minimum in (("RegisterRequest", 12), ("LoginRequest", 1)):
            password = schemas[model]["properties"]["password"]
            self.assertTrue(password["writeOnly"])
            self.assertEqual((password["minLength"], password["maxLength"]), (minimum, 128))
