import pytest

from gate.decision import Evidence, Policy, Sample, Verdict, decide, wilson_bounds

POLICY = Policy()
HEALTHY_STABLE = Sample(requests=10_000, errors=0, slow=0)
HEALTHY_CANARY = Sample(requests=2_000, errors=0, slow=0)


def evidence(**changes) -> Evidence:
    values = {
        "canary": HEALTHY_CANARY,
        "stable": HEALTHY_STABLE,
        "canary_targets": 2,
        "canary_targets_up": 2,
        "oldest_scrape_age_seconds": 5.0,
    }
    return Evidence(**(values | changes))


def verdict(**changes):
    return decide(evidence(**changes), POLICY)


def test_a_clean_canary_is_promoted():
    assert verdict().verdict is Verdict.PROMOTE


@pytest.mark.parametrize(
    ("changes", "word"),
    [
        ({"canary_targets": 0, "canary_targets_up": 0}, "no canary target"),
        ({"canary_targets_up": 1}, "only 1 of 2"),
        ({"oldest_scrape_age_seconds": None}, "too old"),
        ({"oldest_scrape_age_seconds": 45.1}, "too old"),
        ({"canary": None}, "no request data"),
        ({"stable": None}, "no request data"),
        ({"canary": Sample(199, 0, 0)}, "canary has served too few"),
        ({"stable": Sample(199, 0, 0)}, "stable has served too few"),
    ],
)
def test_missing_or_doubtful_evidence_holds_and_never_promotes(changes, word):
    decision = verdict(**changes)
    assert decision.verdict is Verdict.HOLD
    assert word in decision.reason


def test_scrape_age_exactly_at_the_limit_is_still_trusted():
    assert verdict(oldest_scrape_age_seconds=45.0).verdict is Verdict.PROMOTE


def test_exactly_the_minimum_requests_is_enough_to_judge():
    bad = Sample(200, 100, 0)
    assert verdict(canary=bad).verdict is Verdict.ROLLBACK


def test_a_failing_canary_rolls_back():
    decision = verdict(canary=Sample(2_000, 200, 0))
    assert decision.verdict is Verdict.ROLLBACK
    assert "error ratio" in decision.reason


def test_a_slow_canary_rolls_back():
    decision = verdict(canary=Sample(2_000, 0, 400))
    assert decision.verdict is Verdict.ROLLBACK
    assert "slow ratio" in decision.reason


def test_a_handful_of_errors_in_a_small_sample_is_not_enough_to_roll_back():
    decision = verdict(canary=Sample(250, 2, 0))
    assert decision.verdict is Verdict.HOLD
    assert "not enough requests" in decision.reason


def test_an_undecided_latency_holds_even_when_errors_are_clean():
    decision = verdict(canary=Sample(1_000, 0, 25))
    assert decision.verdict is Verdict.HOLD
    assert "not enough requests" in decision.reason


def test_the_same_ratio_in_a_large_sample_is_enough_to_roll_back():
    assert verdict(canary=Sample(100_000, 800, 0)).verdict is Verdict.ROLLBACK


def test_a_canary_matching_a_stable_with_some_errors_can_be_promoted():
    stable = Sample(100_000, 500, 0)
    canary = Sample(100_000, 500, 0)
    assert verdict(stable=stable, canary=canary).verdict is Verdict.PROMOTE


def test_a_canary_matching_a_sick_stable_holds_rather_than_promotes():
    decision = verdict(stable=Sample(10_000, 3_000, 0), canary=Sample(5_000, 1_500, 0))
    assert decision.verdict is Verdict.HOLD
    assert "stable is failing" in decision.reason


def test_a_canary_matching_a_slow_stable_holds_rather_than_promotes():
    decision = verdict(stable=Sample(10_000, 0, 3_000), canary=Sample(5_000, 0, 1_500))
    assert decision.verdict is Verdict.HOLD
    assert "stable is slow" in decision.reason


def test_a_canary_far_worse_than_a_sick_stable_still_rolls_back():
    decision = verdict(stable=Sample(10_000, 3_000, 0), canary=Sample(5_000, 4_500, 0))
    assert decision.verdict is Verdict.ROLLBACK


def test_counts_from_an_extrapolated_increase_cannot_exceed_the_total():
    assert Sample(100, 101.4, 0).error_ratio == 1.0
    low, high = wilson_bounds(101.4, 100)
    assert low <= 1.0 and high == pytest.approx(1.0)


def test_wilson_bounds_for_no_data_say_nothing():
    assert wilson_bounds(0, 0) == (0.0, 1.0)


def test_wilson_upper_bound_at_zero_failures_is_above_zero():
    low, high = wilson_bounds(0, 200)
    assert low == 0.0
    assert high == pytest.approx(0.0188, abs=0.0002)


def test_wilson_matches_a_published_interval():
    low, high = wilson_bounds(10, 100)
    assert low == pytest.approx(0.0552, abs=0.0002)
    assert high == pytest.approx(0.1744, abs=0.0002)
