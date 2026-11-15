"""Password hashing and opaque session token helpers."""

import hashlib
import secrets

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

_password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = _password_hash.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, encoded_password: str) -> bool:
    """Treat unsupported or malformed stored hashes as invalid credentials."""
    try:
        return _password_hash.verify(password, encoded_password)
    except UnknownHashError:
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
