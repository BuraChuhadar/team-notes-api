"""Password hashing and opaque session token helpers."""

import hashlib
import os
import secrets
from collections.abc import Generator
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

_password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = _password_hash.hash(secrets.token_urlsafe(32))
_hash_gates: dict[int, BoundedSemaphore] = {}
_hash_gates_lock = Lock()


class PasswordHashCapacityError(RuntimeError):
    """The process has no free password-hashing memory/CPU admission slot."""


def _password_hash_concurrency() -> int:
    try:
        value = int(os.environ.get("PASSWORD_HASH_CONCURRENCY", "2"))
    except ValueError:
        raise RuntimeError(
            "PASSWORD_HASH_CONCURRENCY must be an integer between 1 and 8."
        ) from None
    if not 1 <= value <= 8:
        raise RuntimeError(
            "PASSWORD_HASH_CONCURRENCY must be an integer between 1 and 8."
        )
    return value


def _password_hash_gate(concurrency: int) -> BoundedSemaphore:
    # Each worker process has its own memory budget. Configure its capacity at
    # deployment time; changing the environment requires a process restart.
    # Guard first creation too: concurrent cache misses must not create separate
    # semaphores that each admit the full process budget.
    with _hash_gates_lock:
        gate = _hash_gates.get(concurrency)
        if gate is None:
            gate = BoundedSemaphore(concurrency)
            _hash_gates[concurrency] = gate
        return gate


@contextmanager
def _password_hash_slot() -> Generator[None, None, None]:
    gate = _password_hash_gate(_password_hash_concurrency())
    if not gate.acquire(blocking=False):
        raise PasswordHashCapacityError(
            "Password hashing capacity is temporarily unavailable."
        )
    try:
        yield
    finally:
        gate.release()


def hash_password(password: str) -> str:
    with _password_hash_slot():
        return _password_hash.hash(password)


def verify_password(password: str, encoded_password: str) -> bool:
    """Treat unsupported or malformed stored hashes as invalid credentials."""
    with _password_hash_slot():
        try:
            return _password_hash.verify(password, encoded_password)
        except UnknownHashError:
            return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
