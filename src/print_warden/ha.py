"""Home Assistant: webhook alerts and light control. The only write path the warden has."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import HomeAssistant

log = logging.getLogger(__name__)


class HAClient:
    def __init__(self, cfg: HomeAssistant):
        self.cfg = cfg
        self._c = httpx.AsyncClient(
            base_url=cfg.url,
            verify=cfg.verify_tls,
            timeout=10,
            headers={"Authorization": f"Bearer {cfg.token}"} if cfg.token else {},
        )

    async def alert(
        self,
        printer: str,
        severity: str,
        title: str,
        message: str,
        snapshot_url: str | None = None,
    ) -> bool:
        payload = {
            "printer": printer,
            "severity": severity,
            "title": title,
            "message": message,
            "snapshot_url": snapshot_url or "",
        }
        try:
            r = await self._c.post(f"/api/webhook/{self.cfg.webhook_id}", json=payload)
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("alert failed: %s", e)
            return False

    async def notify_actionable(
        self,
        title: str,
        message: str,
        actions: list[tuple[str, str]],
        image: str | None = None,
        tag: str | None = None,
    ) -> bool:
        if not self.cfg.notify_service:
            return False
        domain, service = self.cfg.notify_service.split(".", 1)
        data: dict[str, Any] = {
            "actions": [{"action": a, "title": t} for a, t in actions],
            "tag": tag or "print_warden_job",
            "channel": "Print warden",
            "importance": "high",
        }
        if image:
            data["image"] = image
        try:
            r = await self._c.post(
                f"/api/services/{domain}/{service}",
                json={"title": title, "message": message, "data": data},
            )
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("actionable notify failed: %s", e)
            return False

    async def state(self, entity_id: str) -> str | None:
        try:
            r = await self._c.get(f"/api/states/{entity_id}")
            return r.json().get("state") if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    async def light(self, entity_id: str, on: bool) -> None:
        domain = entity_id.split(".")[0]
        try:
            await self._c.post(
                f"/api/services/{domain}/turn_{'on' if on else 'off'}",
                json={"entity_id": entity_id},
            )
        except httpx.HTTPError as e:
            log.warning("light %s: %s", entity_id, e)

    async def aclose(self) -> None:
        await self._c.aclose()


def obico_event_to_alert(payload: dict[str, Any]) -> tuple[str, str, str, str | None]:
    """Map an Obico webhook payload to (kind, severity, message, img_url)."""
    ev = payload.get("event") or {}
    t = ev.get("type") or "unknown"
    img = payload.get("img_url") or None
    fn = (payload.get("print") or {}).get("filename") or ""
    if t == "PrintFailure":
        if ev.get("print_paused"):
            return (
                "obico_failure",
                "critical",
                f"Obico detected a failure and paused {fn}",
                img,
            )
        if ev.get("is_warning"):
            return (
                "obico_warning",
                "warning",
                f"Obico sees possible failure in {fn}",
                img,
            )
        return "obico_failure", "critical", f"Obico detected a failure in {fn}", img
    loud = {"PrintPaused", "PrintCancelled", "FilamentChange"}
    sev = "warning" if t in loud else "info"
    return f"obico_{t.lower()}", sev, f"Obico: {t} {fn}".strip(), img
