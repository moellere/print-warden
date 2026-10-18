import asyncio
import dataclasses

import pytest

from print_warden import energy, schedule, stats
from print_warden.events import Event
from print_warden.jobs import JobStore
from tests.test_monitoring import w  # noqa: F401 (fixture)


def _st(value, unit):
    return {"state": str(value), "attributes": {"unit_of_measurement": unit}}


def test_parse_units():
    assert energy.parse(_st(1500, "Wh")) == energy.Reading("energy", 1.5)
    assert energy.parse(_st(2.5, "kWh")) == energy.Reading("energy", 2.5)
    assert energy.parse(_st(0.2, "kW")) == energy.Reading("power", 200.0)
    assert energy.parse(_st(180, "W")) == energy.Reading("power", 180.0)
    assert energy.parse(_st("unavailable", "W")) is None
    assert energy.parse(_st(3, "A")) is None
    assert energy.parse(None) is None


def test_integrate_skips_gaps():
    assert energy.integrate(0, 100, 36, 300) == pytest.approx(0.002)
    assert energy.integrate(0, 100, 1000, 100) == 0.0


class Readings:
    def __init__(self, *values):
        self.values = list(values)

    async def __call__(self):
        return self.values.pop(0)


async def test_energy_counter(tmp_path):
    kv = JobStore(tmp_path / "j.db")
    m = energy.Meter(
        kv, Readings(_st(100.0, "kWh"), _st(100.05, "kWh"), _st(101.2, "kWh"))
    )
    await m.start("j", 0.2)
    await m.first_layer("j")
    out = await m.stop("j", 0.3)
    assert out == {"energy_kwh": 1.2, "heatup_kwh": 0.05, "energy_cost": 0.3}
    assert kv.kv_get("energy:j") is None
    assert await m.stop("j", 0.3) is None


async def test_power_sensor_is_integrated(tmp_path):
    kv = JobStore(tmp_path / "j.db")
    m = energy.Meter(kv, Readings(*(_st(v, "W") for v in (100, 200, 200, 100))))
    await m.start("j", None, now=0)
    await m.sample("j", now=30)
    await m.first_layer("j", now=60)
    out = await m.stop("j", None, now=90)
    want = (150 * 30 + 200 * 30 + 150 * 30) / 3_600_000
    assert out["energy_kwh"] == pytest.approx(want, abs=1e-4)
    assert out["heatup_kwh"] == pytest.approx(
        (150 * 30 + 200 * 30) / 3_600_000, abs=1e-4
    )
    assert "energy_cost" not in out


async def test_unreadable_sensor_meters_nothing(tmp_path):
    kv = JobStore(tmp_path / "j.db")
    m = energy.Meter(kv, Readings(None))
    await m.start("j", 0.2)
    assert await m.stop("j", 0.2) is None


def test_hold_by_measured_energy():
    # 2 h at 0.15 kW = 0.3 kWh: under min_kwh 0.5, so no hold even though it is over min_hours
    assert schedule.power_hold(7200, "0.4", 0.2, 1, kwh=0.3, min_kwh=0.5) is None
    held = schedule.power_hold(7200, "0.4", 0.2, 1, kwh=0.8, min_kwh=0.5)
    assert held and "0.80 kWh" in held
    # without a measured draw the hours rule stays
    assert schedule.power_hold(7200, "0.4", 0.2, 1, kwh=None, min_kwh=0.5)


def test_stats_energy_and_average_draw():
    jobs = [
        {"printer": "a", "state": "done", "energy_kwh": 0.6, "heatup_kwh": 0.1,
         "energy_cost": 0.18, "print_duration_s": 7200, "created": 0},
        {"printer": "a", "state": "failed", "energy_kwh": 0.2, "heatup_kwh": None,
         "energy_cost": 0.06, "print_duration_s": 3600, "created": 0},
        {"printer": "b", "state": "done", "energy_kwh": None, "print_duration_s": 60, "created": 0},
    ]  # fmt: skip
    assert stats.energy(jobs) == {
        "a": {
            "prints": 2,
            "kwh": 0.8,
            "avg_kw": 0.267,
            "avg_heatup_kwh": 0.1,
            "energy_cost": 0.24,
        }
    }
    assert energy.average_kw(jobs) == pytest.approx(0.8 / 3)
    month = stats.monthly(jobs)[0]
    assert (month["kwh"], month["energy_cost"]) == (0.8, 0.24)


async def test_watcher_meters_a_print(w, monkeypatch):  # noqa: F811
    from print_warden import config

    readings = iter([_st(10.0, "kWh"), _st(10.04, "kWh"), _st(10.5, "kWh")])

    async def state_full(entity):
        assert entity == "sensor.plug_energy"
        return next(readings)

    w.ha.state_full = state_full
    w.p = dataclasses.replace(w.p, power_entity="sensor.plug_energy")
    w.cfg = dataclasses.replace(w.cfg, energy=config.Energy(price_per_kwh=0.3))
    w.meter = energy.Meter(w.jobs, w._read_power)
    monkeypatch.setattr(energy, "SAMPLE_S", 0.01)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(w, "_outcome_prompt", nothing)
    j = w.jobs.transition(
        w.jobs.create(printer="p", name="part", remote_name="warden/a.gcode")["id"],
        "staged",
    )
    await w.handle(
        Event(
            "p",
            "print_started",
            "info",
            "go",
            {"filename": "warden/a.gcode"},
            alert=False,
        )
    )
    await asyncio.sleep(0.05)
    await w.handle(
        Event("p", "layer_changed", "debug", "L1", {"layer": 1}, alert=False)
    )
    await asyncio.sleep(0.05)
    await w.handle(
        Event(
            "p",
            "print_complete",
            "info",
            "done",
            {"filename": "warden/a.gcode"},
            alert=False,
        )
    )
    for _ in range(50):
        if w.jobs.get(j["id"]).get("energy_kwh") is not None:
            break
        await asyncio.sleep(0.01)
    got = w.jobs.get(j["id"])
    assert (got["energy_kwh"], got["heatup_kwh"], got["energy_cost"]) == (
        0.5,
        0.04,
        0.15,
    )
