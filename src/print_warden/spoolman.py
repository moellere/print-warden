"""Spoolman client. Spoolman owns spools and materials; the warden only records which spool is on which printer
(the X4's Moonraker predates the spoolman component, so Moonraker cannot hold the active spool for us)."""

from __future__ import annotations

from typing import Any

import httpx


def _price_per_g(s: dict[str, Any], f: dict[str, Any]) -> float | None:
    """The spool's price over its initial weight, else the filament's list price over its weight."""
    for price, weight in (
        (s.get("price"), s.get("initial_weight")),
        (f.get("price"), f.get("weight")),
    ):
        if price and weight:
            return float(price) / float(weight)
    return None


class Spoolman:
    def __init__(self, url: str):
        self._c = httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v1", timeout=10)

    async def spools(self, include_archived: bool = False) -> list[dict[str, Any]]:
        r = await self._c.get(
            "/spool", params={"allow_archived": str(include_archived).lower()}
        )
        r.raise_for_status()
        return [self._summary(s) for s in r.json()]

    async def spool(self, spool_id: int) -> dict[str, Any]:
        r = await self._c.get(f"/spool/{spool_id}")
        r.raise_for_status()
        return self._summary(r.json())

    async def use_length(self, spool_id: int, mm: float) -> dict[str, Any]:
        """Deduct filament by length; Spoolman converts to weight with the filament's density and diameter."""
        r = await self._c.put(f"/spool/{spool_id}/use", json={"use_length": mm})
        r.raise_for_status()
        return self._summary(r.json())

    @staticmethod
    def _summary(s: dict[str, Any]) -> dict[str, Any]:
        f = s.get("filament") or {}
        return {
            "id": s["id"],
            "material": (f.get("material") or "").upper(),
            "name": f.get("name"),
            "vendor": (f.get("vendor") or {}).get("name"),
            "color": f.get("color_hex"),
            "extruder_temp": f.get("settings_extruder_temp"),
            "bed_temp": f.get("settings_bed_temp"),
            "remaining_g": s.get("remaining_weight"),
            "price_per_g": _price_per_g(s, f),
            "location": s.get("location"),
            "archived": s.get("archived", False),
            "first_used": s.get("first_used"),
        }

    async def aclose(self) -> None:
        await self._c.aclose()
