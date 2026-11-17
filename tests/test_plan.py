from datetime import datetime

from print_warden import plan


def _t(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm).astimezone().timestamp()


def test_a_long_evening_job_waits_for_morning():
    lane = plan.Lane(
        "x4", _t(17, 21), False, finish_window="07:00-23:00", quiet_hours="23:00-07:00"
    )
    start, why = plan.slot(lane, _t(17, 21), 4 * 3600)
    assert start == _t(18, 7)
    assert why == "finishes outside 07:00-23:00"


def test_an_auto_start_printer_keeps_to_its_hours_not_quiet_hours():
    lane = plan.Lane(
        "x1",
        _t(17, 2),
        False,
        quiet_hours="23:00-07:00",
        auto_start=True,
        auto_start_hours="01:00-05:00",
    )
    assert plan.slot(lane, _t(17, 2), 3600) == (_t(17, 2), None)
    lane = plan.Lane(
        "x1", _t(17, 6), False, auto_start=True, auto_start_hours="01:00-05:00"
    )
    assert plan.slot(lane, _t(17, 6), 3600)[0] == _t(18, 1)
    never = plan.Lane(
        "x1",
        0,
        False,
        finish_window="08:00-08:30",
        auto_start=True,
        auto_start_hours="09:00-10:00",
    )
    assert plan.slot(never, _t(17, 6), 1800) == (None, "finishes outside 08:00-08:30")


def test_lanes_queue_jobs_with_the_bed_clearing_gap():
    now = _t(17, 9)
    lanes = [
        plan.Lane("a", now + 3600, True, quiet_hours="23:00-07:00"),
        plan.Lane("b", now, False),
    ]
    queues = {
        "a": [
            {"id": "1", "name": "lid", "est_s": 7200},
            {"id": "2", "name": "base", "est_s": 3600},
        ],
        "b": [{"id": "3", "name": "clip", "est_s": 600, "offered": True}],
    }
    got = plan.plan(lanes, queues, now)
    a = got["a"]
    assert (
        a[0]["start"] == now + 3600 + plan.CLEAR_GAP_S
        and a[0]["waits"] == "the bed to be cleared"
    )
    assert a[1]["start"] == a[0]["finish"] + plan.CLEAR_GAP_S and a[1]["waits"] is None
    assert got["b"][0] == {"job_id": "3", "name": "clip", "start": now, "finish": now + 600,
                           "waits": "offered: waiting for the tap"}  # fmt: skip
    lines = plan.describe(a, now)
    assert lines[0] == "10:15 → 12:15 lid (waits: the bed to be cleared)"
    assert lines[1] == "12:30 → 13:30 base"
    assert plan.describe(
        [{"name": "x", "start": None, "finish": None, "waits": "no slot"}], now
    ) == ["unplanned: x (no slot)"]


def test_a_job_with_no_slot_in_the_horizon_is_not_held():
    lane = plan.Lane("x4", 0, False, quiet_hours="00:00-23:59")
    assert plan.slot(lane, _t(17, 20), 600) == (None, "quiet hours 00:00-23:59")


def test_status_page_shows_the_plan():
    from print_warden import status

    html = status.render(
        {"printers": {}, "version": "x", "plan": {"x4": ["10:15 → 12:15 lid"]}}
    )
    assert "<h2>Plan</h2>" in html and "10:15 → 12:15 lid" in html
