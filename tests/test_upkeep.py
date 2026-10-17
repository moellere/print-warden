from datetime import UTC, datetime

import pytest

from print_warden import config, upkeep

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC).timestamp()
DAY = 86400


def test_reorder_counts_the_queue_against_stock():
    spools = [
        {"material": "PLA", "remaining_g": 300},
        {"material": "PLA", "remaining_g": 50},
        {"material": "PETG", "remaining_g": 900},
        {"material": "ABS", "remaining_g": 100, "archived": True},
    ]
    jobs = [
        {"state": "staged", "material": "PLA", "filament_g": 200},
        {"state": "done", "material": "PLA", "filament_g": 500},
        {"state": "awaiting_approval", "material": "TPU", "filament_g": 40},
    ]
    low = {r["material"]: r for r in upkeep.reorder(spools, jobs, 200)}
    assert set(low) == {"PLA", "TPU"}
    assert low["PLA"] == {
        "material": "PLA",
        "remaining_g": 350,
        "queued_g": 200,
        "after_queue_g": 150,
    }
    assert low["TPU"]["after_queue_g"] == -40


def test_drying_uses_the_later_of_first_use_and_last_dried():
    loaded = {
        "x4": {"id": 1, "material": "TPU", "first_used": "2026-10-01T00:00:00+00:00"},
        "x1": {"id": 2, "material": "PLA", "first_used": "2025-01-01T00:00:00Z"},
        "x9": {"id": 3, "material": "PETG", "first_used": None},
    }
    due = upkeep.drying(loaded, {}, {"TPU": 7, "PETG": 30}, NOW)
    assert [(d["printer"], d["spool_id"]) for d in due] == [("x4", 1)]
    assert upkeep.drying(loaded, {1: NOW - 2 * DAY}, {"TPU": 7}, NOW) == []


def test_maintenance_by_hours_or_days():
    items = upkeep.parse_maintenance(
        {"nozzle": {"every_hours": 10}, "belts": {"every_days": 90}}
    )
    jobs = [
        {
            "printer": "x4",
            "state": "done",
            "finished_at": NOW - 3600,
            "print_duration_s": 6 * 3600,
        },
        {
            "printer": "x4",
            "state": "failed",
            "finished_at": NOW - 7200,
            "print_duration_s": 5 * 3600,
        },
        {
            "printer": "x4",
            "state": "done",
            "finished_at": NOW - 40 * DAY,
            "print_duration_s": 99 * 3600,
        },
        {
            "printer": "x1",
            "state": "done",
            "finished_at": NOW - 3600,
            "print_duration_s": 99 * 3600,
        },
    ]
    resets = {"nozzle": NOW - 30 * DAY, "belts": NOW - 100 * DAY}
    got = {m["item"]: m for m in upkeep.maintenance("x4", items, jobs, resets, NOW)}
    assert got["nozzle"]["hours"] == 11
    assert got["nozzle"]["due"]
    assert got["belts"]["due"]
    assert got["belts"]["used_pct"] == 111
    fresh = upkeep.maintenance("x4", items, jobs, {}, NOW)
    assert not any(m["due"] for m in fresh)


def test_config(tmp_path):
    f = tmp_path / "printers.yaml"
    f.write_text(
        "printers:\n  p:\n    moonraker: http://p\n"
        "    maintenance:\n      nozzle: {every_hours: 300}\n"
        "upkeep:\n  reorder_below_g: 400\n  dry_after_days: {tpu: 7}\n"
    )
    cfg = config.load(f)
    assert cfg.printers["p"].maintenance == {"nozzle": upkeep.Interval(every_hours=300)}
    assert cfg.upkeep == config.Upkeep(reorder_below_g=400, dry_after_days={"TPU": 7})
    f.write_text(
        "printers:\n  p:\n    moonraker: http://p\n    maintenance:\n      nozzle: {}\n"
    )
    with pytest.raises(ValueError, match="every_hours or every_days"):
        config.load(f)
