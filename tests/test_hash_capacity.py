"""Keep registration and password verification inside one process memory budget."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from threading import Barrier, Event, Lock
from unittest.mock import patch

from fastapi.testclient import TestClient
from pwdlib.exceptions import UnknownHashError
from sqlalchemy import func, select

from app import security
from app.database import get_session
from app.main import app
from app.models import User
from test_database import PostgreSQLTestCase


class PasswordHashCapacityTests(unittest.TestCase):
    def test_mixed_registration_and_dummy_verification_share_nonblocking_capacity(
        self,
    ) -> None:
        release = Event()
        both_admitted = Event()
        start = Barrier(8)
        lock = Lock()
        active = 0
        peak = 0

        def slow_operation() -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_admitted.set()
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("Test password work was not released.")
            finally:
                with lock:
                    active -= 1

        def slow_hash(password: str) -> str:
            slow_operation()
            return "test-only-hash"

        def slow_verify(password: str, encoded: str) -> bool:
            slow_operation()
            return False

        def request(index: int) -> str:
            start.wait(timeout=5)
            try:
                if index % 2:
                    security.verify_password(
                        "example-passphrase", security.DUMMY_PASSWORD_HASH
                    )
                else:
                    security.hash_password("example-passphrase")
            except security.PasswordHashCapacityError:
                return "rejected"
            return "admitted"

        with (
            patch.dict(os.environ, {"PASSWORD_HASH_CONCURRENCY": "2"}),
            patch("app.security._password_hash.hash", side_effect=slow_hash),
            patch("app.security._password_hash.verify", side_effect=slow_verify),
        ):
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(request, index) for index in range(8)]
                try:
                    self.assertTrue(both_admitted.wait(timeout=5))
                    completed, pending = wait(futures, timeout=1)
                    # Excess work must finish with rejection while admitted work
                    # is still held; it must not wait in a password-hashing queue.
                    self.assertEqual(len(completed), 6)
                    self.assertEqual(len(pending), 2)
                    self.assertEqual(
                        [future.result() for future in completed], ["rejected"] * 6
                    )
                    self.assertEqual(peak, 2)
                finally:
                    release.set()
                responses = [future.result(timeout=5) for future in futures]
        self.assertEqual(responses.count("admitted"), 2)
        self.assertEqual(responses.count("rejected"), 6)
        self.assertEqual(active, 0)

    def test_slots_are_released_after_encoder_errors_and_unknown_hashes(self) -> None:
        with patch.dict(os.environ, {"PASSWORD_HASH_CONCURRENCY": "1"}):
            with patch(
                "app.security._password_hash.hash",
                side_effect=RuntimeError("encoder unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "encoder unavailable"):
                    security.hash_password("example-passphrase")
            with patch("app.security._password_hash.hash", return_value="next-hash"):
                self.assertEqual(
                    security.hash_password("example-passphrase"), "next-hash"
                )
            with patch(
                "app.security._password_hash.verify",
                side_effect=UnknownHashError("unknown"),
            ):
                self.assertFalse(
                    security.verify_password("example-passphrase", "unknown")
                )
            with patch("app.security._password_hash.verify", return_value=True):
                self.assertTrue(security.verify_password("example-passphrase", "known"))

    def test_invalid_memory_budgets_fail_before_password_work(self) -> None:
        for configured in ("0", "-1", "9", "1.5", "unbounded"):
            with self.subTest(configured=configured):
                with (
                    patch.dict(os.environ, {"PASSWORD_HASH_CONCURRENCY": configured}),
                    patch("app.security._password_hash.hash") as operation,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "PASSWORD_HASH_CONCURRENCY"
                    ):
                        security.hash_password("example-passphrase")
                operation.assert_not_called()


class PasswordHashCapacityHTTPTests(PostgreSQLTestCase):
    def test_register_and_unknown_login_return_503_when_shared_hash_capacity_is_full(
        self,
    ) -> None:
        release = Event()
        both_admitted = Event()
        lock = Lock()
        active = 0

        def occupied_hash(password: str) -> str:
            nonlocal active
            with lock:
                active += 1
                if active == 2:
                    both_admitted.set()
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("Test password work was not released.")
            finally:
                with lock:
                    active -= 1
            return "test-only-hash"

        with patch.dict(os.environ, {"PASSWORD_HASH_CONCURRENCY": "2"}):
            with TestClient(app, raise_server_exceptions=False) as client:
                with patch(
                    "app.security._password_hash.hash", side_effect=occupied_hash
                ):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = [
                            executor.submit(
                                security.hash_password, "example-passphrase"
                            )
                            for _ in range(2)
                        ]
                        try:
                            self.assertTrue(both_admitted.wait(timeout=5))
                            register = client.post(
                                "/auth/register",
                                json={
                                    "email": "new@example.com",
                                    "display_name": "New",
                                    "password": "example-passphrase",
                                },
                            )
                            unknown_login = client.post(
                                "/auth/login",
                                json={
                                    "email": "unknown@example.com",
                                    "password": "example-passphrase",
                                },
                            )
                            for response in (register, unknown_login):
                                self.assertEqual(
                                    response.status_code, 503, response.text
                                )
                                self.assertEqual(
                                    response.headers["Cache-Control"], "no-store"
                                )
                                self.assertGreaterEqual(
                                    int(response.headers["Retry-After"]), 1
                                )
                                self.assertNotIn("example-passphrase", response.text)
                            with get_session() as session:
                                self.assertEqual(
                                    session.scalar(
                                        select(func.count()).select_from(User)
                                    ),
                                    0,
                                )
                        finally:
                            release.set()
                        for future in futures:
                            self.assertEqual(future.result(timeout=5), "test-only-hash")
