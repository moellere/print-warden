import dataclasses

from print_warden import config, server, status, upkeep
from tests.test_server import _ready_printer


def test_forecast_and_dry_boxes():
    jobs = [
        {"material": "PLA", "state": "done", "grams_used": 300},
        {"material": "PLA", "state": "failed", "grams_used": 60},
        {"material": "PETG", "state": "staged", "grams_used": 999},  # not printed
    ]
    got = upkeep.forecast(jobs, {"PLA": 720.0, "TPU": 400.0}, 30)
    assert got == [
        {"material": "PLA", "stock_g": 720, "per_day_g": 12.0, "days_left": 60.0},
        {"material": "TPU", "stock_g": 400, "per_day_g": 0.0, "days_left": None},
    ]
    boxes = upkeep.dry_boxes([("A", 20, 15.0), ("B", 20, 34.5), ("C", 25, None)])
    assert [(b["box"], b["humidity"]) for b in boxes] == [("B", 34.5), ("C", None)]
    html = status.render({"printers": {}, "forecast": got})
    assert (
        "PLA: about 60 days left at recent use (12.0 g a day)" in html
        and "TPU:" not in html
    )


def test_config(tmp_path):
    cfg = tmp_path / "printers.yaml"
    cfg.write_text(
        "printers:\n  a:\n    moonraker: http://a:7125\n    cameras: {}\n    spool_scale_entity: sensor.a_scale\n"
        "upkeep:\n  todo_entity: todo.shopping\n  dry_boxes:\n    - {name: shelf, entity: sensor.shelf_rh, max_pct: 25}\n"
    )
    c = config.load(cfg)
    assert c.upkeep.todo_entity == "todo.shopping"
    assert c.upkeep.dry_boxes == (config.DryBox("shelf", "sensor.shelf_rh", 25.0),)
    assert c.printers["a"].spool_scale_entity == "sensor.a_scale"


class Stock:
    def __init__(self, grams):
        self.grams = grams

    async def spools(self, include_archived=False):
        return [
            {"id": 1, "material": "PLA", "remaining_g": self.grams, "archived": False}
        ]

    async def spool(self, sid):
        return {"id": sid, "material": "PLA", "remaining_g": self.grams}


async def test_reorder_lands_on_the_todo_list_and_leaves_it(monkeypatch):
    calls = []

    async def call(domain, service, data):
        calls.append((service, data["item"]))
        return True

    stock = Stock(50)
    monkeypatch.setattr(server, "SPOOLMAN", stock)
    monkeypatch.setattr(server.HA, "call_service", call)
    monkeypatch.setattr(
        server,
        "CFG",
        dataclasses.replace(
            server.CFG, upkeep=config.Upkeep(todo_entity="todo.shopping")
        ),
    )
    for k in server.JOBS.kv_prefix("todo_added:"):
        server.JOBS.kv_delete(k)
    assert await server.todo_sync() == ["PLA filament"]
    assert await server.todo_sync() == ["PLA filament"]
    assert calls == [("add_item", "PLA filament")]
    stock.grams = 3000
    assert await server.todo_sync() == []
    assert calls[-1] == ("remove_item", "PLA filament")


async def test_spool_scale_weighs_when_idle_and_moved(monkeypatch):
    weighed = []
    reading = {"v": "1150"}

    async def ha_state(entity):
        return reading["v"]

    async def weigh(spool, gross, who, empty_g=None):
        weighed.append((spool, gross, who))
        return {}

    p = dataclasses.replace(server._w("testp").p, spool_scale_entity="sensor.x4_scale")
    monkeypatch.setattr(server._w("testp"), "p", p)
    monkeypatch.setattr(server, "SPOOLMAN", Stock(900))
    monkeypatch.setattr(server.HA, "state", ha_state)
    monkeypatch.setattr(server, "weigh_spool", weigh)
    server.JOBS.kv_set("active_spool:testp", "7")
    server.JOBS.kv_delete("active_spool:testq")
    server.JOBS.kv_delete("scale_last:testp")
    _ready_printer("testp", state="printing")
    assert await server.scale_tick() == []  # never while printing
    _ready_printer("testp")
    assert await server.scale_tick() == ["S7"]
    reading["v"] = "1151"  # under the 2 g step
    assert await server.scale_tick() == []
    reading["v"] = "unavailable"
    assert await server.scale_tick() == []
    assert weighed == [("7", 1150.0, "testp spool scale")]
