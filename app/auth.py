"""Account registration and revocable, database-backed bearer sessions."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.api_contract import api_responses
from app.database import get_session
from app.models import AuthSession, User
from app.rate_limit import try_consume_login_attempt
from app.security import (
    DUMMY_PASSWORD_HASH,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.validation import PasswordText, PostgresText

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    bearerFormat="opaque",
    description=(
        "Obtain an opaque token from POST /auth/login and send it as "
        "Authorization: Bearer <access_token>. Sessions expire after one hour. "
        "POST /auth/logout revokes the current token immediately. Tokens are not JWTs."
    ),
)


class EmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(
        description="Email address, stripped and normalized to lowercase.",
        examples=["reader@example.com"],
    )

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class RegisterRequest(EmailRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "reader@example.com",
                    "display_name": "Sample User",
                    "password": "review-only-passphrase",
                }
            ]
        },
    )

    display_name: Annotated[
        PostgresText,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        Field(description="Public name; leading and trailing whitespace is removed."),
    ]
    password: PasswordText = Field(
        min_length=12,
        max_length=128,
        description="Password to hash; whitespace is preserved. Never returned in responses.",
    )


class LoginRequest(EmailRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "reader@example.com",
                    "password": "review-only-passphrase",
                }
            ]
        },
    )

    password: PasswordText = Field(
        min_length=1,
        max_length=128,
        description="Account password; whitespace is preserved.",
    )


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Server-assigned account ID.", examples=[1])
    email: str = Field(description="Normalized account email address.")
    display_name: str = Field(description="Public display name.")
    created_at: datetime = Field(
        description="Account creation time, with a UTC offset."
    )


class TokenResponse(BaseModel):
    access_token: str = Field(
        description="Opaque session token. Send it in the Authorization header; keep it private."
    )
    token_type: Literal["bearer"] = Field(
        default="bearer", description="Authorization scheme."
    )
    expires_at: datetime = Field(description="Session expiry time, with a UTC offset.")


def invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_session_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not credentials.credentials
        or len(credentials.credentials) > 256
    ):
        raise invalid_credentials()
    return credentials.credentials


def get_current_user(
    token: Annotated[str, Depends(get_session_token)],
) -> UserResponse:
    with get_session() as session:
        user = session.scalar(
            select(User)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.token_hash == hash_token(token),
                AuthSession.expires_at > func.now(),
            )
        )
        if user is None:
            raise invalid_credentials()
        return UserResponse.model_validate(user)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=201,
    operation_id="register_user",
    summary="Register an account",
    description=(
        "Create an account and return public user fields. Registration does not sign "
        "the user in; call POST /auth/login to create a session. Email uniqueness is "
        "case-insensitive. Unknown fields are rejected."
    ),
    responses=api_responses(
        201, errors={409: "An account with this email already exists."}, validation=True
    ),
)
def register(payload: RegisterRequest) -> UserResponse:
    encoded_password = hash_password(payload.password.get_secret_value())
    try:
        with get_session() as session:
            user = User(
                email=str(payload.email),
                display_name=payload.display_name,
                password_hash=encoded_password,
            )
            session.add(user)
            session.flush()
            result = UserResponse.model_validate(user)
    except IntegrityError as error:
        if (
            not isinstance(error.orig, UniqueViolation)
            or error.orig.diag.constraint_name != "users_email_key"
        ):
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from None
    return result


@router.post(
    "/login",
    response_model=TokenResponse,
    operation_id="create_session",
    summary="Sign in and create a session",
    description=(
        "Return a one-hour opaque bearer token. Unknown email addresses and incorrect "
        "passwords return the same generic 401 response. The token is returned only "
        "here and is stored as a hash. Account and source-address budgets limit "
        "password verification attempts. A 429 response supplies Retry-After in seconds."
    ),
    responses=api_responses(
        errors={
            401: "Invalid authentication credentials.",
            429: "The account or source-address login budget is exhausted. Retry after the documented delay.",
        },
        validation=True,
    ),
)
def login(payload: LoginRequest, request: Request) -> TokenResponse:
    # Trust only the ASGI peer selected by the server proxy configuration.
    client_host = request.client.host if request.client is not None else "unknown"
    try_consume_login_attempt(str(payload.email), client_host)
    with get_session() as session:
        user = session.scalar(
            select(User).where(func.lower(User.email) == str(payload.email))
        )

    valid_password = verify_password(
        payload.password.get_secret_value(),
        user.password_hash if user else DUMMY_PASSWORD_HASH,
    )
    if user is None or not valid_password:
        raise invalid_credentials()

    token = generate_token()
    with get_session() as session:
        expires_at = session.scalar(select(func.now()))
        if expires_at is None:
            raise RuntimeError("The database did not return the current time.")
        auth_session = AuthSession(
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=expires_at + timedelta(hours=1),
        )
        session.add(auth_session)
        session.flush()
        return TokenResponse(access_token=token, expires_at=auth_session.expires_at)


@router.get(
    "/me",
    response_model=UserResponse,
    operation_id="get_current_user",
    summary="Get the signed-in account",
    description="Return public fields for the account associated with the current valid session.",
    responses=api_responses(
        errors={401: "Missing, invalid, expired, or revoked bearer token."}
    ),
)
def me(user: Annotated[UserResponse, Depends(get_current_user)]) -> UserResponse:
    return user


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    operation_id="delete_current_session",
    summary="Revoke the current session",
    description=(
        "Revoke only the supplied bearer token. Other sessions remain valid. "
        "Success returns an empty 204 response. A token already revoked returns 401."
    ),
    responses=api_responses(
        204, errors={401: "Missing, invalid, expired, or revoked bearer token."}
    ),
)
def logout(
    user: Annotated[UserResponse, Depends(get_current_user)],
    token: Annotated[str, Depends(get_session_token)],
) -> Response:
    with get_session() as session:
        session.execute(
            delete(AuthSession).where(
                AuthSession.token_hash == hash_token(token),
                AuthSession.user_id == user.id,
            )
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
