import httpx
import pytest

from gate.decision import Policy
from gate.evidence import EvidenceCollector
from gate.prometheus import Prometheus, PrometheusUnavailable


def prometheus(handler) -> Prometheus:
    return Prometheus("http://prometheus.test", transport=httpx.MockTransport(handler))


def vector(*values):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"n": str(i)}, "value": [1.0, str(v)]} for i, v in enumerate(values)
            ],
        },
    }


def test_returns_labels_and_float_values_and_passes_the_time():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=vector(1.5))

    assert prometheus(handler).vector("up", 123.0) == [({"n": "0"}, 1.5)]
    assert seen == {"query": "up", "time": "123.0"}


def test_nan_samples_are_left_out():
    assert prometheus(lambda r: httpx.Response(200, json=vector("NaN", 2))).vector("x", 1) == [
        ({"n": "1"}, 2.0)
    ]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="down"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"status": "error", "error": "bad query"}),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(
            200, json={"status": "error", "data": {"resultType": "vector", "result": []}}
        ),
        httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        ),
        httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": [{}]}}
        ),
        httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "x"]}]},
            },
        ),
    ],
)
def test_any_answer_it_cannot_use_is_unavailable(response):
    with pytest.raises(PrometheusUnavailable):
        prometheus(lambda r: response).vector("x", 1)


def test_a_connection_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(PrometheusUnavailable):
        prometheus(handler).vector("x", 1)


def test_a_timeout_is_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(PrometheusUnavailable):
        prometheus(handler).vector("x", 1)


def collector(answers: dict, now=1000.0) -> EvidenceCollector:
    def handler(request):
        query = request.url.params["query"]
        assert request.url.params["time"] == str(now)
        for needle, value in answers.items():
            if needle in query:
                return httpx.Response(200, json=vector(*value))
        raise AssertionError(f"unexpected query: {query}")

    return EvidenceCollector(prometheus(handler), "v2", Policy().window, now=lambda: now)


def answers(**changes):
    return {
        "timestamp(up": [995.0, 990.0],
        'up{job="quote-api",track="canary"}': [1, 0],
        **changes,
    }


def test_collects_targets_and_counts_the_oldest_scrape():
    samples = answers(increase=[100])
    evidence = collector(samples).collect()
    assert evidence.canary_targets == 2
    assert evidence.canary_targets_up == 1
    assert evidence.oldest_scrape_age_seconds == 10.0


def test_no_targets_means_no_scrape_age():
    evidence = collector({"timestamp(up": [], 'up{job="quote-api"': [], "increase": []}).collect()
    assert evidence.canary_targets == 0
    assert evidence.oldest_scrape_age_seconds is None
    assert evidence.canary is None and evidence.stable is None


def test_canary_queries_name_the_version_and_stable_queries_do_not():
    queries = []

    def handler(request):
        queries.append(request.url.params["query"])
        return httpx.Response(200, json=vector(7))

    EvidenceCollector(prometheus(handler), "v2", "5m", now=lambda: 1.0).collect()
    canary = [q for q in queries if 'track="canary",version="v2"' in q]
    stable = [q for q in queries if 'track="stable"' in q]
    assert len(canary) == 4 and len(stable) == 4
    assert all("version" not in q for q in stable)
    assert all("[5m]" in q for q in canary + stable)


def test_slow_is_the_count_minus_the_fast_bucket():
    samples = answers(
        **{
            'le="0.3"': [80],
            "duration_seconds_count": [100],
            'code=~"5.."': [3],
            "http_requests_total": [100],
        }
    )
    evidence = collector(samples).collect()
    assert evidence.canary.requests == 100
    assert evidence.canary.errors == 3
    assert evidence.canary.slow == 20


def test_no_error_series_counts_as_zero_errors():
    def handler(request):
        query = request.url.params["query"]
        return httpx.Response(200, json=vector() if "5.." in query else vector(10))

    assert EvidenceCollector(prometheus(handler), "v2", "5m").collect().canary.errors == 0


@pytest.mark.parametrize("version", ['v1"} or up{a="', "", "a b", "x" * 129, "v1\n"])
def test_a_version_that_could_change_the_query_is_refused(version):
    with pytest.raises(ValueError):
        EvidenceCollector(prometheus(lambda r: httpx.Response(200)), version, "5m")
