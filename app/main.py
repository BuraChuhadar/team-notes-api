from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

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


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
