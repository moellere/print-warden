import dataclasses
import json
import stat

import httpx

from print_warden import check, config


def _vendor(tmp_path):
    v = tmp_path / "vendor"
    for kind, name, extra in (
        ("machine", "M", {}),
        ("process", "base P", {}),
        ("process", "P", {"inherits": "base P"}),
        ("filament", "F", {}),
    ):
        (v / kind).mkdir(parents=True, exist_ok=True)
        (v / kind / f"{name}.json").write_text(json.dumps(extra))
    orca = tmp_path / "AppRun"
    orca.write_text("#!/bin/sh\n")
    orca.chmod(orca.stat().st_mode | stat.S_IEXEC)
    rules = tmp_path / "rules.json"
    rules.write_text("{}")
    return config.SlicerConfig(
        orca=str(orca), vendor_dir=str(v), house_rules=str(rules), openscad="sh"
    )


def _cfg(tmp_path, **over):
    p = config.Printer(
        name="x4",
        moonraker="http://x4",
        cameras={"bed": "http://cam/bed.jpg"},
        machine="M",
        processes={"standard": "P"},
        filaments={"PLA": "F"},
        light="light.x4",
        power_device="printer",
        bed_check=config.BedCheck(camera="bed"),
    )
    base = {
        "printers": {"x4": p},
        "ha": config.HomeAssistant(
            url="http://ha",
            webhook_id="w",
            token="t",
            action_secret="s",
            notify_service="notify.phone",
            mqtt=config.Mqtt(host="broker"),
        ),
        "obico_url": None,
        "obico_webhook_secret": None,
        "storage_dir": tmp_path / "data",
        "keep_days": 14,
        "host": "0.0.0.0",
        "port": 8720,
        "public_url": "http://w",
        "slicer": _vendor(tmp_path),
        "spoolman_url": "http://spool",
        "api_token": "tok",
        "quiet_entity": "input_boolean.sleeping",
    }
    base.update(over)
    return config.Config(**base)


def _handler(broken=()):
    def handle(r: httpx.Request) -> httpx.Response:
        url = str(r.url)
        if any(b in url for b in broken):
            return httpx.Response(503)
        if url == "http://x4/machine/device_power/devices":
            return httpx.Response(
                200, json={"result": {"devices": [{"device": "printer"}]}}
            )
        if url == "http://x4/server/info":
            return httpx.Response(200, json={"result": {"klippy_state": "ready"}})
        if url.startswith("http://cam/"):
            return httpx.Response(
                200, content=b"jpg", headers={"content-type": "image/jpeg"}
            )
        if url.startswith("http://ha/"):
            assert r.headers["authorization"] == "Bearer t"
            if url == "http://ha/api/services":
                return httpx.Response(
                    200, json=[{"domain": "notify", "services": {"phone": {}}}]
                )
            if url.startswith("http://ha/api/states/"):
                known = ("light.x4", "input_boolean.sleeping")
                return httpx.Response(200 if url.rsplit("/", 1)[1] in known else 404)
            return httpx.Response(200, json={"message": "API running."})
        if url == "http://spool/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(404)

    return handle


async def _run(cfg, handler, mqtt_error=None):
    async def probe(m):
        if mqtt_error:
            raise mqtt_error

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await check.run(cfg, c, probe)


def _by_status(results, status):
    return [(r.area, r.detail) for r in results if r.status == status]


async def test_a_ready_setup_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda name: f"/usr/bin/{name}")
    results = await _run(_cfg(tmp_path), _handler())
    assert _by_status(results, check.FAIL) == []
    assert _by_status(results, check.WARN) == []
    areas = {r.area for r in results}
    assert {
        "x4 profiles",
        "x4 moonraker",
        "x4 power",
        "x4 camera bed",
        "spoolman",
        "mqtt",
    } <= areas


