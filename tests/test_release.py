import boto3
import pytest
from botocore.stub import Stubber

from gate.decision import Evidence, Policy, Sample
from gate.deployer import AlbWeights
from gate.prometheus import PrometheusUnavailable
from gate.release import Status, run_release

POLICY = Policy(steps=(10, 50), bake_seconds=60, poll_seconds=10, step_deadline_seconds=30)
STABLE = Sample(10_000, 0, 0)
GOOD = Evidence(Sample(2_000, 0, 0), STABLE, 1, 1, 1.0)
BAD = Evidence(Sample(2_000, 500, 0), STABLE, 1, 1, 1.0)
THIN = Evidence(Sample(10, 0, 0), STABLE, 1, 1, 1.0)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeDeployer:
    def __init__(self, fail_when=lambda percent, nth: False):
        self.weights = []
        self.calls = 0
        self.fail_when = fail_when

    def set_canary_weight(self, percent):
        self.calls += 1
        if self.fail_when(percent, self.calls):
            raise RuntimeError("load balancer refused")
        self.weights.append(percent)


def run(answers, deployer=None, clock=None):
    answers = iter(answers)

    def collect():
        item = next(answers)
        if isinstance(item, Exception):
            raise item
        return item

    deployer = deployer or FakeDeployer()
    clock = clock or FakeClock()
    return run_release(deployer, collect, POLICY, clock), deployer, clock


def test_a_healthy_release_walks_every_step_and_stays_at_the_last():
    outcome, deployer, _ = run([GOOD, GOOD])
    assert outcome.status is Status.PROMOTED
    assert deployer.weights == [10, 50]


def test_every_step_bakes_before_it_is_judged():
    _, _, clock = run([GOOD, GOOD])
    assert clock.sleeps == [60, 60]


def test_a_bad_first_step_goes_back_to_zero_and_never_reaches_the_next():
    outcome, deployer, _ = run([BAD])
    assert outcome.status is Status.ROLLED_BACK
    assert deployer.weights == [10, 0]


def test_a_bad_later_step_goes_back_to_zero():
    outcome, deployer, _ = run([GOOD, BAD])
    assert outcome.status is Status.ROLLED_BACK
    assert deployer.weights == [10, 50, 0]


def test_a_hold_polls_and_can_still_promote():
    outcome, deployer, clock = run([THIN, THIN, GOOD, GOOD])
    assert outcome.status is Status.PROMOTED
    assert deployer.weights == [10, 50]
    assert clock.sleeps[:3] == [60, 10, 10]


def test_never_getting_a_verdict_rolls_back_at_the_deadline():
    outcome, deployer, clock = run([THIN] * 10)
    assert outcome.status is Status.ROLLED_BACK
    assert "deadline" in outcome.reason
    assert deployer.weights == [10, 0]
    assert clock.t == 60 + 30


def test_prometheus_going_away_holds_and_then_rolls_back():
    outcome, deployer, _ = run([PrometheusUnavailable("refused")] * 10)
    assert outcome.status is Status.ROLLED_BACK
    assert "prometheus could not answer" in outcome.reason
    assert deployer.weights == [10, 0]


def test_prometheus_returning_in_time_carries_on():
    outcome, _, _ = run([PrometheusUnavailable("refused"), GOOD, GOOD])
    assert outcome.status is Status.PROMOTED


def test_an_unexpected_crash_in_the_collector_still_moves_traffic_back():
    outcome, deployer, _ = run([RuntimeError("bug")])
    assert outcome.status is Status.ROLLED_BACK
    assert deployer.weights == [10, 0]


def test_failing_to_raise_the_weight_rolls_back():
    deployer = FakeDeployer(fail_when=lambda percent, nth: percent == 50)
    outcome, deployer, _ = run([GOOD], deployer=deployer)
    assert outcome.status is Status.ROLLED_BACK
    assert deployer.weights == [10, 0]


def test_a_rollback_that_fails_at_first_is_retried_with_backoff():
    deployer = FakeDeployer(fail_when=lambda percent, nth: percent == 0 and nth < 4)
    clock = FakeClock()
    outcome, deployer, clock = run([BAD], deployer=deployer, clock=clock)
    assert outcome.status is Status.ROLLED_BACK
    assert deployer.weights == [10, 0]
    assert clock.sleeps == [60, 1, 2]


def test_a_rollback_that_never_works_says_a_person_is_needed():
    deployer = FakeDeployer(fail_when=lambda percent, nth: percent == 0)
    outcome, deployer, _ = run([BAD], deployer=deployer)
    assert outcome.status is Status.STUCK
    assert "could not move traffic back" in outcome.reason
    assert deployer.calls == 1 + 4


def alb():
    client = boto3.client(
        "elbv2", region_name="eu-central-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    return client, Stubber(client)


def test_the_weights_are_sent_in_a_shape_the_aws_api_accepts():
    client, stubber = alb()
    listener = "arn:aws:elasticloadbalancing:eu-central-1:1:listener/app/a/b/c"
    stubber.add_response(
        "modify_listener",
        {"Listeners": []},
        {
            "ListenerArn": listener,
            "DefaultActions": [
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [
                            {"TargetGroupArn": "stable-arn", "Weight": 75},
                            {"TargetGroupArn": "canary-arn", "Weight": 25},
                        ],
                        "TargetGroupStickinessConfig": {"Enabled": False},
                    },
                }
            ],
        },
    )
    with stubber:
        AlbWeights(client, listener, "stable-arn", "canary-arn").set_canary_weight(25)
    stubber.assert_no_pending_responses()


@pytest.mark.parametrize("percent", [-1, 101])
def test_an_impossible_weight_is_refused_before_any_call(percent):
    client, stubber = alb()
    with stubber, pytest.raises(ValueError):
        AlbWeights(client, "l", "s", "c").set_canary_weight(percent)


def test_an_aws_error_reaches_the_caller():
    client, stubber = alb()
    stubber.add_client_error("modify_listener", "ThrottlingException")
    with stubber, pytest.raises(client.exceptions.ClientError):
        AlbWeights(client, "l", "s", "c").set_canary_weight(10)
