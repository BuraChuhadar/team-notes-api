from datetime import datetime, timedelta, UTC
from hashlib import sha256

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select, update

from app.database import get_session
from app.models import AuthSession, User
from app.main import app
from test_database import PostgreSQLTestCase

PASSWORD = "correct-horse-battery"


class AuthenticationTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.enterContext(TestClient(app))

    def register(self, **overrides: str) -> Response:
        payload = {"email": "alice@example.com", "display_name": "Alice", "password": PASSWORD}
        payload.update(overrides)
        return self.client.post("/auth/register", json=payload)

    def login(self, **overrides: str) -> Response:
        payload = {"email": "alice@example.com", "password": PASSWORD}
        payload.update(overrides)
        return self.client.post("/auth/login", json=payload)

    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_startup_applies_migrations_and_health_endpoint_responds(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)

    def test_register_login_me_and_logout(self) -> None:
        registered = self.register(email="Alice@EXAMPLE.COM", display_name="  Alice  ")
        self.assertEqual(registered.status_code, 201, registered.text)
        user = registered.json()
        self.assertEqual(set(user), {"id", "email", "display_name", "created_at"})
        self.assertEqual(user["email"], "alice@example.com")
        self.assertEqual(user["display_name"], "Alice")
        self.assertNotIn(PASSWORD, registered.text)
        logged_in = self.login(email="ALICE@example.com")
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        session = logged_in.json()
        self.assertEqual(set(session), {"access_token", "token_type", "expires_at"})
        self.assertEqual(session["token_type"], "bearer")
        headers = self.bearer(session["access_token"])
        me = self.client.get("/auth/me", headers=headers)
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json(), user)
        logged_out = self.client.post("/auth/logout", headers=headers)
        self.assertEqual(logged_out.status_code, 204, logged_out.text)
        self.assertEqual(logged_out.content, b"")
        self.assertEqual(self.client.get("/auth/me", headers=headers).status_code, 401)
        for response in (registered, logged_in, me, logged_out):
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_duplicate_registration_is_case_insensitive(self) -> None:
        self.assertEqual(self.register().status_code, 201)
        duplicate = self.register(email="ALICE@example.com")
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.headers.get("Cache-Control"), "no-store")
        with get_session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(User)), 1)

    def test_registration_rejects_invalid_input_without_echoing_passwords(self) -> None:
        valid = {"email": "alice@example.com", "display_name": "Alice", "password": PASSWORD}
        invalid = [
            {key: value for key, value in valid.items() if key != "password"},
            {key: value for key, value in valid.items() if key != "email"},
            {**valid, "email": "not-an-email"},
            {**valid, "display_name": " \t\n "},
            {**valid, "display_name": "A" * 101},
            {**valid, "password": "shortsecret"},
            {**valid, "password": "s" * 129},
            {**valid, "role": "admin"},
        ]
        for payload in invalid:
            with self.subTest(payload_keys=list(payload), email=payload.get("email")):
                response = self.client.post("/auth/register", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                self.assertNotIn(payload.get("password", PASSWORD), response.text)
                for error in response.json()["detail"]:
                    self.assertNotIn("input", error)

    def test_unknown_email_and_wrong_password_have_identical_errors(self) -> None:
        self.assertEqual(self.register().status_code, 201)
        wrong_password = self.login(password="incorrect-password")
        unknown_user = self.login(email="nobody@example.com")
        self.assertEqual(wrong_password.status_code, 401, wrong_password.text)
        self.assertEqual(unknown_user.status_code, 401, unknown_user.text)
        self.assertEqual(wrong_password.json(), unknown_user.json())
        for response in (wrong_password, unknown_user):
            self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertNotIn("incorrect-password", response.text)

    def test_missing_malformed_and_unknown_tokens_are_unauthorized(self) -> None:
        for authorization in (
            None, "Bearer", "Basic credentials", "Bearer token with spaces",
            "Bearer unknown-token", "Bearer " + "x" * 257,
        ):
            with self.subTest(authorization=authorization):
                headers = {} if authorization is None else {"Authorization": authorization}
                response = self.client.get("/auth/me", headers=headers)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        response = self.client.post("/auth/logout")
        self.assertEqual(response.status_code, 401)

    def test_password_and_session_storage_contain_only_hashes(self) -> None:
        self.assertEqual(self.register().status_code, 201)
        response = self.login()
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["access_token"]
        with get_session() as session:
            user = session.scalars(select(User)).one()
            stored_session = session.scalars(select(AuthSession)).one()
            self.assertTrue(user.password_hash.startswith("$argon2id$"))
            self.assertNotIn(PASSWORD, user.password_hash)
            self.assertEqual(stored_session.token_hash, sha256(token.encode()).hexdigest())
            self.assertNotIn(token, stored_session.token_hash)
            self.assertEqual(stored_session.expires_at - stored_session.created_at, timedelta(hours=1))

    def test_logout_revokes_only_the_current_session(self) -> None:
        self.assertEqual(self.register().status_code, 201)
        first = self.login().json()["access_token"]
        second = self.login().json()["access_token"]
        self.assertNotEqual(first, second)
        response = self.client.post("/auth/logout", headers=self.bearer(first))
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.client.get("/auth/me", headers=self.bearer(first)).status_code, 401)
        self.assertEqual(self.client.get("/auth/me", headers=self.bearer(second)).status_code, 200)

    def test_expired_session_is_unauthorized(self) -> None:
        self.assertEqual(self.register().status_code, 201)
        response = self.login()
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["access_token"]
        now = datetime.now(UTC)
        with get_session() as session:
            session.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == sha256(token.encode()).hexdigest())
                .values(created_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1))
            )
        expired = self.client.get("/auth/me", headers=self.bearer(token))
        self.assertEqual(expired.status_code, 401, expired.text)
        self.assertEqual(expired.headers.get("WWW-Authenticate"), "Bearer")

    def test_password_whitespace_is_preserved(self) -> None:
        password = f" {PASSWORD} "
        self.assertEqual(self.register(password=password).status_code, 201)
        self.assertEqual(self.login(password=password).status_code, 200)
        self.assertEqual(self.login(password=password.strip()).status_code, 401)

    def test_login_validation_does_not_echo_credentials(self) -> None:
        for payload in ({"email": "alice@example.com"},
                        {"email": "invalid", "password": PASSWORD},
                        {"email": "alice@example.com", "password": "x" * 129},
                        {"email": "alice@example.com", "password": PASSWORD, "role": "admin"}):
            with self.subTest(payload_keys=list(payload)):
                response = self.client.post("/auth/login", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn(payload.get("password", PASSWORD), response.text)