async def test_failures_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda name: f"/usr/bin/{name}")
    cfg = _cfg(tmp_path)
    p = cfg.printers["x4"]
    import dataclasses

    cfg = dataclasses.replace(
        cfg,
        printers={
            "x4": dataclasses.replace(
                p, filaments={"PLA": "F", "PETG": "missing"}, light="light.gone"
            )
        },
    )
    results = await _run(
        cfg, _handler(broken=("http://x4/", "http://spool/")), OSError("refused")
    )
    fails = dict(_by_status(results, check.FAIL))
    assert "missing" in fails["x4 profiles"]
    assert "x4 moonraker" in fails
    assert "spoolman" in fails
    assert "refused" in fails["mqtt"]
    assert any("light.gone" in d for a, d in _by_status(results, check.FAIL))


async def test_a_camera_that_is_not_an_image_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda name: f"/usr/bin/{name}")

    def handler(r):
        if str(r.url).startswith("http://cam/"):
            return httpx.Response(
                200, text="<html>login</html>", headers={"content-type": "text/html"}
            )
        return _handler()(r)

    results = await _run(_cfg(tmp_path), handler)
    assert "not an image" in dict(_by_status(results, check.FAIL))["x4 camera bed"]


async def test_missing_optional_pieces_are_warnings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        check.shutil, "which", lambda name: None if name == "sh" else f"/usr/bin/{name}"
    )
    cfg = _cfg(tmp_path, ha=None, spoolman_url=None, api_token=None, quiet_entity=None)
    results = await _run(cfg, _handler())
    warns = dict(_by_status(results, check.WARN))
    assert {"home assistant", "spoolman", "server"} <= set(warns)
    assert any("OpenSCAD" in d for d in warns.values())
    assert _by_status(results, check.FAIL) == []


async def test_home_assistant_check_follows_verify_tls(tmp_path, monkeypatch):
    real = httpx.AsyncClient
    made = []

    def client(**kw):
        made.append(kw.get("verify", True))
        return real(transport=httpx.MockTransport(_handler()))

    monkeypatch.setattr(check.httpx, "AsyncClient", client)

    async def probe(m):
        pass

    ha = _cfg(tmp_path).ha
    for verify in (False, True):
        made.clear()
        cfg = _cfg(tmp_path, ha=dataclasses.replace(ha, verify_tls=verify))
        results = await check.run(cfg, None, probe)
        assert ("home assistant", "http://ha accepts the token") in _by_status(
            results, check.OK
        )
        assert made == ([True, False] if not verify else [True])


def test_main_reports_a_bad_config(tmp_path, capsys):
    bad = tmp_path / "printers.yaml"
    bad.write_text("printers: [")
    assert check.main([str(bad)]) == 1
    assert capsys.readouterr().out.startswith("fail")


async def test_model_sites_only_when_asked(tmp_path, monkeypatch):
    import dataclasses

    monkeypatch.setattr(check.shutil, "which", lambda name: f"/usr/bin/{name}")
    cfg = _cfg(tmp_path)
    results = await _run(cfg, _handler())
    assert not [r for r in results if r.area in ("thingiverse", "printables")]
    cfg = dataclasses.replace(cfg, thingiverse_token="t")

    async def probe(m):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as c:
        results = await check.run(cfg, c, probe, sites=True)
    sites = {
        r.area: r.status for r in results if r.area in ("thingiverse", "printables")
    }
    assert sites == {"thingiverse": check.WARN, "printables": check.WARN}


async def test_power_entity_needs_a_known_unit(tmp_path, monkeypatch):
    import dataclasses

    monkeypatch.setattr(check.shutil, "which", lambda name: f"/usr/bin/{name}")
    cfg = _cfg(tmp_path)
    (name, p), *_ = cfg.printers.items()
    cfg = dataclasses.replace(
        cfg, printers={name: dataclasses.replace(p, power_entity="sensor.plug")}
    )
    base = _handler()

    def handler(unit):
        def h(r):
            if str(r.url) == "http://ha/api/states/sensor.plug":
                return httpx.Response(
                    200,
                    json={"state": "12", "attributes": {"unit_of_measurement": unit}},
                )
            return base(r)

        return h

    ok = await _run(cfg, handler("W"))
    assert any("power entity sensor.plug: power sensor" in r.detail for r in ok)
    bad = await _run(cfg, handler("A"))
    assert any(r.status == check.FAIL and "not in kWh/Wh/W/kW" in r.detail for r in bad)
