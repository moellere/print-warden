import time
from datetime import datetime

import pytest

from print_warden import calibration, digest


def test_day_and_slot():
    assert digest.day_index("Sunday") == 6 and digest.day_index("mon") == 0
    with pytest.raises(ValueError):
        digest.day_index("someday")
    wed = datetime(2026, 11, 18, 12, 0).astimezone()  # a Wednesday
    assert digest.slot(6, "18:00", wed) == datetime(2026, 11, 15, 18, 0).astimezone()
    assert digest.slot(2, "09:30", wed) == datetime(2026, 11, 18, 9, 30).astimezone()
    assert digest.slot(2, "13:00", wed) == datetime(2026, 11, 11, 13, 0).astimezone()


def _job(printer, state, **kw):
    now = time.time()
    return {
        "printer": printer,
        "state": state,
        "created": now - 3600,
        "finished_at": now - 60,
        **kw,
    }


def test_summary_counts_the_week_and_what_is_due():
    now = time.time()
    jobs = [
        _job(
            "a",
            "done",
            print_duration_s=7200,
            grams_used=40,
            cost=1.0,
            energy_kwh=0.3,
            energy_cost=0.1,
        ),
        _job("a", "done", outcome="failed", outcome_reason="adhesion", grams_used=5),
        _job("b", "failed", failure_cause="move_out_of_range"),
        _job(
            "b",
            "failed",
            failure_cause="move_out_of_range",
            calibration=calibration.fault_spec(),
        ),
        _job("b", "done", finished_at=now - 9 * 86400),
        _job("a", "staged"),
    ]
    upkeep = {
        "maintenance": [{"printer": "a", "item": "nozzle", "due": True}, {"printer": "b", "item": "belts", "due": False}],
        "drying": [{"printer": "a", "material": "TPU", "name": None}],
        "weigh": [{"printer": "b", "code": "S12"}],
        "reorder": [{"material": "PETG"}],
        "forecast": [{"material": "PLA", "days_left": 9.6}, {"material": "ABS", "days_left": 90}],
    }  # fmt: skip
    d = digest.summary(jobs, upkeep, now)
    assert (d["prints"], d["failed"]) == (3, 2)
    assert d["by_printer"] == {
        "a": {"prints": 2, "failed": 1},
        "b": {"prints": 1, "failed": 1},
    }
    assert {r["reason"] for r in d["reasons"]} == {"adhesion", "move_out_of_range"}
    assert (d["hours"], d["grams"], d["kwh"]) == (2.0, 45, 0.3)
    assert d["due"] == {
        "maintenance": ["a nozzle"], "drying": ["a: TPU"], "weigh": ["b: S12"], "reorder": ["PETG"],
        "running_out": ["PLA (~10 d)"],
    }  # fmt: skip
    title, msg = digest.message(d)
    assert title == "Week in prints"
    assert msg.startswith("3 print(s), 2 failed (")
    assert "Maintenance due: a nozzle." in msg and "Running out: PLA (~10 d)." in msg
    quiet = digest.message(digest.summary([], {}, now))[1]
    assert quiet == "No prints finished this week. Nothing due."


def test_status_page_shows_the_week():
    from print_warden import status

    week = digest.summary(
        [_job("a", "failed", failure_cause="move_out_of_range")], {}, time.time()
    )
    html = status.render({"printers": {}, "version": "x", "week": week})
    assert (
        "This week" in html
        and "1 print(s), 1 failed" in html
        and "move out of range ×1" in html
    )
