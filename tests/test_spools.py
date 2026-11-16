import copy
import json
import re

import httpx
import pytest

from print_warden import server, spools
from print_warden.spoolman import Spoolman
from tests.test_server import (  # noqa: F401 (fixture)
    _queued,
    _ready_printer,
    clean_queues,
    client,
)


def test_codes_colors_and_empty_weight():
    assert (
        spools.parse_code("S12")
        == spools.parse_code("s12")
        == spools.parse_code(12)
        == 12
    )
    with pytest.raises(ValueError):
        spools.parse_code("spool")
    assert spools.color("#1976d2") == ("1976D2", "#1976D2")
    assert spools.color("Black") == ("000000", "Black")
    with pytest.raises(ValueError, match="#RRGGBB"):
        spools.color("chartreuse-ish")
    raw = {
        "spool_weight": None,
        "filament": {"spool_weight": 0, "vendor": {"empty_spool_weight": 196}},
    }
    assert spools.empty_weight(raw) == 196
    assert spools.empty_weight({**raw, "spool_weight": 180}) == 180
    assert spools.empty_weight({}) is None


def test_drift():
    # counted 300 g used, the scale says 250 g: the counting is 20 % over
    d = spools.drift(1000, 700, 750)
    assert (d["used_counted_g"], d["used_measured_g"], d["drift_pct"]) == (
        300,
        250,
        20.0,
    )
    assert "20% over" in d["flag"]
    assert spools.drift(1000, 702, 700)["flag"] is None
    assert spools.drift(1000, 990, 980)["drift_pct"] is None  # too little used to judge


def test_swap_checks_and_weigh_due():
    spool = {"id": 3, "material": "PLA", "remaining_g": 100}
    queue = [
        {"material": "PLA", "filament_g": 80},
        {"material": "PLA", "filament_g": 40},
        {"material": "PETG"},
    ]
    assert spools.swap_checks(spool, queue, 40, 30) == [
        "the queue has PETG jobs; this spool is PLA",
        "the queued PLA jobs need 120 g; the spool has 100 g",
        "open 40 days (dry after 30): dry it first",
    ]
    assert spools.swap_checks(spool, [], 5, 30) == []
    loaded = {"a": {"id": 1, "name": "PLA Black"}, "b": {"id": 2}, "c": {"id": 3}}
    due = spools.weigh_due(loaded, {1: 0.0}, {2: 86400 * 9, 3: None}, 14, 86400 * 30)
    assert [(d["printer"], d["code"], d["weighed"]) for d in due] == [
        ("a", "S1", True),
        ("b", "S2", False),
    ]


async def test_spoolman_client_matches_or_creates():
    calls = []
    vendors = [{"id": 1, "name": "Elegoo"}]
    filaments = [
        {"id": 7, "vendor": {"id": 1}, "material": "PLA", "color_hex": "000000"}
    ]

    def handler(r):
        calls.append((r.method, r.url.path))
        if r.url.path == "/api/v1/vendor":
            if r.method == "GET":
                return httpx.Response(200, json=vendors)
            return httpx.Response(200, json={"id": 2, **json.loads(r.content)})
        if r.url.path == "/api/v1/filament":
            if r.method == "GET":
                return httpx.Response(200, json=filaments)
            return httpx.Response(200, json={"id": 8})
        return httpx.Response(404)

    sm = Spoolman("http://sm")
    sm._c = httpx.AsyncClient(
        base_url="http://sm/api/v1", transport=httpx.MockTransport(handler)
    )
    assert await sm.vendor_id("elegoo") == 1
    assert await sm.vendor_id("Polymaker") == 2
    assert await sm.filament_id(1, {"material": "PLA", "color_hex": "000000"}) == (
        7,
        False,
    )
    assert await sm.filament_id(1, {"material": "PETG", "color_hex": "000000"}) == (
        8,
        True,
    )
    assert ("POST", "/api/v1/vendor") in calls and ("POST", "/api/v1/filament") in calls


class FakeSpoolman:
    """Spoolman in memory, with the raw spool shape the client methods return."""

    def __init__(self):
        self.vendors = {1: {"id": 1, "name": "Elegoo", "empty_spool_weight": 190}}
        self.filaments = {}
        self.raw = {}

    def _sum(self, raw):
        return Spoolman._summary(raw)

    async def vendor_id(self, name):
        for v in self.vendors.values():
            if v["name"].lower() == name.lower():
                return v["id"]
        vid = len(self.vendors) + 1
        self.vendors[vid] = {"id": vid, "name": name}
        return vid

    async def filament_id(self, vendor_id, fields):
        for f in self.filaments.values():
            if f["vendor"]["id"] == vendor_id and (f["material"], f["color_hex"]) == (
                fields["material"],
                fields["color_hex"],
            ):
                return f["id"], False
        fid = len(self.filaments) + 1
        self.filaments[fid] = {"id": fid, **fields, "vendor": self.vendors[vendor_id]}
        return fid, True

    async def create_spool(self, fields):
        sid = len(self.raw) + 1
        f = self.filaments[fields["filament_id"]]
        self.raw[sid] = {
            "id": sid,
            "filament": f,
            "remaining_weight": fields.get("initial_weight"),
            **fields,
        }
        return self._sum(self.raw[sid])

    async def raw_spool(self, sid):
        return copy.deepcopy(self.raw[sid])

    async def spool(self, sid):
        return self._sum(self.raw[sid])

    async def spools(self, include_archived=False):
        return [
            self._sum(r)
            for r in self.raw.values()
            if include_archived or not r.get("archived")
        ]

    async def update_spool(self, sid, fields):
        self.raw[sid].update(fields)
        return self._sum(self.raw[sid])


