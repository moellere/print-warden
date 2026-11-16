import dataclasses

import pytest

from print_warden import config, presence, server
from tests.test_monitoring import w  # noqa: F401 (fixture)
from tests.test_server import (  # noqa: F401 (fixture)
    _queued,
    _ready_printer,
    clean_queues,
)


@pytest.fixture(autouse=True)
def fresh():
    presence.reset()
    yield
    presence.reset()


def test_is_home():
    homes = ("home",)
    assert presence.is_home("home", homes) is True
    assert presence.is_home("not_home", homes) is False
    assert presence.is_home("Work", homes) is False
    assert presence.is_home("2", homes) is True  # zone.home counts people
    assert presence.is_home("0", homes) is False
    for unknown in (None, "unknown", "unavailable", ""):
        assert presence.is_home(unknown, homes) is None


def test_transitions_and_unknown_readings():
    assert presence.update(True, 100) is None
    assert presence.update(None, 150) is None and presence.STATE["home"] is True
    assert presence.update(False, 200) == "left" and presence.STATE["away_since"] == 200
    assert presence.away()
    assert presence.update(False, 300) is None
    assert presence.update(True, 400) == "back"


def test_summary():
    jobs = [
        {"name": "clip", "state": "done"},
        {"name": "lid", "state": "failed", "failure_cause": "not_homed"},
        {"name": "q", "state": "staged"},
    ]
    printers = {
        "a": {"bed": "occupied", "queued": 2, "awaiting": True},
        "b": {"bed": "clear"},
    }
    text = presence.summary(jobs, printers, 5.25)
    assert text == (
        "Away 5.2 h. Finished: clip. Failed: lid (not homed). "
        "a: bed occupied, 2 queued, waiting for a Start tap. b: bed clear."
    )
    assert "No prints ended." in presence.summary([], {}, 1)


def test_config():
    assert config._presence(None) is None
    p = config._presence({"entity": "zone.home", "away_offer_max_hours": 2})
    assert (p.entity, p.away_offer_max_hours, p.auto_start_when_away) == (
        "zone.home",
        2.0,
        False,
    )


async def test_quiet_hours_do_not_mute_while_away(w):  # noqa: F811
    w.cfg = dataclasses.replace(w.cfg, quiet_hours="00:00-23:59")
    assert await w.quiet()
    presence.update(True, 0)
    presence.update(False, 1)
    assert not await w.quiet()


def _with_presence(monkeypatch, **kw):
    monkeypatch.setattr(
        server,
        "CFG",
        dataclasses.replace(
            server.CFG, presence=presence.Presence("person.enoch", **kw)
        ),
    )


async def test_no_unattended_start_unless_someone_is_home(monkeypatch):
    _with_presence(monkeypatch)
    p = dataclasses.replace(server._w("testp").p, auto_start=True)
    monkeypatch.setattr(server._w("testp"), "p", p)
    ok, why, _ = await server._auto_gate(server._w("testp"), {"est_seconds": 60})
    assert not ok and why[0].startswith("presence is unknown")
    presence.update(True, 0)
    presence.update(False, 1)
    ok, why, _ = await server._auto_gate(server._w("testp"), {"est_seconds": 60})
    assert not ok and why[0].startswith("nobody is home")


async def test_long_offers_wait_while_away_then_come_home(clean_queues, monkeypatch):  # noqa: F811
    _with_presence(monkeypatch, away_offer_max_hours=2)
    state = {"v": "home"}
    offered, alerts = [], []

    async def ha_state(entity):
        return state["v"]

    async def alert(printer, severity, title, message, url=None):
        alerts.append((title, message))
        return True

    async def request(job_id):
        offered.append(job_id)
        server.JOBS.transition(job_id, "awaiting_approval", "test")
        return {"notification_sent": True}

    monkeypatch.setattr(server.HA, "state", ha_state)
    monkeypatch.setattr(server.HA, "alert", alert)
    monkeypatch.setattr(server, "request_start", request)
    server._CORRECTIONS.clear()
    _ready_printer("testp")
    assert await server.presence_tick(now=1000) is None
    state["v"] = "not_home"
    assert await server.presence_tick(now=2000) == "left"
    j = _queued(est_seconds=5 * 3600)
    res = await server.advance_queue("testp")
    assert res["why"].startswith("nobody is home: q runs 5.0 h") and offered == []
    state["v"] = "home"
    assert await server.presence_tick(now=2000 + 3 * 3600) == "back"
    assert (
        alerts
        and alerts[0][0] == "while you were out"
        and alerts[0][1].startswith("Away 3.0 h.")
    )
    assert offered == [j["id"]]
