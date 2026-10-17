from print_warden.config import Rules
from print_warden.rules import RuleEngine


def _st(state, **extra):
    return {"print_stats": {"state": state, "filename": "warden/a.gcode", **extra}}


def _kinds(evs):
    return [e.kind for e in evs]


def test_start_and_complete_carry_filament_used():
    r = RuleEngine("p", Rules())
    r.evaluate(_st("standby"), None, now=0)
    assert "print_started" in _kinds(r.evaluate(_st("printing"), None, now=1))
    evs = r.evaluate(
        _st("complete", print_duration=3600, filament_used=1234.5), None, now=2
    )
    done = next(e for e in evs if e.kind == "print_complete")
    assert done.data["filament_used_mm"] == 1234.5
    assert done.data["print_duration"] == 3600


def test_heater_deviation_only_after_settling():
    r = RuleEngine("p", Rules(heater_deviation_c=5, heater_deviation_s=30))
    r.evaluate(_st("standby"), None, now=0)

    def at(temp, now):
        return r.evaluate(
            {**_st("printing"), "extruder": {"target": 200, "temperature": temp}},
            None,
            now=now,
        )

    at(25, 1)
    assert "heater_deviation" not in _kinds(at(100, 100))
    at(200, 200)
    at(180, 210)
    assert "heater_deviation" in _kinds(at(180, 250))
    assert "heater_deviation" not in _kinds(at(180, 300))


def test_stall_alerts_once():
    r = RuleEngine("p", Rules(stall_minutes=1))
    r.evaluate(_st("standby"), None, now=0)
    st = {**_st("printing"), "virtual_sdcard": {"progress": 0.5}}
    r.evaluate(st, None, now=1)
    assert "progress_stall" in _kinds(r.evaluate(st, None, now=100))
    assert "progress_stall" not in _kinds(r.evaluate(st, None, now=200))


def test_unreachable_alerts_after_a_minute():
    r = RuleEngine("p", Rules())
    first = r.evaluate({}, "transport_lost", now=1000)
    assert not first[0].alert
    later = r.evaluate({}, "transport_lost", now=1090)
    assert later[0].alert and later[0].severity == "critical"
    back = r.evaluate(_st("standby"), "snapshot", now=1100)
    assert back[0].kind == "moonraker_reconnected" and back[0].alert


def test_layer_from_z_ignores_hops():
    r = RuleEngine("p", Rules())
    r.evaluate(_st("standby"), None, now=0)
    layers = []
    for z in (0.2, 0.6, 0.2, 0.4, 0.4):
        for e in r.evaluate(
            {**_st("printing"), "gcode_move": {"gcode_position": [0, 0, z]}},
            None,
            now=1,
        ):
            if e.kind == "layer_changed":
                layers.append(e.data["layer"])
    assert layers == [1, 2]


import pytest

from print_warden.rules import classify_shutdown, watched_object


@pytest.mark.parametrize(
    "msg,cause",
    [
        ("Heater extruder not heating at expected rate", "heater_not_heating"),
        ("MCU 'mcu' shutdown: ADC out of range", "thermistor_out_of_range"),
        ("Lost communication with MCU 'mcu'", "mcu_comm_lost"),
        ("MCU 'mcu' shutdown: Timer too close", "mcu_timing"),
        ("TMC 'stepper_x' reports error: GSTAT: reset=1", "tmc_driver_error"),
        ("something new", "unknown"),
    ],
)
def test_classify_shutdown(msg, cause):
    assert classify_shutdown(msg)[0] == cause


def test_shutdown_event_carries_the_cause():
    r = RuleEngine("p", Rules())
    st = {"webhooks": {"state_message": "MCU 'mcu' shutdown: ADC out of range"}}
    ev = r.evaluate(st, "notify_klippy_shutdown", now=1000)[0]
    assert ev.data["cause"] == "thermistor_out_of_range" and "thermistor" in ev.message


def test_watched_objects():
    assert all(
        watched_object(n)
        for n in (
            "fan",
            "heater_fan hotend_fan",
            "filament_switch_sensor runout",
            "temperature_sensor mcu",
        )
    )
    assert not any(
        watched_object(n) for n in ("fan_feedback", "extruder", "heater_bed")
    )


def _printing(**objs):
    return {**_st("printing"), **objs}


