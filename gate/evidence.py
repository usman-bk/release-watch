"""Asks Prometheus what a release has actually done."""

import re
import time
from collections.abc import Callable

from gate.decision import Evidence, Sample
from gate.prometheus import Prometheus

VERSION_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
SLOW_ABOVE_SECONDS = "0.3"


class EvidenceCollector:
    def __init__(
        self,
        prometheus: Prometheus,
        version: str,
        window: str,
        job: str = "quote-api",
        route: str = "/api/quote",
        now: Callable[[], float] = time.time,
    ):
        if not VERSION_PATTERN.fullmatch(version):
            raise ValueError(f"not a usable version label: {version!r}")
        self._prometheus = prometheus
        self._version = version
        self._window = window
        self._job = job
        self._route = route
        self._now = now

    def collect(self) -> Evidence:
        # One timestamp for every query, so the numbers describe the same moment.
        at = self._now()
        target = f'job="{self._job}",track="canary"'
        ups = self._prometheus.vector(f"up{{{target}}}", at)
        stamps = self._prometheus.vector(f"timestamp(up{{{target}}})", at)
        return Evidence(
            canary=self._sample(f'track="canary",version="{self._version}"', at),
            stable=self._sample('track="stable"', at),
            canary_targets=len(ups),
            canary_targets_up=sum(1 for _, value in ups if value == 1),
            oldest_scrape_age_seconds=at - min(v for _, v in stamps) if stamps else None,
        )

    def _sample(self, selector: str, at: float) -> Sample | None:
        base = f'job="{self._job}",route="{self._route}",{selector}'
        window = self._window
        requests = self._sum(f"increase(http_requests_total{{{base}}}[{window}])", at)
        if requests is None:
            return None
        errors = self._sum(f'increase(http_requests_total{{{base},code=~"5.."}}[{window}])', at)
        fast = self._sum(
            f"increase(http_request_duration_seconds_bucket"
            f'{{{base},le="{SLOW_ABOVE_SECONDS}"}}[{window}])',
            at,
        )
        counted = self._sum(
            f"increase(http_request_duration_seconds_count{{{base}}}[{window}])", at
        )
        slow = max((counted or 0.0) - (fast or 0.0), 0.0)
        return Sample(requests=requests, errors=errors or 0.0, slow=slow)

    def _sum(self, expression: str, at: float) -> float | None:
        result = self._prometheus.vector(f"sum({expression})", at)
        return result[0][1] if result else None
