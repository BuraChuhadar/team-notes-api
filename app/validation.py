"""Request text constraints shared by PostgreSQL fields and password inputs."""

from typing import Annotated

from pydantic import AfterValidator, SecretStr, StringConstraints


PostgresText = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]


def _validate_password_utf8(password: SecretStr) -> SecretStr:
    # SecretStr preserves input verbatim, including NUL. Argon2 accepts NUL but
    # cannot encode lone Unicode surrogates; reject those before password work.
    try:
        password.get_secret_value().encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Password must contain valid Unicode text.") from None
    return password


PasswordText = Annotated[SecretStr, AfterValidator(_validate_password_utf8)]
