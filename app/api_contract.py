"""Shared response models and metadata for the public HTTP contract."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    """A safe error message without database, credential, or traceback details."""

    model_config = ConfigDict(extra="forbid")

    detail: str = Field(description="A human-readable explanation of the error.")


class ValidationIssue(BaseModel):
    """One validation failure with submitted values and context omitted."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="The validation error identifier, such as 'missing'.")
    loc: list[str | int] = Field(
        description="The request location and field path, including array indexes."
    )
    msg: str = Field(
        description="A human-readable explanation of the validation failure."
    )


class ValidationErrorResponse(BaseModel):
    """The sanitized validation response returned for invalid requests."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "detail": [
                        {
                            "type": "missing",
                            "loc": ["body", "title"],
                            "msg": "Field required",
                        }
                    ]
                }
            ]
        },
    )

    detail: list[ValidationIssue] = Field(
        description="Validation failures without raw submitted input or validator context."
    )


class HealthResponse(BaseModel):
    """Process liveness; database readiness is not checked by this endpoint."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"status": "ok"}]}
    )

    status: Literal["ok"] = Field(description="The application process is responding.")


class ReadinessResponse(BaseModel):
    """The database is reachable and at the application's expected schema revision."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = Field(
        description="Database connectivity and schema checks passed."
    )


def _response_headers(
    status_code: int, *, private: bool, location: bool = False
) -> dict[str, dict[str, Any]]:
    headers: dict[str, dict[str, Any]] = {}
    if private:
        headers["Cache-Control"] = {
            "description": "Private responses must not be stored by clients or caches.",
            "schema": {"type": "string", "const": "no-store"},
            "example": "no-store",
        }
    if status_code == HTTPStatus.UNAUTHORIZED:
        headers["WWW-Authenticate"] = {
            "description": "The authentication scheme required for this request.",
            "schema": {"type": "string", "const": "Bearer"},
            "example": "Bearer",
        }
    if status_code in {HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.SERVICE_UNAVAILABLE}:
        headers["Retry-After"] = {
            "description": "Seconds to wait before retrying an operation whose outcome is known.",
            "schema": {"type": "string", "pattern": "^[1-9][0-9]*$"},
            "example": "1",
        }
    if location:
        headers["Location"] = {
            "description": "URI reference identifying the newly created resource.",
            "schema": {"type": "string", "format": "uri-reference"},
        }
    return headers


def api_responses(
    status_code: int = 200,
    *,
    errors: Mapping[int, str] | None = None,
    validation: bool = False,
    location: bool = False,
    private: bool = True,
) -> dict[int | str, dict[str, Any]]:
    """Describe statuses and headers while routes supply their success models.

    Every operation documents a safe JSON server error. A route that accepts
    validated input additionally documents the same sanitized 422 model used by
    the exception handler. This helper describes the wire contract; routes and
    middleware remain responsible for returning the documented headers.
    """
    if location and status_code != HTTPStatus.CREATED:
        raise ValueError("Location is documented only for a 201 creation response.")

    descriptions: dict[int, str] = {
        HTTPStatus.OK: "The request completed successfully.",
        HTTPStatus.CREATED: "The resource was created successfully.",
        HTTPStatus.NO_CONTENT: "The operation completed successfully with an empty response body.",
    }
    responses: dict[int | str, dict[str, Any]] = {
        status_code: {
            "description": descriptions.get(
                status_code, HTTPStatus(status_code).phrase
            ),
            "headers": _response_headers(
                status_code, private=private, location=location
            ),
        }
    }
    error_descriptions = {
        HTTPStatus.SERVICE_UNAVAILABLE: "A database resource is temporarily unavailable; retry only when safe.",
        HTTPStatus.INTERNAL_SERVER_ERROR: (
            "An unexpected server error occurred. Internal details are not exposed."
        ),
        **(errors or {}),
    }
    for error_code, description in error_descriptions.items():
        responses[error_code] = {
            "model": ErrorResponse,
            "description": description,
            "headers": _response_headers(error_code, private=private),
        }
        exact_examples = {
            HTTPStatus.UNAUTHORIZED: {"detail": "Invalid authentication credentials"},
            HTTPStatus.INTERNAL_SERVER_ERROR: {"detail": "Internal server error"},
            HTTPStatus.SERVICE_UNAVAILABLE: {
                "detail": "Service temporarily unavailable"
            },
        }
        if error_code in exact_examples:
            responses[error_code]["content"] = {
                "application/json": {"example": exact_examples[error_code]}
            }
    if validation:
        responses[HTTPStatus.UNPROCESSABLE_ENTITY] = {
            "model": ValidationErrorResponse,
            "description": (
                "Request validation failed. Each issue contains only type, loc, and msg; "
                "submitted values and validator context are omitted."
            ),
            "headers": _response_headers(422, private=private),
        }
    return responses


OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "auth",
        "description": (
            "Register accounts, create one-hour opaque bearer sessions, inspect the "
            "current account, and revoke the current session."
        ),
    },
    {
        "name": "teams",
        "description": (
            "Create teams and manage membership. Owners control membership changes; "
            "current members may view their team's roster."
        ),
    },
    {
        "name": "notes",
        "description": (
            "Work with private and team notes. Access is evaluated against ownership "
            "and current team membership for every request."
        ),
    },
    {
        "name": "sharing",
        "description": (
            "Manage read-only team shares of notes. Private owners and source-team "
            "owners control shares; recipient teams receive read access only."
        ),
    },
    {
        "name": "health",
        "description": "Check process liveness and database/schema readiness.",
    },
]

API_DESCRIPTION = """A JSON API for accounts, teams, and notes.

## HTTP conventions

Resource collections use plural nouns. Successful reads return `200`; creation
operations return `201`; successful operations documented as `204` return no
response body. Login returns `200` with a newly created bearer session. Where a
creation response documents `Location`, that header identifies the new resource.
Identifiers are positive PostgreSQL BIGINT values. Timestamps use RFC 3339
date-time strings with a timezone.

## Authentication and privacy

Registration, login, liveness and readiness checks are public. Other operations require
`Authorization: Bearer <access_token>`. Obtain the opaque token from `/auth/login`;
it is not a JWT. Sessions expire after one hour, and logout revokes the current
token immediately. Private responses, including their errors, carry
`Cache-Control: no-store`. Authentication failures return `401` with
`WWW-Authenticate: Bearer`. An authenticated caller without the required role
receives `403`; inaccessible resources may return `404` to protect their existence.

## Collections

Paginated collections accept `limit` (default `50`, minimum `1`, maximum `100`)
and `offset` (minimum `0`). Access filtering happens before pagination, and each
operation documents its stable ordering. Empty collections and pages return an
empty JSON array, not `404`.

## Errors and validation

Application errors use `{"detail": "A human-readable explanation"}`. Status `409`
indicates a resource conflict. Invalid request fields, unknown body fields, or
out-of-range parameters return `422` with `detail` as an array of validation
issues. Each issue contains only `type`, `loc`, and `msg`; raw submitted values
and validator context are omitted. Unexpected server failures return a generic
`500` JSON error without internal implementation details. Endpoint response
definitions describe the applicable status codes, bodies, and headers.
Login is throttled with shared database counters; excess attempts return `429`
with `Retry-After`. Temporary database resource exhaustion returns `503` with
`Retry-After`. `/health` reports process liveness, while `/ready` checks the
configured database and its migration revision.
"""
