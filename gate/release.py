"""Walks a release through its traffic steps and leaves it somewhere safe."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from gate.decision import Decision, Evidence, Policy, Verdict, decide
from gate.prometheus import PrometheusUnavailable

ROLLBACK_ATTEMPTS = 4


class Status(str, Enum):
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    # Traffic could not be moved back, so a person has to.
    STUCK = "stuck"


@dataclass(frozen=True)
class Outcome:
    status: Status
    reason: str


class Deployer(Protocol):
    def set_canary_weight(self, percent: int) -> None: ...


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def evaluate(collect: Callable[[], Evidence], policy: Policy) -> Decision:
    try:
        evidence = collect()
    except PrometheusUnavailable as exc:
        return Decision(Verdict.HOLD, f"prometheus could not answer: {exc}")
    return decide(evidence, policy)


def _roll_back(deployer: Deployer, clock: Clock, reason: str) -> Outcome:
    last_error = None
    for attempt in range(ROLLBACK_ATTEMPTS):
        try:
            deployer.set_canary_weight(0)
        except Exception as exc:  # noqa: BLE001 - any failure here means trying again
            last_error = exc
            if attempt < ROLLBACK_ATTEMPTS - 1:
                clock.sleep(2**attempt)
            continue
        return Outcome(Status.ROLLED_BACK, reason)
    return Outcome(Status.STUCK, f"{reason}; could not move traffic back: {last_error}")


def _walk(
    deployer: Deployer,
    collect: Callable[[], Evidence],
    policy: Policy,
    clock: Clock,
    report: Callable[[int, Decision], None],
) -> Outcome:
    for step in policy.steps:
        deployer.set_canary_weight(step)
        clock.sleep(policy.bake_seconds)
        deadline = clock.now() + policy.step_deadline_seconds
        while True:
            decision = evaluate(collect, policy)
            report(step, decision)
            if decision.verdict is Verdict.ROLLBACK:
                return _roll_back(deployer, clock, decision.reason)
            if decision.verdict is Verdict.PROMOTE:
                break
            if clock.now() >= deadline:
                reason = f"no verdict at {step}% before the deadline: {decision.reason}"
                return _roll_back(deployer, clock, reason)
            clock.sleep(policy.poll_seconds)
    return Outcome(Status.PROMOTED, f"healthy at every step up to {policy.steps[-1]}%")


def run_release(
    deployer: Deployer,
    collect: Callable[[], Evidence],
    policy: Policy,
    clock: Clock | None = None,
    report: Callable[[int, Decision], None] = lambda step, decision: None,
) -> Outcome:
    clock = clock or SystemClock()
    try:
        return _walk(deployer, collect, policy, clock, report)
    except Exception as exc:  # noqa: BLE001 - whatever broke, traffic must not stay on the canary
        return _roll_back(deployer, clock, f"release stopped: {exc}")
