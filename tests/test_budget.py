from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent_loop import budget
from agent_loop.agent import Limits, Window
from agent_loop.config import Settings

# Defaults of the plan: start allowance 0.2, reserves 0.10 and 0.05, step margin 0.03.
SETTINGS = Settings()
HOUR = 3600
# The weekly reset of the account is Monday 17:00 local time; a fixed offset keeps the tests
# independent of the machine time zone.
LOCAL = timezone(timedelta(hours=3))
WEEK_RESET = int(datetime(2026, 10, 19, 17, 0, tzinfo=LOCAL).timestamp())
WEEK_START = WEEK_RESET - budget.WEEK
FIVE_RESET = WEEK_START + 10 * HOUR
FIVE_START = FIVE_RESET - budget.FIVE_HOURS


def at(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=LOCAL).timestamp()


def five(utilization):
    return Window(utilization, FIVE_RESET)


def seven(utilization):
    return Window(utilization, WEEK_RESET)


@pytest.mark.parametrize(
    ("elapsed", "cap"),
    [(0, 0.1), (2 * HOUR, 0.5), (4 * HOUR, 0.9), (4.5 * HOUR, 0.9)],
)
def test_cap_5h_ramps_to_full_in_four_hours(elapsed, cap):
    assert budget.cap_5h(five(0), FIVE_START + elapsed, SETTINGS) == pytest.approx(cap)


@pytest.mark.parametrize(
    ("now", "day"),
    [
        (at(12, 17), 1),
        (at(13, 16, 59, 59), 1),
        (at(13, 17), 2),
        (at(14, 16, 59, 59), 2),
        (at(14, 17), 3),
        (at(19, 16, 59, 59), 7),
    ],
)
def test_cap_7d_counts_days_from_monday_17(now, day):
    assert budget.cap_7d(seven(0), now, SETTINGS) == pytest.approx(day / 7 - 0.05)


def test_step_allowed_under_both_caps():
    now = FIVE_START + 2 * HOUR

    verdict = budget.check(Limits(five(0.4), seven(0.05)), now, SETTINGS)

    assert verdict == budget.Verdict(True, None, "5h 40%/50% · 7d 5%/9%")


def test_5h_resumes_when_the_ramp_leaves_room_for_a_step():
    limits = Limits(five(0.4), None)
    now = FIVE_START + HOUR

    verdict = budget.check(limits, now, SETTINGS)

    # 0.2 + 0.8 * e / 4h - 0.10 - 0.03 > 0.4  =>  e > 1.65h
    assert not verdict.allowed
    assert verdict.resume_at == FIVE_START + int(1.65 * HOUR) + 1
    assert not budget.check(limits, verdict.resume_at - 1, SETTINGS).allowed
    assert budget.check(limits, verdict.resume_at, SETTINGS).allowed


def test_5h_beyond_the_curve_waits_for_reset():
    limits = Limits(five(0.88), None)

    verdict = budget.check(limits, FIVE_START + 4.5 * HOUR, SETTINGS)

    assert verdict.resume_at == FIVE_RESET
    assert budget.check(limits, FIVE_RESET, SETTINGS) == budget.Verdict(True, None, "5h - · 7d -")


def test_7d_resumes_at_the_start_of_the_first_day_with_room():
    limits = Limits(None, seven(0.30))

    verdict = budget.check(limits, at(13, 18), SETTINGS)

    # day 3: 3/7 - 0.05 - 0.03 = 0.349 > 0.30
    assert verdict.resume_at == at(14, 17)
    assert not budget.check(limits, verdict.resume_at - 1, SETTINGS).allowed
    assert budget.check(limits, verdict.resume_at, SETTINGS).allowed


def test_7d_beyond_the_curve_waits_for_reset():
    verdict = budget.check(Limits(None, seven(0.93)), at(19, 12), SETTINGS)

    assert verdict.resume_at == WEEK_RESET


def test_both_windows_short_wait_for_the_later_one():
    verdict = budget.check(Limits(five(0.4), seven(0.30)), FIVE_START + HOUR, SETTINGS)

    assert verdict.resume_at == at(14, 17)


@pytest.mark.parametrize(
    "limits",
    [
        Limits(None, None),
        Limits(Window(0.99, FIVE_START), Window(0.99, WEEK_START)),
    ],
    ids=["no windows", "windows already reset"],
)
def test_missing_or_reset_windows_do_not_block(limits):
    verdict = budget.check(limits, FIVE_START + HOUR, SETTINGS)

    assert verdict == budget.Verdict(True, None, "5h - · 7d -")


def test_reserves_and_margin_come_from_settings():
    settings = replace(SETTINGS, reserve_5h=0, step_margin=0.01)

    verdict = budget.check(Limits(five(0.18), None), FIVE_START, settings)

    assert verdict.allowed
    assert verdict.summary == "5h 18%/20% · 7d -"


def test_limits_file_round_trip(tmp_path):
    path = tmp_path / budget.LIMITS_FILE
    limits = Limits(five(0.31), seven(0.52))

    assert budget.load_limits(path) is None
    budget.save_limits(path, limits)

    assert budget.load_limits(path) == limits
    assert [p.name for p in tmp_path.iterdir()] == [budget.LIMITS_FILE]


def test_event_without_a_window_keeps_the_known_one(tmp_path):
    path = tmp_path / budget.LIMITS_FILE
    budget.save_limits(path, Limits(five(0.31), seven(0.52)))

    budget.save_limits(path, Limits(five(0.35), None))

    assert budget.load_limits(path) == Limits(five(0.35), seven(0.52))


def test_overrun_is_taken_from_the_worse_window():
    now = FIVE_START + 2 * HOUR

    # 5h: 0.4 - 0.5; 7d on day 1: 0.2 - (1/7 - 0.05)
    assert budget.overrun(Limits(five(0.4), seven(0.2)), now, SETTINGS) == pytest.approx(
        0.2 - 1 / 7 + 0.05
    )
    assert budget.overrun(Limits(None, Window(0.9, WEEK_START)), now, SETTINGS) is None
