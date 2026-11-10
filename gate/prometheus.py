"""The part of the Prometheus HTTP API the gate uses. Any doubt is an exception."""

import math

import httpx


class PrometheusUnavailable(Exception):
    pass


class Prometheus:
    def __init__(self, base_url: str, timeout: float = 5.0, transport=None):
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def vector(self, promql: str, at: float) -> list[tuple[dict, float]]:
        """Instant query. Returns (labels, value), leaving out NaN samples."""
        try:
            response = self._client.get("/api/v1/query", params={"query": promql, "time": at})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PrometheusUnavailable(str(exc) or type(exc).__name__) from exc

        if not isinstance(body, dict) or body.get("status") != "success":
            raise PrometheusUnavailable(f"query was not successful: {str(body)[:200]}")
        data = body.get("data") or {}
        if data.get("resultType") != "vector":
            raise PrometheusUnavailable(f"expected a vector, got {data.get('resultType')}")

        samples = []
        try:
            for item in data["result"]:
                value = float(item["value"][1])
                if not math.isnan(value):
                    samples.append((item["metric"], value))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PrometheusUnavailable(f"malformed sample: {exc}") from exc
        return samples
