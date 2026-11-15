"""Exercise shared login budgets through PostgreSQL and public HTTP responses."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select, update

from app.database import get_session
from app.main import app
from app.models import AuthSession, LoginThrottle
from app.rate_limit import get_rate_limit_settings, try_consume_login_attempt
from test_database import PostgreSQLTestCase


class RateLimitSettingsTests(unittest.TestCase):
    def test_settings_reject_disabled_unbounded_and_invalid_limits(self) -> None:
        for variable, values in {
            "LOGIN_RATE_LIMIT_ATTEMPTS": ("0", "-1", "10001", "unlimited"),
            "LOGIN_IP_RATE_LIMIT_ATTEMPTS": ("0", "100001", "1.5"),
            "LOGIN_RATE_LIMIT_WINDOW_SECONDS": ("0", "86401", "forever"),
        }.items():
            for value in values:
                with self.subTest(variable=variable, value=value):
                    with patch.dict(os.environ, {variable: value}):
                        with self.assertRaisesRegex(RuntimeError, variable):
                            get_rate_limit_settings()


class LoginRateLimitTests(PostgreSQLTestCase):
    def setUp(self) -> None:
        super().setUp()
        environment = patch.dict(
            os.environ,
            {
                "LOGIN_RATE_LIMIT_ATTEMPTS": "2",
                "LOGIN_IP_RATE_LIMIT_ATTEMPTS": "20",
                "LOGIN_RATE_LIMIT_WINDOW_SECONDS": "60",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.client = self.enterContext(TestClient(app))

    def attempt(self, email: str = "reader@example.com", **headers: str) -> Response:
        return self.client.post(
            "/auth/login",
            json={"email": email, "password": "example-passphrase"},
            headers=headers,
        )

    def test_failed_attempts_commit_and_block_before_password_verification(
        self,
    ) -> None:
        with patch("app.auth.verify_password", return_value=False) as verify:
            first = self.attempt()
            second = self.attempt(email="READER@example.com")
            blocked = self.attempt()
            blocked_again = self.attempt()
        self.assertEqual((first.status_code, second.status_code), (401, 401))
        self.assertEqual(verify.call_count, 2)
        for response in (blocked, blocked_again):
            self.assertEqual(response.status_code, 429, response.text)
            self.assertEqual(
                response.json(), {"detail": "Too many login attempts. Try again later."}
            )
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertTrue(1 <= int(response.headers["Retry-After"]) <= 60)
            self.assertNotIn("reader@example.com", response.text)
        with get_session() as session:
            counters = session.scalars(select(LoginThrottle)).all()
            self.assertEqual(len(counters), 2)
            self.assertEqual(sorted(counter.attempts for counter in counters), [3, 4])
            self.assertTrue(
                all(len(counter.subject_hash) == 64 for counter in counters)
            )
            self.assertNotIn(
                "reader@example.com",
                str([counter.subject_hash for counter in counters]),
            )

    def test_account_budget_is_shared_across_client_addresses(self) -> None:
        try_consume_login_attempt("reader@example.com", "192.0.2.1")
        try_consume_login_attempt("READER@example.com", "192.0.2.2")
        with self.assertRaises(HTTPException) as failure:
            try_consume_login_attempt("reader@example.com", "192.0.2.3")
        self.assertEqual(failure.exception.status_code, 429)

    def test_address_budget_cannot_be_bypassed_by_forwarded_headers(self) -> None:
        with patch.dict(os.environ, {"LOGIN_IP_RATE_LIMIT_ATTEMPTS": "2"}):
            with patch("app.auth.verify_password", return_value=False) as verify:
                responses = [
                    self.attempt(
                        email=f"reader{index}@example.com",
                        **{
                            "X-Forwarded-For": f"192.0.2.{index}",
                            "X-Real-IP": f"198.51.100.{index}",
                        },
                    )
                    for index in range(3)
                ]
        self.assertEqual(
            [response.status_code for response in responses], [401, 401, 429]
        )
        self.assertEqual(verify.call_count, 2)

    def test_unrelated_accounts_and_addresses_have_independent_budgets(self) -> None:
        try_consume_login_attempt("first@example.com", "192.0.2.1")
        try_consume_login_attempt("first@example.com", "192.0.2.1")
        try_consume_login_attempt("second@example.com", "192.0.2.2")
        try_consume_login_attempt("second@example.com", "192.0.2.2")
        with self.assertRaises(HTTPException):
            try_consume_login_attempt("first@example.com", "192.0.2.1")

    def test_expired_window_resets_and_retry_after_uses_remaining_time(self) -> None:
        try_consume_login_attempt("reader@example.com", "192.0.2.1")
        try_consume_login_attempt("reader@example.com", "192.0.2.1")
        with get_session() as session:
            session.execute(
                update(LoginThrottle).values(
                    window_started_at=func.now() - timedelta(seconds=40)
                )
            )
        with self.assertRaises(HTTPException) as failure:
            try_consume_login_attempt("reader@example.com", "192.0.2.1")
        headers = failure.exception.headers
        assert headers is not None
        self.assertTrue(18 <= int(headers["Retry-After"]) <= 21)
        with get_session() as session:
            session.execute(
                update(LoginThrottle).values(
                    window_started_at=func.now() - timedelta(seconds=61)
                )
            )
        try_consume_login_attempt("reader@example.com", "192.0.2.1")
        with get_session() as session:
            self.assertEqual(
                list(session.scalars(select(LoginThrottle.attempts))), [1, 1]
            )

    def test_successful_sign_ins_also_consume_the_password_verification_budget(
        self,
    ) -> None:
        registration = self.client.post(
            "/auth/register",
            json={
                "email": "reader@example.com",
                "display_name": "Reader",
                "password": "example-passphrase",
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        first, second, third = self.attempt(), self.attempt(), self.attempt()
        self.assertEqual(
            [response.status_code for response in (first, second, third)],
            [200, 200, 429],
        )
        with get_session() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(AuthSession)), 2
            )

    def test_concurrent_requests_cannot_overspend_the_account_budget(self) -> None:
        def attempt(index: int) -> int:
            try:
                try_consume_login_attempt("reader@example.com", f"192.0.2.{index + 1}")
            except HTTPException as error:
                return error.status_code
            return 200

        with patch.dict(
            os.environ,
            {"LOGIN_RATE_LIMIT_ATTEMPTS": "5", "LOGIN_IP_RATE_LIMIT_ATTEMPTS": "100"},
        ):
            with ThreadPoolExecutor(max_workers=12) as executor:
                responses = list(executor.map(attempt, range(12)))
        self.assertEqual(responses.count(200), 5)
        self.assertEqual(responses.count(429), 7)

    def test_invalid_request_does_not_spend_a_password_verification_budget(
        self,
    ) -> None:
        response = self.client.post(
            "/auth/login", json={"email": "invalid", "password": "x"}
        )
        self.assertEqual(response.status_code, 422, response.text)
        with get_session() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(LoginThrottle)), 0
            )

    def test_openapi_documents_the_rate_limit_response(self) -> None:
        response = app.openapi()["paths"]["/auth/login"]["post"]["responses"]["429"]
        self.assertEqual(
            response["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ErrorResponse",
        )
        self.assertIn("Retry-After", response["headers"])
        self.assertIn("Cache-Control", response["headers"])
