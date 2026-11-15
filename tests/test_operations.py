import asyncio
import os
import socket
import threading
import unittest
from unittest.mock import patch

import anyio.to_thread
import httpx2
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, TimeoutError as PoolTimeout

from app.main import app
from app.database import get_engine


class HealthCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_liveness_responds_while_database_worker_capacity_is_exhausted(self) -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_capacity = limiter.total_tokens
        limiter.total_tokens = 1
        started = threading.Event()
        release = threading.Event()

        def stalled_database(*_args: object) -> None:
            started.set()
            release.wait(timeout=5)
            raise HTTPException(status_code=503, detail="Simulated database stall")

        transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        try:
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://audit"
            ) as client:
                with patch("app.auth.try_consume_login_attempt", side_effect=stalled_database):
                    login = asyncio.create_task(client.post(
                        "/auth/login",
                        json={"email": "reader@example.com", "password": "valid-passphrase"},
                    ))
                    health = None
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 2))
                        self.assertEqual(limiter.borrowed_tokens, 1)
                        health = asyncio.create_task(client.get("/health"))
                        response = await asyncio.wait_for(asyncio.shield(health), timeout=1)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json(), {"status": "ok"})
                        self.assertFalse(release.is_set())
                    finally:
                        release.set()
                        await asyncio.gather(
                            login, *([health] if health is not None else []),
                            return_exceptions=True,
                        )
                    self.assertEqual(login.result().status_code, 503)
        finally:
            release.set()
            limiter.total_tokens = original_capacity


class OperationalEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_readiness_checks_database_and_never_exposes_internal_errors(self) -> None:
        with patch("app.main.check_database_ready") as ready:
            response = self.client.get("/ready")
        ready.assert_called_once()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "ok"})
        with patch(
            "app.main.check_database_ready",
            side_effect=RuntimeError("private-database-url"),
        ):
            failed = self.client.get("/ready")
            health = self.client.get("/health")
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertEqual(failed.json(), {"detail": "Service temporarily unavailable"})
        self.assertEqual(failed.headers["Cache-Control"], "no-store")
        self.assertNotIn("private-database-url", failed.text)
        self.assertEqual(health.status_code, 200)
        with patch(
            "app.main.check_database_ready",
            side_effect=ProgrammingError(
                "private-schema-query", {}, RuntimeError("permission denied")
            ),
        ):
            denied = self.client.get("/ready")
        self.assertEqual(denied.status_code, 503, denied.text)
        self.assertEqual(denied.headers["Retry-After"], "1")
        self.assertNotIn("private-schema-query", denied.text)

    def test_metrics_are_bounded_and_do_not_include_request_identifiers(self) -> None:
        self.client.get("/notes/987654321")
        self.client.get("/unknown-sensitive-path-987654321")
        with patch.dict(os.environ, {"METRICS_TOKEN": ""}):
            response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("notes_http_requests_total", response.text)
        self.assertIn("notes_http_request_duration_seconds", response.text)
        self.assertIn('route="/notes/{note_id}"', response.text)
        self.assertIn('route="unmatched"', response.text)
        self.assertNotIn("987654321", response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_metrics_bearer_token_is_required_when_configured(self) -> None:
        with patch.dict(os.environ, {"METRICS_TOKEN": "local-test-metrics-token"}):
            denied = self.client.get("/metrics")
            wrong = self.client.get(
                "/metrics", headers={"Authorization": "Bearer wrong"}
            )
            allowed = self.client.get(
                "/metrics",
                headers={"Authorization": "Bearer local-test-metrics-token"},
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(denied.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(allowed.status_code, 200)

    def test_exhausted_connection_pool_has_a_retryable_safe_response(self) -> None:
        with (
            patch("app.auth.try_consume_login_attempt"),
            patch(
                "app.auth.get_session",
                side_effect=PoolTimeout("secret-connection-string"),
            ),
        ):
            response = self.client.post(
                "/auth/login",
                json={"email": "reader@example.com", "password": "valid-passphrase"},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": "Service temporarily unavailable"})
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertNotIn("secret-connection-string", response.text)

    def test_closed_database_port_returns_safe_retryable_unavailability(self) -> None:
        # Reserving but not listening on the port makes this a real failed
        # connection without depending on a particular service being installed.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            with patch.dict(
                os.environ,
                {
                    "DATABASE_URL": f"postgresql://notes:private-test-password@127.0.0.1:{port}/unavailable",
                    "DB_CONNECT_TIMEOUT_SECONDS": "1",
                },
            ):
                try:
                    response = self.client.post(
                        "/auth/login",
                        json={
                            "email": "reader@example.com",
                            "password": "valid-passphrase",
                        },
                    )
                finally:
                    get_engine().dispose()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers["Retry-After"], "1")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("private-test-password", response.text)

    def test_malformed_non_ascii_metrics_credentials_are_unauthorized(self) -> None:
        with patch.dict(os.environ, {"METRICS_TOKEN": "local-test-metrics-token"}):
            response = self.client.get(
                "/metrics", headers=[(b"authorization", b"Bearer\xff")]
            )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
