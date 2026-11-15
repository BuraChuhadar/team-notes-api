"""Per-process Prometheus metrics with bounded, credential-free labels."""

from time import perf_counter

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import GCCollector, PlatformCollector, ProcessCollector
from sqlalchemy.pool import QueuePool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.database import get_engine

REGISTRY = CollectorRegistry()
GCCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)
ProcessCollector(registry=REGISTRY)
REQUESTS = Counter(
    "notes_http_requests_total",
    "Completed HTTP requests.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
DURATION = Histogram(
    "notes_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
POOL_CHECKED_OUT = Gauge(
    "notes_db_pool_checked_out",
    "Connections currently checked out in this API process.",
    registry=REGISTRY,
)
POOL_SIZE = Gauge(
    "notes_db_pool_size",
    "Configured persistent connection pool size in this process.",
    registry=REGISTRY,
)


def pool_value(*, checked_out: bool) -> int:
    try:
        pool = get_engine().pool
    except (RuntimeError, ValueError):
        return 0
    if not isinstance(pool, QueuePool):
        return 0
    return pool.checkedout() if checked_out else pool.size()


POOL_CHECKED_OUT.set_function(lambda: pool_value(checked_out=True))
POOL_SIZE.set_function(lambda: pool_value(checked_out=False))


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        status = 500

        async def measured_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            # Route templates bound cardinality; raw paths, user IDs, query strings,
            # tokens, and client addresses must never become metric labels.
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope.get("method", "OTHER")
            if method not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
                "TRACE",
            }:
                method = "OTHER"
            REQUESTS.labels(method, route, str(status)).inc()
            DURATION.labels(method, route).observe(perf_counter() - started)
