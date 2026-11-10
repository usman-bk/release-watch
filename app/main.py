"""A small price quote API, instrumented for the error budget and the release gate.

The demo settings make one process behave like a bad release. Nothing else in the
service knows about them.
"""

import asyncio
import os
import random
import time
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

QUOTE_ROUTE = "/api/quote"
UNIT_PRICE_CENTS = 1250
BULK_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 15

# 0.3 is the latency objective, so it has to be a bucket edge rather than an estimate.
LATENCY_BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0)

# Requests to these do not spend error budget.
UNMEASURED_ROUTES = {"/metrics", "/healthz"}


@dataclass(frozen=True)
class Settings:
    version: str = "dev"
    demo_error_ratio: float = 0.0
    demo_delay_ms: int = 0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            version=os.environ.get("APP_VERSION", "dev"),
            demo_error_ratio=float(os.environ.get("DEMO_ERROR_RATIO", "0")),
            demo_delay_ms=int(os.environ.get("DEMO_DELAY_MS", "0")),
        )


def quote_total_cents(units: int) -> int:
    total = units * UNIT_PRICE_CENTS
    if units >= BULK_THRESHOLD:
        total -= total * BULK_DISCOUNT_PERCENT // 100
    return total


def create_app(settings: Settings | None = None, rng: random.Random | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    rng = rng or random.Random()
    registry = CollectorRegistry()

    requests_total = Counter(
        "http_requests",
        "Requests served, by route and status.",
        ["route", "code", "version"],
        registry=registry,
    )
    duration = Histogram(
        "http_request_duration_seconds",
        "Time to serve a request.",
        ["route", "version"],
        buckets=LATENCY_BUCKETS,
        registry=registry,
    )

    # A counter that first appears at 1 has no earlier sample, so increase() misses that
    # first error. Publishing zeros at start up means a release's first failures count.
    for code in ("200", "500", "503"):
        requests_total.labels(QUOTE_ROUTE, code, settings.version)
    duration.labels(QUOTE_ROUTE, settings.version)

    app = FastAPI(title="quote-api", docs_url=None, redoc_url=None, openapi_url=None)

    def record(request: Request, code: int, started: float) -> None:
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        if path in UNMEASURED_ROUTES:
            return
        requests_total.labels(path, str(code), settings.version).inc()
        duration.labels(path, settings.version).observe(time.perf_counter() - started)

    @app.middleware("http")
    async def measure(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            record(request, 500, started)
            raise
        record(request, response.status_code, started)
        return response

    @app.get(QUOTE_ROUTE)
    async def quote(units: int = Query(ge=1, le=1000)) -> dict:
        if settings.demo_delay_ms:
            await asyncio.sleep(settings.demo_delay_ms / 1000)
        if rng.random() < settings.demo_error_ratio:
            raise HTTPException(status_code=503, detail="pricing unavailable")
        return {
            "units": units,
            "total_cents": quote_total_cents(units),
            "version": settings.version,
        }

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "version": settings.version}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
