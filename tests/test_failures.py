import pytest

from print_warden import failures, outcome, stats


@pytest.mark.parametrize(
    ("text", "cause"),
    [
        ("Must home axis first: 120.000 100.000 0.400 [0.000]", "not_homed"),
        ("Move out of range: 225.000 110.000 0.200 [12.0]", "move_out_of_range"),
        (
            "Extrude below minimum temp\nSee the 'min_extrude_temp' config option",
            "cold_extrusion",
        ),
        ("Extrude only move too long (120.000mm vs 50.000mm)", "extrude_too_long"),
        (
            "Move exceeds maximum extrusion (0.900mm^2 vs 0.640mm^2)",
            "extrusion_too_fast",
        ),
        ('Unknown command:"PRINT_START"', "unknown_command"),
        ("No trigger on probe after full movement", "probe_fault"),
        (
            "Heater extruder not heating at expected rate",
            "heater_not_heating",
        ),  # a shutdown cause
        ("Lost communication with MCU 'mcu'", "mcu_comm_lost"),
        ("something new", "unknown"),
    ],
)
def test_classify(text, cause):
    got, hint = failures.classify(text)
    assert got == cause and hint


def test_diagnose_reads_the_gcode_store():
    store = [
        {"message": "G1 X120 Y100 F6000", "type": "command"},
        {"message": "ok", "type": "response"},
        {"message": "G1 X225 Y110 E1.2", "type": "command"},
        {
            "message": "!! Move out of range: 225.000 110.000 0.200 [12.0]",
            "type": "response",
        },
    ]
    got = failures.diagnose(["", ""], store)
    assert got["failure_cause"] == "move_out_of_range"
    assert got["failure_detail"] == (
        "Move out of range: 225.000 110.000 0.200 [12.0] (last command: G1 X225 Y110 E1.2)"
    )
    # without the store, print_stats' message still classifies
    assert (
        failures.diagnose(["Must home axis first: 0 0 0"], [])["failure_cause"]
        == "not_homed"
    )
    assert failures.diagnose([], [])["failure_detail"] == "no message"


def test_stats_and_reason_read_the_cause():
    jobs = [
        {"printer": "a", "state": "failed", "failure_cause": "not_homed"},
        {"printer": "a", "state": "failed", "failure_cause": "not_homed"},
        {"printer": "b", "state": "failed", "failure_cause": "mcu_comm_lost"},
        {"printer": "b", "state": "done"},
    ]
    assert stats.failure_causes(jobs) == {
        "a": {"not_homed": 2},
        "b": {"mcu_comm_lost": 1},
    }
    assert (
        outcome.reason({"state": "failed", "failure_cause": "not_homed"}) == "not_homed"
    )
    labelled = {
        "state": "failed",
        "failure_cause": "not_homed",
        "outcome": "failed",
        "outcome_reason": "adhesion",
    }
    assert outcome.reason(labelled) == "adhesion"
    assert outcome.first("j", False, known_error=True)[0] == (
        "PRINT_WARDEN_REPRINT_j",
        "Reprint",
    )
