"""Spoolman client. Spoolman owns spools and materials; the warden only records which spool is on which printer
(the X4's Moonraker predates the spoolman component, so Moonraker cannot hold the active spool for us)."""

from __future__ import annotations

from typing import Any

import httpx


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

    async def use_weight(self, spool_id: int, grams: float) -> None:
        await self._c.put(f"/spool/{spool_id}/use", json={"use_weight": grams})

    @staticmethod
    def _summary(s: dict[str, Any]) -> dict[str, Any]:
        f = s.get("filament") or {}
        return {
            "id": s["id"],
            "material": (f.get("material") or "").upper(),
            "name": f.get("name"),
            "vendor": (f.get("vendor") or {}).get("name"),
            "color": f.get("color_hex"),
            "remaining_g": s.get("remaining_weight"),
            "location": s.get("location"),
            "archived": s.get("archived", False),
        }

    async def aclose(self) -> None:
        await self._c.aclose()
