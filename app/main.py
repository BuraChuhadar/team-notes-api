from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth import router as auth_router
from app.database import get_engine, init_database


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    engine = get_engine()
    try:
        await run_in_threadpool(init_database)
        yield
    finally:
        await run_in_threadpool(engine.dispose)


app = FastAPI(
    title="Notes API",
    description="A REST service for team notes, built one step at a time.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(auth_router)


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
async def prevent_auth_caching(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    if request.url.path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
