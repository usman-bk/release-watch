"""Real requests, a real scrape, and the gate reading the real answer."""

import subprocess
import sys
import threading

import pytest

from gate.decision import Policy, Verdict
from gate.evidence import EvidenceCollector
from gate.prometheus import Prometheus
from gate.release import Status, evaluate, run_release
from tests.integration.conftest import ROOT, wait_until

pytestmark = pytest.mark.integration

POLICY = Policy(
    window="1m",
    min_requests=100,
    max_extra_error_ratio=0.05,
    max_extra_slow_ratio=0.1,
    max_baseline_error_ratio=0.05,
    max_baseline_slow_ratio=0.1,
    max_scrape_age_seconds=5,
    steps=(10, 50),
    bake_seconds=4,
    poll_seconds=0.5,
    step_deadline_seconds=25,
)


def bring_up(stack, canary: dict | None = None, stable: dict | None = None):
    stack.start_service("stable", "v1", **(stable or {}))
    process = stack.start_service("canary", "v2", **(canary or {}))
    stack.start_alertmanager()
    stack.start_prometheus()
    return process


def collector(stack):
    return EvidenceCollector(Prometheus(stack.prometheus_url), "v2", POLICY.window)


def release(stack, load):
    return run_release(load, collector(stack).collect, POLICY)


def test_a_healthy_canary_is_promoted_through_every_step(stack):
    bring_up(stack)
    load = stack.load()
    outcome = release(stack, load)
    assert outcome.status is Status.PROMOTED, outcome.reason
    assert load.weights == [10, 50]
    assert load.sent["canary"] > 100


def test_a_failing_canary_is_rolled_back_and_stops_receiving_traffic(stack):
    bring_up(stack, canary={"demo_error_ratio": 0.5})
    load = stack.load()
    outcome = release(stack, load)
    assert outcome.status is Status.ROLLED_BACK, outcome.reason
    assert "error ratio" in outcome.reason
    assert load.weights == [10, 0]


def test_a_slow_canary_is_rolled_back(stack):
    bring_up(stack, canary={"demo_delay_ms": 350})
    load = stack.load()
    outcome = release(stack, load)
    assert outcome.status is Status.ROLLED_BACK, outcome.reason
    assert "slow ratio" in outcome.reason


def test_losing_prometheus_mid_release_never_promotes(stack):
    bring_up(stack)
    load = stack.load()
    threading.Timer(1.0, stack.prometheus.stop).start()
    outcome = release(stack, load)
    assert outcome.status is Status.ROLLED_BACK, outcome.reason
    assert "prometheus could not answer" in outcome.reason
    assert load.weights == [10, 0]


def test_a_canary_that_dies_is_never_promoted(stack):
    process = bring_up(stack)
    load = stack.load()
    threading.Timer(1.0, process.stop).start()
    outcome = release(stack, load)
    assert outcome.status is Status.ROLLED_BACK, outcome.reason
    assert "canary targets are up" in outcome.reason


def test_a_shared_failure_is_not_blamed_on_the_canary_and_does_not_promote_it(stack):
    bring_up(stack, canary={"demo_error_ratio": 0.3}, stable={"demo_error_ratio": 0.3})
    load = stack.load()
    load.set_canary_weight(50)

    def held_for_the_right_reason():
        decision = evaluate(collector(stack).collect, POLICY)
        return decision if "stable is failing" in decision.reason else None

    decision = wait_until(held_for_the_right_reason, 25, "the shared failure to be seen")
    assert decision.verdict is Verdict.HOLD


def test_a_canary_that_has_seen_no_traffic_is_held(stack):
    bring_up(stack)
    stack.load(canary=False)
    wait_until(lambda: stack.query('up{track="canary"}'), 10, "the first scrape")
    decision = evaluate(collector(stack).collect, POLICY)
    assert decision.verdict is Verdict.HOLD


def gate(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "gate", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_command_line_holds_and_exits_3_when_prometheus_is_unreachable():
    result = gate("check", "--prometheus", "http://127.0.0.1:9", "--version", "v2")
    assert result.returncode == 3
    assert '"verdict": "hold"' in result.stdout


def test_the_command_line_asks_the_real_prometheus(stack):
    bring_up(stack)
    wait_until(lambda: stack.query("increase(http_requests_total[1m])"), 15, "two scrapes")
    result = gate("check", "--prometheus", stack.prometheus_url, "--version", "v2")
    assert result.returncode == 3
    assert "too few" in result.stdout
