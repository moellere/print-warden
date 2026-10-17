from datetime import UTC, datetime, timedelta

from print_warden import ha_entities, stats
from print_warden.spoolman import _price_per_g


def _job(printer="p", state="done", est=3600, actual=3600, **kw):
    return {
        "printer": printer,
        "state": state,
        "est_seconds": est,
        "print_duration_s": actual,
        "created": 1_800_000_000.0,
        "finished_at": 1_800_000_000.0,
        "quality": "standard",
        "material": "PLA",
        **kw,
    }


def test_estimate_accuracy_groups_completed_prints():
    rows = [
        _job(actual=3960),
        _job(actual=4320),
        _job(state="failed", actual=100),
        _job(quality="fine", actual=1800),
    ]
    acc = stats.estimate_accuracy(rows)
    std = next(r for r in acc if r["quality"] == "standard")
    assert (
        std["prints"] == 2
        and std["median_actual_over_estimate"] == 1.15
        and std["worst"] == 1.2
    )


def test_correction_needs_samples_and_is_clamped():
    assert stats.correction([_job(actual=4000)] * 2, "p") == 1.0
    assert stats.correction([_job(actual=3960)] * 3, "p") == 1.1
    assert stats.correction([_job(actual=36000)] * 5, "p") == 2.0
    assert stats.correction([_job(printer="q", actual=3960)] * 5, "p") == 1.0


def test_correction_uses_the_most_recent_window():
    old = [_job(actual=7200)] * 30
    new = [_job(actual=3600)] * stats.CORRECTION_WINDOW
    assert stats.correction(old + new, "p") == 1.0


def test_outcomes_and_failure_reasons():
    rows = [
        _job(),
        _job(state="failed", error="print_cancelled: by user"),
        _job(state="failed", error="print_cancelled: again"),
        _job(material="PETG", state="failed", error="outcome unknown"),
        _job(state="skipped"),
    ]
    by_mat = {r["material"]: r for r in stats.outcomes(rows, "material")}
    assert by_mat["PLA"]["prints"] == 3 and by_mat["PLA"]["failure_rate"] == 0.667
    assert by_mat["PLA"]["top_failures"][0] == {"reason": "print_cancelled", "count": 2}
    assert by_mat["PETG"]["failed"] == 1


def test_monthly_usage():
    rows = [
        _job(grams_used=10.5, cost=0.21),
        _job(state="failed", grams_used=2, actual=1800),
        _job(state="staged"),
    ]
    (month,) = stats.monthly(rows)
    assert month["prints"] == 2 and month["failed"] == 1 and month["grams"] == 12
    assert month["hours"] == 1.5 and month["cost"] == 0.21


def test_price_per_gram():
    assert _price_per_g({"price": 25, "initial_weight": 1000}, {}) == 0.025
    assert _price_per_g({}, {"price": 20, "weight": 1000}) == 0.02
    assert _price_per_g({}, {}) is None


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_entities_while_printing():
    st = {
        "moonraker_connected": True,
        "state": "printing",
        "klippy": "ready",
        "progress_pct": 42.5,
        "filename": "f",
    }
    rows = {
        e: (s, a)
        for e, s, a in ha_entities.printer_entities(
            "Garage X4",
            st,
            {"id": "j1", "name": "part"},
            600,
            2,
            "occupied",
            "PLA",
            NOW,
        )
    }
    assert rows["sensor.print_warden_garage_x4_state"][0] == "printing"
    assert rows["sensor.print_warden_garage_x4_state"][1]["job"] == "part"
    assert rows["sensor.print_warden_garage_x4_progress"][0] == "42.5"
    assert (
        rows["sensor.print_warden_garage_x4_finish"][0]
        == (NOW + timedelta(seconds=600)).isoformat()
    )
    assert rows["sensor.print_warden_garage_x4_queue"][0] == "2"
    assert rows["sensor.print_warden_garage_x4_bed"][0] == "occupied"


def test_entities_idle_and_offline():
    idle = {
        e: s
        for e, s, _ in ha_entities.printer_entities(
            "p",
            {"moonraker_connected": True, "state": "standby"},
            None,
            None,
            0,
            "clear",
            None,
            NOW,
        )
    }
    assert (
        idle["sensor.print_warden_p_finish"] == "unknown"
        and idle["sensor.print_warden_p_progress"] == "0"
    )
    off = {
        e: s
        for e, s, _ in ha_entities.printer_entities(
            "p", {"moonraker_connected": False}, None, None, 0, "unknown", None, NOW
        )
    }
    assert off["sensor.print_warden_p_state"] == "offline"
