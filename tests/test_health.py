import httpx

from print_warden import health, moonraker, server

SETTINGS = {
    "printer": {"kinematics": "cartesian", "max_velocity": 300.0},
    "extruder": {"rotation_distance": 7.5, "nozzle_diameter": 0.4},
    "bed_mesh default": {"points": "..."},
}
MESH = {"profiles": {"default": {"points": [[0.0, 0.05], [-0.05, 0.02]]}}}


def test_config_changes_and_mesh_drift():
    snap = health.snapshot(SETTINGS, MESH)
    assert "bed_mesh default" not in snap["settings"]
    assert snap["mesh"] == {"default": (0.1, 0.005)}
    changed = {
        **SETTINGS,
        "extruder": {"rotation_distance": 7.71, "nozzle_diameter": 0.4},
        "fan": {"pin": "PA8"},
    }
    warped = {"profiles": {"default": {"points": [[0.0, 0.2], [-0.12, 0.05]]}}}
    found = health.findings(snap, changed, warped, True, "v0.12.0-45-g1", {}, {})
    assert [f["kind"] for f in found] == [
        "save_config_pending",
        "config_changed",
        "config_changed",
        "mesh_drift",
    ]
    assert found[1]["detail"] == "[extruder] rotation_distance: 7.5 -> 7.71"
    assert found[2]["detail"] == "[fan] added"
    assert "range 0.100 -> 0.320 mm" in found[3]["detail"]
    assert health.findings(snap, SETTINGS, MESH, False, None, {}, {}) == []
    assert health.findings(None, changed, warped, False, None, {}, {}) == []


def test_versions():
    found = health.version_findings(
        "v0.12.0-45-gabc",
        {"mcu": "v0.12.0-10-gdef", "rpi": "v0.11.0-200-g123"},
        {"klipper": {"version": "v0.12.0-45", "remote_version": "v0.12.0-80"},
         "moonraker": {"version": "v0.9.3", "remote_version": "v0.9.3"}},
    )  # fmt: skip
    assert found == [
        {"kind": "firmware_mismatch",
         "detail": "MCU 'rpi' runs v0.11.0-200-g123 but Klipper is v0.12.0-45-gabc: rebuild and flash its firmware"},
        {"kind": "update_available", "detail": "klipper v0.12.0-45 -> v0.12.0-80 available"},
    ]  # fmt: skip
    assert health.release("weird") is None


async def test_health_inputs_reads_moonraker(monkeypatch):
    def handler(r):
        path = r.url.path
        if path == "/printer/objects/list":
            return httpx.Response(
                200,
                json={
                    "result": {"objects": ["configfile", "mcu", "mcu rpi", "bed_mesh"]}
                },
            )
        if path == "/printer/objects/query":
            assert "mcu%20rpi" in str(r.url) or "mcu rpi" in r.url.query.decode()
            return httpx.Response(200, json={"result": {"status": {
                "configfile": {"settings": SETTINGS, "save_config_pending": True},
                "bed_mesh": MESH, "mcu": {"mcu_version": "v0.12.0-1"}, "mcu rpi": {"mcu_version": "v0.11.0-2"}}}})  # fmt: skip
        if path == "/printer/info":
            return httpx.Response(
                200, json={"result": {"software_version": "v0.12.0-45"}}
            )
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        moonraker.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    got = await moonraker.health_inputs("http://x")
    assert got["save_pending"] and got["host"] == "v0.12.0-45" and got["updates"] == {}
    assert got["mcus"] == {"mcu": "v0.12.0-1", "rpi": "v0.11.0-2"}


async def test_snapshot_then_alert_once_per_change(monkeypatch):
    inputs = {
        "settings": SETTINGS,
        "save_pending": False,
        "bed_mesh": MESH,
        "host": "v0.12.0",
        "mcus": {},
        "updates": {},
    }
    alerts = []

    async def read(base):
        return dict(inputs)

    async def alert(printer, severity, title, message, url=None):
        alerts.append((printer, message))
        return True

    monkeypatch.setattr(server.moonraker, "health_inputs", read)
    monkeypatch.setattr(server.HA, "alert", alert)
    for n in ("testp", "testq"):
        server.JOBS.kv_delete(f"health_snapshot:{n}")
        server.JOBS.kv_delete(f"health_alerted:{n}")
        server._w(n).mr.connected = n == "testp"
    first = await server.printer_health("testp")
    assert first["findings"] == [] and first["note"].startswith("snapshot taken")
    inputs["settings"] = {
        **SETTINGS,
        "printer": {"kinematics": "cartesian", "max_velocity": 250.0},
    }
    await server.health_alerts()
    await server.health_alerts()
    assert alerts == [("testp", "[printer] max_velocity: 300.0 -> 250.0")]
    assert server.health_report()["printers"]["testp"]["health"] == ["config_changed"]
    again = await server.printer_health("testp", accept=True)
    assert again["findings"] == []
