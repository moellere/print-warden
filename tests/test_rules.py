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
