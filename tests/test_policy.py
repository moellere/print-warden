from datetime import UTC, datetime

from print_warden import policy
from print_warden.bedcheck import Check

READY = {"moonraker_connected": True, "klippy": "ready", "state": "standby"}


def _job(**kw):
    return {"state": "staged", "remote_name": "warden/x.gcode", "material": "PLA", **kw}


def test_ready_printer_passes():
    assert policy.can_start(_job(), READY, "pla") == (True, [])


def test_wrong_material_blocks():
    ok, reasons = policy.can_start(_job(), READY, "PETG")
    assert not ok and "PETG" in reasons[0]


def test_busy_printer_blocks():
    ok, reasons = policy.can_start(_job(), {**READY, "state": "printing"}, "PLA")
    assert not ok and reasons == ["printer is printing"]


def test_power_device_tolerates_klippy_down():
    st = {"moonraker_connected": True, "klippy": "shutdown", "state": "error"}
    assert policy.can_start(_job(), st, "PLA", has_power_device=True)[0]
    assert not policy.can_start(_job(), st, "PLA")[0]


def test_terminal_job_blocks():
    ok, reasons = policy.can_start(_job(state="done"), READY, "PLA")
    assert not ok and "done" in reasons[0]


def test_spool_weight_blocks_with_margin():
    job = _job(filament_g=100)
    assert policy.can_start(job, READY, "PLA", spool_remaining_g=111)[0]
    ok, reasons = policy.can_start(job, READY, "PLA", spool_remaining_g=105)
    assert not ok and "105 g left" in reasons[0]


def test_unknown_spool_weight_does_not_block():
    assert policy.can_start(_job(filament_g=100), READY, "PLA", spool_remaining_g=None)[
        0
    ]


def test_approval_expiry():
    job = {"approval_requested_at": 1000.0}
    assert not policy.approval_expired(job, 1000 + 14 * 60, 15)
    assert policy.approval_expired(job, 1000 + 16 * 60, 15)
    assert policy.approval_expired({}, 1000, 15)


def test_occupied_bed_blocks():
    ok, reasons = policy.can_start(_job(), READY, "PLA", bed_state="occupied")
    assert not ok and "bed not cleared" in reasons[0]
    assert policy.can_start(_job(), READY, "PLA", bed_state="unknown")[0]


def test_in_window():
    def at(h, m):
        return datetime(2026, 1, 1, h, m, tzinfo=UTC)

    assert policy.in_window("08:00-22:00", at(9, 0))
    assert not policy.in_window("08:00-22:00", at(22, 0))
    assert policy.in_window("22:00-06:00", at(23, 30))
    assert policy.in_window("22:00-06:00", at(5, 59))
    assert not policy.in_window("22:00-06:00", at(12, 0))
    assert not policy.in_window(None, at(12, 0))


def _checks(n, agree=True):
    return [Check(float(i), "clear", 0.0, agree, None) for i in range(n)]


NOON = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LIVE = Check(9.0, None, 0.004, True, None)


def test_auto_start_passes_with_clean_record():
    assert policy.can_auto_start("clear", LIVE, _checks(3), 3, "08:00-22:00", NOON) == (
        True,
        [],
    )


def test_auto_start_reasons():
    ok, reasons = policy.can_auto_start(
        "occupied",
        Check(9.0, None, 0.09, False, None),
        _checks(2),
        3,
        "08:00-10:00",
        NOON,
    )
    assert not ok
    text = " | ".join(reasons)
    for part in (
        "bed is occupied",
        "sees something",
        "2/3 labelled",
        "outside auto_start_hours",
    ):
        assert part in text


def test_one_disagreement_in_window_blocks():
    hist = _checks(3)
    hist[1] = Check(1.0, "occupied", 0.001, True, None)
    ok, reasons = policy.can_auto_start("clear", LIVE, hist, 3, "08:00-22:00", NOON)
    assert not ok and "disagreed" in reasons[0]
    assert not policy.can_auto_start("clear", None, _checks(3), 3, "08:00-22:00", NOON)[
        0
    ]
