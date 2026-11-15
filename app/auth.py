"""Account registration and revocable, database-backed bearer sessions."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.errors import UniqueViolation
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.database import get_session
from app.models import AuthSession, User
from app.security import (
    DUMMY_PASSWORD_HASH,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class EmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class RegisterRequest(EmailRequest):
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    password: SecretStr = Field(min_length=12, max_length=128)


class LoginRequest(EmailRequest):
    password: SecretStr = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


def invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_session_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer)
    ],
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


@router.post("/register", response_model=UserResponse, status_code=201)
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


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    with get_session() as session:
        user = session.scalar(select(User).where(func.lower(User.email) == str(payload.email)))

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


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[UserResponse, Depends(get_current_user)]) -> UserResponse:
    return user


@router.post("/logout", status_code=204, response_class=Response)
def logout(
    user: Annotated[UserResponse, Depends(get_current_user)],
    token: Annotated[str, Depends(get_session_token)],
) -> Response:
    with get_session() as session:
        session.execute(
            delete(AuthSession).where(
                AuthSession.token_hash == hash_token(token), AuthSession.user_id == user.id
            )
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
