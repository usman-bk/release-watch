import random

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app, quote_total_cents


def client(**settings) -> TestClient:
    return TestClient(create_app(Settings(version="v1", **settings)), raise_server_exceptions=False)


def metric(c: TestClient, needle: str) -> float:
    for line in c.get("/metrics").text.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{needle} not exported")


def line(route: str, code: str) -> str:
    return f'http_requests_total{{code="{code}",route="{route}",version="v1"}}'


@pytest.mark.parametrize(
    ("units", "cents"), [(1, 1250), (9, 11250), (10, 10625), (11, 11688), (1000, 1062500)]
)
def test_quote_prices_units_and_applies_the_bulk_discount_from_ten(units, cents):
    assert quote_total_cents(units) == cents


def test_quote_returns_the_total_and_version():
    body = client().get("/api/quote", params={"units": 2}).json()
    assert body == {"units": 2, "total_cents": 2500, "version": "v1"}


def test_error_series_exist_at_zero_before_any_request():
    c = client()
    assert metric(c, line("/api/quote", "503")) == 0
    assert metric(c, line("/api/quote", "500")) == 0
    assert metric(c, line("/api/quote", "200")) == 0


def test_a_served_request_counts_once_under_its_route_and_status():
    c = client()
    c.get("/api/quote", params={"units": 1})
    assert metric(c, line("/api/quote", "200")) == 1


def test_a_refused_request_is_not_a_server_error():
    c = client()
    assert c.get("/api/quote", params={"units": 0}).status_code == 422
    assert metric(c, line("/api/quote", "422")) == 1
    assert metric(c, line("/api/quote", "500")) == 0


def test_unknown_paths_share_one_label_so_scanners_cannot_grow_the_series():
    c = client()
    assert c.get("/wp-admin").status_code == 404
    assert c.get("/.env").status_code == 404
    assert metric(c, line("unmatched", "404")) == 2
    assert "wp-admin" not in c.get("/metrics").text


def test_health_and_metrics_do_not_spend_error_budget():
    c = client()
    c.get("/healthz")
    c.get("/metrics")
    text = c.get("/metrics").text
    assert "/healthz" not in text
    assert 'route="/metrics"' not in text


def test_demo_error_ratio_answers_503_and_is_counted():
    c = client(demo_error_ratio=1.0)
    assert c.get("/api/quote", params={"units": 1}).status_code == 503
    assert metric(c, line("/api/quote", "503")) == 1


def test_an_unhandled_exception_is_counted_as_500():
    app = create_app(Settings(version="v1"), rng=_Exploding())
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/quote", params={"units": 1}).status_code == 500
    assert metric(c, line("/api/quote", "500")) == 1


def test_latency_has_a_bucket_exactly_at_the_objective():
    text = client().get("/metrics").text
    assert 'http_request_duration_seconds_bucket{le="0.3",route="/api/quote",version="v1"}' in text


def test_a_slow_request_lands_above_the_objective():
    c = client(demo_delay_ms=320)
    c.get("/api/quote", params={"units": 1})
    fast = metric(
        c, 'http_request_duration_seconds_bucket{le="0.3",route="/api/quote",version="v1"}'
    )
    assert fast == 0
    assert metric(c, 'http_request_duration_seconds_count{route="/api/quote",version="v1"}') == 1


class _Exploding(random.Random):
    def random(self) -> float:
        raise RuntimeError("boom")
