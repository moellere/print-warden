from print_warden import phase


def st(
    state="printing",
    bed=(20, 0),
    noz=(20, 0),
    layer=None,
    total=None,
    progress=0.2,
    **kw,
):
    return {"moonraker_connected": True, "klippy": "ready", "state": state, "layer": layer, "total_layers": total,
            "progress_pct": progress, "bed": {"temperature": bed[0], "target": bed[1]},
            "extruder": {"temperature": noz[0], "target": noz[1]}, **kw}  # fmt: skip


def test_garagex4_start_sequence_from_this_week():
    # what garagex4 reported, minute by minute, while it read "printing 0.2 %"
    assert phase.describe(st(bed=(47.5, 80), noz=(139.9, 140))) == (
        "heating",
        "heating: bed 48/80 °C",
    )
    key, words = phase.describe(st(bed=(80.2, 80), noz=(108.5, 80)))
    assert key == "starting" and "probing" in words and "80 °C" in words
    assert phase.describe(st(bed=(80, 80), noz=(200, 240))) == (
        "heating",
        "heating: nozzle 200/240 °C",
    )
    assert phase.describe(st(bed=(80, 80), noz=(240, 240))) == (
        "starting",
        "starting: purge and first moves",
    )
    assert phase.describe(st(bed=(80, 80), noz=(240, 240), layer=12, total=84)) == (
        "printing",
        "printing layer 12 of 84",
    )
    assert phase.describe(st(bed=(80, 80), noz=(240, 240), progress=37.4)) == (
        "printing",
        "printing, 37 %",
    )


def test_other_states():
    assert phase.describe(st(state="paused"))[0] == "paused"
    assert phase.describe(st(state="complete", noz=(120, 0))) == (
        "cooling",
        "complete; cooling (nozzle 120 °C)",
    )
    assert phase.describe(st(state="standby")) == ("idle", "idle")
    assert phase.describe({"moonraker_connected": False})[0] == "offline"
    assert (
        phase.describe(st(klippy="shutdown", klippy_message="M112"))[1]
        == "Klipper shutdown: M112"
    )


def test_why_there_is_no_bed_cleared_button():
    assert phase.bed_note({"state": "standby"}, "occupied") is None
    assert "when this print ends" in phase.bed_note({"state": "printing"}, "occupied")
    assert (
        phase.bed_note({"state": "standby"}, "clear", "cube")
        == "Bed is clear; cube is waiting for your Start."
    )
    assert "unknown" in phase.bed_note({"state": "standby"}, None)
