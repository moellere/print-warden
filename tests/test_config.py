import pytest


def test_purge_zone_is_validated():
    from print_warden.config import _purge_zone

    assert _purge_zone("p", None) is None
    assert _purge_zone("p", [0, 222, 220, 230]) == (0.0, 222.0, 220.0, 230.0)
    for bad in ([1, 2, 3], [10, 0, 5, 1], "0,0,1,1"):
        with pytest.raises(ValueError, match="purge_zone"):
            _purge_zone("p", bad)


def test_unknown_keys_are_named_with_the_closest_known_one():
    from print_warden import config

    raw = {
        "printers": {"x": {"moonraker": "h", "auto_strat": True, "cameras": {"anything": "u"},
                           "maintenance": {"belts": {"every_dayz": 9}}}},
        "polcy": {},
        "upkeep": {"dry_boxes": [{"name": "a", "entity": "s", "max_pc": 20}]},
    }  # fmt: skip
    assert config.unknown_keys(raw) == [
        "printers.x.auto_strat (did you mean auto_start?)",
        "printers.x.maintenance.belts.every_dayz (did you mean every_days?)",
        "polcy (did you mean policy?)",
        "upkeep.dry_boxes[0].max_pc (did you mean max_pct?)",
    ]