def test_fan_stall_needs_a_tachometer_and_time():
    r = RuleEngine("p", Rules(fan_stall_s=10))
    r.evaluate(_st("standby"), None, now=1000)
    assert "fan_stalled" not in _kinds(
        r.evaluate(_printing(fan={"speed": 1.0, "rpm": None}), None, now=1001)
    )
    stalled = {"fan": {"speed": 0.8, "rpm": 0}}
    r.evaluate(_printing(**stalled), None, now=1002)
    assert "fan_stalled" not in _kinds(r.evaluate(_printing(**stalled), None, now=1008))
    assert "fan_stalled" in _kinds(r.evaluate(_printing(**stalled), None, now=1013))
    assert "fan_stalled" not in _kinds(r.evaluate(_printing(**stalled), None, now=1030))
    r.evaluate(_printing(fan={"speed": 0.8, "rpm": 5000}), None, now=1031)
    r.evaluate(_printing(**stalled), None, now=1032)
    assert "fan_stalled" in _kinds(r.evaluate(_printing(**stalled), None, now=1050))


def test_runout_fires_on_the_edge_only():
    r = RuleEngine("p", Rules())
    r.evaluate(_st("standby"), None, now=1000)
    s = "filament_switch_sensor runout"
    r.evaluate(
        _printing(**{s: {"filament_detected": True, "enabled": True}}), None, now=1001
    )
    evs = r.evaluate(
        _printing(**{s: {"filament_detected": False, "enabled": True}}), None, now=1002
    )
    assert "filament_runout" in _kinds(evs)
    evs = r.evaluate(
        _printing(**{s: {"filament_detected": False, "enabled": True}}), None, now=1003
    )
    assert "filament_runout" not in _kinds(evs)
    r2 = RuleEngine("p", Rules())
    r2.evaluate(_st("standby"), None, now=1000)
    r2.evaluate(
        _printing(**{s: {"filament_detected": True, "enabled": False}}), None, now=1001
    )
    evs = r2.evaluate(
        _printing(**{s: {"filament_detected": False, "enabled": False}}), None, now=1002
    )
    assert "filament_runout" not in _kinds(evs)


def test_board_and_configured_temperature_limits():
    r = RuleEngine("p", Rules(temp_limits={"chamber": 45}))
    mcu = "temperature_sensor mcu_temp"
    chamber = "temperature_sensor chamber"
    other = "temperature_sensor enclosure"
    hot = {
        mcu: {"temperature": 80},
        chamber: {"temperature": 50},
        other: {"temperature": 90},
    }
    evs = r.evaluate({**_st("standby"), **hot}, None, now=1000)
    assert sorted(e.data["sensor"] for e in evs if e.kind == "over_temperature") == [
        chamber,
        mcu,
    ]
    assert not r.evaluate({**_st("standby"), **hot}, None, now=1001)
    r.evaluate({**_st("standby"), mcu: {"temperature": 72}}, None, now=1002)
    assert (
        r.evaluate({**_st("standby"), mcu: {"temperature": 76}}, None, now=1003) == []
    )
    r.evaluate({**_st("standby"), mcu: {"temperature": 60}}, None, now=1004)
    assert _kinds(
        r.evaluate({**_st("standby"), mcu: {"temperature": 76}}, None, now=1005)
    ) == ["over_temperature"]


def test_first_layer_check_once_per_print():
    r = RuleEngine("p", Rules(first_layer_check=2))
    r.evaluate(_st("standby"), None, now=1000)

    def layer(n, now):
        return r.evaluate(
            _printing(print_stats={"state": "printing", "info": {"current_layer": n}}),
            None,
            now=now,
        )

    r.evaluate(_st("printing"), None, now=1001)
    assert "first_layer_check" not in _kinds(layer(1, 1002))
    assert "first_layer_check" in _kinds(layer(2, 1003))
    assert "first_layer_check" not in _kinds(layer(3, 1004))
    assert RuleEngine("p", Rules(first_layer_check=0)).rules.first_layer_check == 0


def test_first_layer_check_not_sent_late_after_a_restart():
    r = RuleEngine("p", Rules(first_layer_check=2))
    st = {"print_stats": {"state": "printing", "info": {"current_layer": 150}}}
    r.evaluate(st, "snapshot", now=1000)
    st = {"print_stats": {"state": "printing", "info": {"current_layer": 151}}}
    assert "first_layer_check" not in _kinds(r.evaluate(st, None, now=1001))
