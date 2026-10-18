import pytest


def test_purge_zone_is_validated():
    from print_warden.config import _purge_zone

    assert _purge_zone("p", None) is None
    assert _purge_zone("p", [0, 222, 220, 230]) == (0.0, 222.0, 220.0, 230.0)
    for bad in ([1, 2, 3], [10, 0, 5, 1], "0,0,1,1"):
        with pytest.raises(ValueError, match="purge_zone"):
            _purge_zone("p", bad)
