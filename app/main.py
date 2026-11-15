from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
import hmac
import os

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError as PoolTimeout
from fastapi.routing import iter_route_contexts
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from app.api_contract import (
    API_DESCRIPTION,
    OPENAPI_TAGS,
    HealthResponse,
    ReadinessResponse,
    api_responses,
)
from app.auth import router as auth_router
from app.config import get_settings
from app.database import check_database_ready, get_engine, init_database
from app.observability import MetricsMiddleware, render_metrics
from app.notes import router as notes_router
from app.security import PasswordHashCapacityError
from app.sharing import router as sharing_router
from app.teams import router as teams_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    engine = get_engine()
    try:
        if get_settings().auto_migrate:
            await run_in_threadpool(init_database)
        else:
            await run_in_threadpool(check_database_ready)
        yield
    finally:
        await run_in_threadpool(engine.dispose)


app = FastAPI(
    title="Notes API",
    description=API_DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
    swagger_ui_parameters={"displayRequestDuration": True},
)
app.add_middleware(MetricsMiddleware)
app.include_router(auth_router)
app.include_router(notes_router)
app.include_router(sharing_router)
app.include_router(teams_router)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(
    request: Request, exception: StarletteHTTPException
) -> Response:
    response = await http_exception_handler(request, exception)
    if exception.status_code == 405:
        # The router reports only its first partial match; include every method
        # supported by this resource while preserving the standard error response.
        allowed_methods = {
            method.strip()
            for method in response.headers.get("Allow", "").split(",")
            if method.strip()
        }
        for route in iter_route_contexts(app.routes):
            match, _ = route.matches(request.scope)
            methods = getattr(route, "methods", None)
            if match in {Match.PARTIAL, Match.FULL} and methods:
                allowed_methods.update(methods)
        if allowed_methods:
            response.headers["Allow"] = ", ".join(sorted(allowed_methods))
    return response


@app.exception_handler(PoolTimeout)
@app.exception_handler(OperationalError)
async def database_resource_error(
    request: Request, exception: PoolTimeout | OperationalError
) -> JSONResponse:
    sqlstate = getattr(getattr(exception, "orig", None), "sqlstate", "") or ""
    transient = isinstance(exception, PoolTimeout) or (
        isinstance(exception, OperationalError)
        and (
            exception.connection_invalidated
            or not sqlstate
            or sqlstate.startswith("08")
            or sqlstate in {"40001", "40P01", "55P03", "57014", "57P01", "53300"}
        )
    )
    if not transient:
        raise exception
    return JSONResponse(
        status_code=503,
        content={"detail": "Service temporarily unavailable"},
        headers={"Cache-Control": "no-store", "Retry-After": "1"},
    )


@app.exception_handler(PasswordHashCapacityError)
async def password_hash_capacity_error(
    request: Request, exception: PasswordHashCapacityError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Service temporarily unavailable"},
        headers={"Cache-Control": "no-store", "Retry-After": "1"},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(
    request: Request, exception: Exception
) -> JSONResponse:
    # Server-side failures must not expose database details or credentials.
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exception: RequestValidationError
) -> JSONResponse:
    # Validation errors must never echo submitted passwords or other input values.
    errors = [
        {key: error[key] for key in ("type", "loc", "msg")}
        for error in exception.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.middleware("http")
async def prevent_private_caching(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    if request.url.path.startswith(
        ("/auth/", "/notes/", "/teams/")
    ) or request.url.path in {"/notes", "/teams"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get(
    "/health",
    tags=["health"],
    response_model=HealthResponse,
    operation_id="health_check",
    summary="Check process liveness",
    description=(
        "Returns the API process status. This endpoint is public and does not "
        "check database connectivity or readiness."
    ),
    responses=api_responses(private=False),
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get(
    "/ready",
    tags=["health"],
    response_model=ReadinessResponse,
    operation_id="readiness_check",
    summary="Check database and schema readiness",
    description="Check database connectivity and the expected Alembic revision. Returns 503 when the instance should not receive traffic.",
    responses=api_responses(
        errors={
            503: "The database is unreachable or not at the expected schema revision."
        }
    ),
)
async def readiness() -> ReadinessResponse | JSONResponse:
    try:
        await run_in_threadpool(check_database_ready)
    except (RuntimeError, SQLAlchemyError):
        return JSONResponse(
            status_code=503,
            content={"detail": "Service temporarily unavailable"},
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
        )
    return JSONResponse(content={"status": "ok"}, headers={"Cache-Control": "no-store"})


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    expected = os.environ.get("METRICS_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if expected and not hmac.compare_digest(
        supplied.encode("utf-8"), ("Bearer " + expected).encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    return Response(
        content=render_metrics(),
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )
