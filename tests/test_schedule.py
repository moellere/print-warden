import pytest

from print_warden import schedule

H = 3600


def C(name, free=0.0, per=H, cap=50):
    return schedule.Candidate(name, free, per, cap)


def test_even_split_on_equal_idle_printers():
    assert schedule.allocate(10, [C("a"), C("b")]) == {"a": 5, "b": 5}


def test_a_busy_printer_takes_fewer():
    cands = [C("a"), C("b", free=4 * H)]
    share = schedule.allocate(10, cands)
    assert share == {"a": 7, "b": 3}
    plan = schedule.plan_summary(share, cands, 10)
    assert plan["all_done_in_s"] == 7 * H
    assert plan["one_printer_would_take_s"] == 10 * H


def test_plate_capacity_is_respected_and_overflow_refused():
    assert schedule.allocate(6, [C("a", cap=2), C("b", per=3 * H)]) == {"a": 2, "b": 4}
    with pytest.raises(ValueError, match="at most 4"):
        schedule.allocate(5, [C("a", cap=2), C("b", cap=2)])


def test_a_slow_printer_is_left_out_when_it_would_not_help():
    assert schedule.allocate(2, [C("a"), C("b", per=10 * H)]) == {"a": 2}


def test_power_hold():
    assert schedule.power_hold(4 * H, "0.31", 0.2, 3).startswith(
        "waiting for cheaper power"
    )
    assert schedule.power_hold(4 * H, "0.12", 0.2, 3) is None
    assert schedule.power_hold(1 * H, "0.31", 0.2, 3) is None
    assert schedule.power_hold(4 * H, None, 0.2, 3) is None
    assert schedule.power_hold(4 * H, "unavailable", 0.2, 3) is None


def test_assign_puts_long_jobs_first_on_the_soonest_printer():
    durations = {
        "a": {"fast": 100, "slow": 200},
        "b": {"fast": 60, "slow": 120},
        "c": {"fast": 60, "slow": 120},
        "big": {"slow": 300},  # only fits the slow printer
    }
    plan, done = schedule.assign(durations, {"fast": 0, "slow": 0})
    assert plan == {"big": "slow", "a": "fast", "b": "fast", "c": "fast"}
    assert done == {"fast": 220, "slow": 300}
    busy, _ = schedule.assign(
        {"x": {"fast": 100, "slow": 200}}, {"fast": 500, "slow": 0}
    )
    assert busy == {"x": "slow"}
    with pytest.raises(ValueError, match="fits none"):
        schedule.assign({"x": {}}, {})