@pytest.fixture
def sm(monkeypatch):
    fake = FakeSpoolman()
    monkeypatch.setattr(server, "SPOOLMAN", fake)
    for n in ("testp", "testq"):
        server.JOBS.kv_delete(f"active_spool:{n}")
    return fake


async def test_register_weigh_swap_retire(sm, clean_queues, monkeypatch):  # noqa: F811
    async def advance(printer):
        return {"action": "none", "why": "test"}

    monkeypatch.setattr(server, "advance_queue", advance)
    a = await server.register_spool(
        "elegoo", "pla", "black", "Enoch in chat", price=19.99, location="shelf"
    )
    assert (
        a["code"] == "S1"
        and a["filament_created"]
        and a["spool"]["name"] == "PLA Black"
    )
    b = await server.register_spool("Elegoo", "PLA", "#000000", "x")
    assert not b["filament_created"]  # same vendor, material and colour
    # weigh: empty from the vendor (190 g); counted 1000 -> 700, the scale says 940 gross = 750 left
    sm.raw[1]["remaining_weight"] = 700
    w = await server.weigh_spool("S1", 940, "Enoch in chat")
    assert (w["remaining_g"], w["counted_g"], w["drift_pct"]) == (750, 700, 20.0) and w[
        "flag"
    ]
    assert sm.raw[1]["remaining_weight"] == 750
    assert json.loads(server.JOBS.kv_get("weighed:1"))["g"] == 750
    # swap S2 onto testp with a PETG job queued and the outgoing S1 weighed
    _ready_printer("testp", material="PLA")
    await server.set_active_spool("testp", 1)
    petg = server.JOBS.create(
        printer="testp",
        name="q",
        material="PETG",
        remote_name="warden/q.gcode",
        offer=True,
    )
    server.JOBS.transition(petg["id"], "staged")
    s = await server.swap_spool("testp", "S2", "Enoch in chat", outgoing_gross_g=900)
    assert s["outgoing"]["remaining_g"] == 710 and s["active_spool"]["code"] == "S2"
    assert s["warnings"] == ["the queue has PETG jobs; this spool is PLA"]
    assert (
        server.JOBS.kv_get("active_spool:testp") == "2"
        and sm.raw[2]["location"] == "testp"
    )
    r = await server.retire_spool("S2", "x")
    assert r["unloaded_from"] == "testp" and sm.raw[2]["archived"]
    assert server.JOBS.kv_get("active_spool:testp") is None
    with pytest.raises(ValueError, match="no empty-spool weight"):
        sm.vendors[1]["empty_spool_weight"] = None
        await server.weigh_spool("S1", 900, "x")


async def test_weigh_reminder_and_phone_reply(sm, monkeypatch):
    import dataclasses

    from print_warden import config

    sent, alerts = [], []

    async def notify(title, message, actions, image=None, tag=None, **kw):
        sent.append((title, list(actions)))
        return True

    async def alert(printer, severity, title, message, url=None):
        alerts.append((title, message))
        return True

    async def clear(tag):
        return True

    monkeypatch.setattr(server.HA, "notify_actionable", notify)
    monkeypatch.setattr(server.HA, "alert", alert)
    monkeypatch.setattr(server.HA, "clear_notification", clear)
    monkeypatch.setattr(
        server,
        "CFG",
        dataclasses.replace(
            server.CFG, upkeep=config.Upkeep(weigh_every_days=14), quiet_hours=None
        ),
    )
    await server.register_spool("Elegoo", "PLA", "white", "x")
    sm.raw[1]["first_used"] = "2026-01-01T00:00:00"
    server.JOBS.kv_set("active_spool:testp", "1")
    server.JOBS.kv_delete("weighed:1")
    server.JOBS.kv_delete("upkeep_alerted:weigh:1")
    await server.upkeep_alerts()
    weigh = [s for s in sent if s[0] == "Weigh S1 on testp"]
    assert weigh and weigh[0][1] == [
        ("PRINT_WARDEN_WEIGH_1", "Enter grams", "textInput")
    ]
    await server._on_ha_action("PRINT_WARDEN_WEIGH_1", {"reply_text": "1140 g"})
    assert sm.raw[1]["remaining_weight"] == 950
    assert alerts[-1][0] == "S1 weighed" and alerts[-1][1].startswith("950 g left")


def test_spools_page(sm, client):  # noqa: F811
    page = client.get("/spools?token=test-token")
    assert page.status_code == 200 and "Register a spool" in page.text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text)[1]
    bad = client.post(
        "/spools/register?token=test-token",
        data={"csrf": "0" * 64, "vendor": "x", "material": "PLA", "color": "red"},
    )
    assert bad.status_code == 403 and not sm.raw
    ok = client.post(
        "/spools/register?token=test-token",
        data={"csrf": csrf, "vendor": "Sunlu", "material": "PETG", "color": "blue"},
    )
    assert (
        ok.status_code == 200
        and "Registered S1: PETG Blue" in ok.text
        and "S1 PETG Blue" in ok.text
    )
    oops = client.post(
        "/spools/weigh?token=test-token",
        data={"csrf": csrf, "spool": "S1", "gross_g": "800"},
    )
    assert oops.status_code == 400 and "no empty-spool weight" in oops.text
    assert client.get("/spools").status_code == 401
