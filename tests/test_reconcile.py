from print_warden.reconcile import decide

REMOTE = "warden/abc_part_PLA.gcode"


def _job(state, **kw):
    return {"state": state, "remote_name": REMOTE, "started_at": 1000.0, **kw}


def test_still_printing_is_left_alone():
    st = {"filename": REMOTE, "state": "printing"}
    assert decide(_job("printing"), st, []) is None


def test_completed_in_history():
    hist = [
        {"filename": "other.gcode", "status": "completed", "start_time": 1100},
        {
            "filename": REMOTE,
            "status": "completed",
            "start_time": 1010,
            "filament_used": 500.0,
        },
    ]
    assert decide(_job("printing"), {"state": "standby"}, hist) == (
        "done",
        "completed while the warden was away (history)",
        500.0,
    )


def test_cancelled_in_history_fails():
    hist = [{"filename": REMOTE, "status": "cancelled", "start_time": 1010}]
    state, note, _ = decide(_job("printing"), {"state": "standby"}, hist)
    assert state == "failed" and "cancelled" in note


def test_older_run_of_same_file_is_ignored():
    hist = [{"filename": REMOTE, "status": "completed", "start_time": 10}]
    state, note, _ = decide(_job("printing"), {"state": "standby"}, hist)
    assert state == "failed" and "unknown" in note


def test_approved_after_restart_goes_back_to_staged():
    assert decide(_job("approved"), {"state": "standby"}, [])[0] == "staged"


def test_pending_job_found_printing():
    st = {"filename": REMOTE, "state": "printing"}
    assert decide(_job("staged"), st, [])[0] == "printing"
    assert decide(_job("awaiting_approval"), {"state": "standby"}, []) is None
