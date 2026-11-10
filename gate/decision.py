"""Turns what Prometheus measured into promote, hold or roll back.

Pure functions, so every branch can be tested without a clock or a network.
"""

from dataclasses import dataclass
from enum import Enum
from math import sqrt

CONFIDENCE_Z = 1.96


class Verdict(str, Enum):
    PROMOTE = "promote"
    HOLD = "hold"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class Sample:
    requests: float
    errors: float
    slow: float

    @property
    def error_ratio(self) -> float:
        return _ratio(self.errors, self.requests)

    @property
    def slow_ratio(self) -> float:
        return _ratio(self.slow, self.requests)


@dataclass(frozen=True)
class Evidence:
    canary: Sample | None
    stable: Sample | None
    canary_targets: int
    canary_targets_up: int
    oldest_scrape_age_seconds: float | None


@dataclass(frozen=True)
class Policy:
    window: str = "5m"
    # Below this a ratio is mostly noise, so neither good nor bad news counts.
    min_requests: float = 200
    # How much worse than stable the canary may be.
    max_extra_error_ratio: float = 0.005
    max_extra_slow_ratio: float = 0.02
    # A stable track worse than this cannot serve as a baseline.
    max_baseline_error_ratio: float = 0.01
    max_baseline_slow_ratio: float = 0.05
    max_scrape_age_seconds: float = 45
    steps: tuple[int, ...] = (10, 25, 50)
    bake_seconds: float = 300
    poll_seconds: float = 30
    step_deadline_seconds: float = 900


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


def _ratio(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return min(max(part, 0.0), whole) / whole


def wilson_bounds(bad: float, total: float) -> tuple[float, float]:
    """95% interval for a ratio. It stays sensible at zero failures, unlike the normal one."""
    if total <= 0:
        return 0.0, 1.0
    bad = min(max(bad, 0.0), total)
    observed = bad / total
    z2 = CONFIDENCE_Z**2
    denominator = 1 + z2 / total
    centre = observed + z2 / (2 * total)
    margin = CONFIDENCE_Z * sqrt(observed * (1 - observed) / total + z2 / (4 * total**2))
    return max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator)


def decide(evidence: Evidence, policy: Policy) -> Decision:
    def hold(reason: str) -> Decision:
        return Decision(Verdict.HOLD, reason)

    if evidence.canary_targets == 0:
        return hold("no canary target is being scraped")
    if evidence.canary_targets_up < evidence.canary_targets:
        up, all_targets = evidence.canary_targets_up, evidence.canary_targets
        return hold(f"only {up} of {all_targets} canary targets are up")
    age = evidence.oldest_scrape_age_seconds
    if age is None or age > policy.max_scrape_age_seconds:
        return hold("the newest measurements are too old to trust")

    canary, stable = evidence.canary, evidence.stable
    if canary is None or stable is None:
        return hold("no request data for one of the tracks")
    if stable.requests < policy.min_requests:
        return hold(f"stable has served too few requests ({stable.requests:.0f}) to compare")
    if canary.requests < policy.min_requests:
        return hold(f"canary has served too few requests ({canary.requests:.0f})")

    error_limit = stable.error_ratio + policy.max_extra_error_ratio
    slow_limit = stable.slow_ratio + policy.max_extra_slow_ratio
    error_low, error_high = wilson_bounds(canary.errors, canary.requests)
    slow_low, slow_high = wilson_bounds(canary.slow, canary.requests)

    if error_low > error_limit:
        return Decision(
            Verdict.ROLLBACK,
            f"canary error ratio {canary.error_ratio:.4f} is above the limit {error_limit:.4f}",
        )
    if slow_low > slow_limit:
        return Decision(
            Verdict.ROLLBACK,
            f"canary slow ratio {canary.slow_ratio:.4f} is above the limit {slow_limit:.4f}",
        )

    # Checked after the rollback tests: a canary far worse than a sick stable is still bad,
    # but a canary that merely matches one has shown nothing.
    if stable.error_ratio > policy.max_baseline_error_ratio:
        return hold(f"stable is failing too ({stable.error_ratio:.4f}), so nothing can be judged")
    if stable.slow_ratio > policy.max_baseline_slow_ratio:
        return hold(f"stable is slow too ({stable.slow_ratio:.4f}), so nothing can be judged")

    if error_high < error_limit and slow_high < slow_limit:
        return Decision(Verdict.PROMOTE, "canary is within both limits")
    return hold("not enough requests yet to tell the canary from the limit")
