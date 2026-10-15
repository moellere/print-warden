from print_warden import policy

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
