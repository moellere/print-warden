"""Remote access to /mcp over OAuth. With `server.oauth` set, a request that arrives for the public hostname (or
through a reverse proxy) reaches only /mcp and the RFC 9728 metadata, and only with a bearer token the configured
identity provider signed. The token's groups pick a role: operators get every tool, the queue role can look and
queue but cannot start a print, waive a check, say the bed is clear or change stock. Requests on the local network
are untouched; they keep the PRINT_WARDEN_TOKEN rule."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jwt
from mcp import types
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import OAuthConfig

log = logging.getLogger("print_warden.remote")

SCOPE_KEY = "print_warden.caller"
METADATA_PATH = "/.well-known/oauth-protected-resource"
PROXY_HEADERS = (b"forwarded", b"x-forwarded-for", b"cf-connecting-ip")
ALGORITHMS = ("RS256", "ES256")

QUEUE_TOOLS = frozenset(
    {
        "list_printers", "print_status", "get_events", "get_snapshot", "bed_status", "check_bed", "loaded_material",
        "list_spools", "batch_status", "list_saved_batches", "model_page", "queue", "model_history", "print_history",
        "printer_stats", "list_jobs", "job_status", "plan_week", "weekly_digest", "upkeep_status",
        "submit_job", "submit_split", "submit_batch", "save_batch", "queue_saved_batch", "reprint", "request_start",
        "offer_next",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Caller:
    subject: str
    name: str
    role: str  # "operator" or "queue"


class Denied(Exception):
    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


KeyResolver = Callable[[str], Any]


class TokenChecker:
    """Turns a bearer token into a Caller, or raises Denied. The signing key comes from the provider's JWKS."""

    def __init__(self, cfg: OAuthConfig, key_for: KeyResolver | None = None):
        self.cfg = cfg
        self._key_for = key_for
        self._jwks: jwt.PyJWKClient | None = None

    def _signing_key(self, token: str) -> Any:
        if self._key_for:
            return self._key_for(token)
        if self._jwks is None:
            url = self.cfg.jwks_url
            if not url:
                well_known = (
                    self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
                )
                with urllib.request.urlopen(well_known, timeout=10) as r:
                    url = json.load(r)["jwks_uri"]
            self._jwks = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
        return self._jwks.get_signing_key_from_jwt(token).key

    def check(self, token: str) -> Caller:
        try:
            claims = jwt.decode(
                token,
                self._signing_key(token),
                algorithms=list(ALGORITHMS),
                audience=self.cfg.audience,
                issuer=self.cfg.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            raise Denied(401, "invalid_token", f"token rejected: {e}") from e
        except Exception as e:
            raise Denied(
                401, "invalid_token", f"could not verify the token: {e}"
            ) from e
        groups = claims.get(self.cfg.groups_claim) or []
        groups = {groups} if isinstance(groups, str) else set(groups)
        if groups & set(self.cfg.operator_groups):
            role = "operator"
        elif groups & set(self.cfg.queue_groups):
            role = "queue"
        else:
            raise Denied(
                403,
                "insufficient_scope",
                "this account is in no group that may use the print warden",
            )
        name = claims.get("preferred_username") or claims.get("name") or claims["sub"]
        return Caller(subject=str(claims["sub"]), name=str(name), role=role)


class RemoteGate:
    """Sends remote requests through the OAuth rules and everything else to `local` unchanged."""

    def __init__(
        self,
        local: ASGIApp,
        app: ASGIApp,
        cfg: OAuthConfig,
        checker: TokenChecker | None = None,
    ):
        self.local, self.app, self.cfg = local, app, cfg
        self.checker = checker or TokenChecker(cfg)
        parts = urlsplit(cfg.resource)
        self.host = (parts.hostname or "").lower()
        self.path = parts.path.rstrip("/") or "/mcp"
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.metadata_url = f"{self.origin}{METADATA_PATH}{self.path}"

    def is_remote(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1").split(":")[0].lower()
        return host == self.host or any(h in headers for h in PROXY_HEADERS)

    def metadata(self) -> dict[str, Any]:
        return {
            "resource": self.cfg.resource,
            "authorization_servers": [self.cfg.issuer],
            "scopes_supported": list(self.cfg.scopes),
            "bearer_methods_supported": ["header"],
            "resource_name": "print-warden",
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.is_remote(scope):
            await self.local(scope, receive, send)
            return
        path = scope["path"].rstrip("/")
        if path in (METADATA_PATH, METADATA_PATH + self.path):
            await JSONResponse(
                self.metadata(), headers={"Access-Control-Allow-Origin": "*"}
            )(scope, receive, send)
            return
        if path != self.path:
            await JSONResponse({"error": "not_found"}, status_code=404)(
                scope, receive, send
            )
            return
        auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        if not auth.lower().startswith("bearer "):
            await self._deny(
                Denied(401, "", "sign in to use the print warden"), scope, receive, send
            )
            return
        try:
            caller = await asyncio.to_thread(self.checker.check, auth[7:].strip())
        except Denied as e:
            log.warning("remote request refused: %s", e.detail)
            await self._deny(e, scope, receive, send)
            return
        scope[SCOPE_KEY] = caller
        await self.app(scope, receive, send)

    async def _deny(
        self, e: Denied, scope: Scope, receive: Receive, send: Send
    ) -> None:
        challenge = f'Bearer resource_metadata="{self.metadata_url}"'
        if e.error:
            challenge += f', error="{e.error}", error_description="{e.detail}"'
        body = {"error": e.error or "unauthorized", "error_description": e.detail}
        await JSONResponse(
            body, status_code=e.status, headers={"WWW-Authenticate": challenge}
        )(scope, receive, send)


def install_tool_gate(mcp: FastMCP) -> None:
    """Hide and refuse the tools a remote caller's role does not include. Local callers see everything."""
    server = mcp._mcp_server
    list_tools = server.request_handlers[types.ListToolsRequest]
    call_tool = server.request_handlers[types.CallToolRequest]

    def caller() -> Caller | None:
        try:
            request = server.request_context.request
        except LookupError:
            return None
        found = getattr(request, "scope", {}).get(SCOPE_KEY)
        return found if isinstance(found, Caller) else None

    async def gated_list(req: types.ListToolsRequest) -> types.ServerResult:
        result = await list_tools(req)
        who = caller()
        if (
            who is None
            or who.role == "operator"
            or not isinstance(result.root, types.ListToolsResult)
        ):
            return result
        allowed = [t for t in result.root.tools if t.name in QUEUE_TOOLS]
        return types.ServerResult(result.root.model_copy(update={"tools": allowed}))

    async def gated_call(req: types.CallToolRequest) -> types.ServerResult:
        who = caller()
        if who is not None:
            name = req.params.name
            if who.role != "operator" and name not in QUEUE_TOOLS:
                log.warning("remote %s (%s) was refused %s", who.name, who.role, name)
                text = f"{name} is for the operator. This account can look at printers and jobs and queue prints."
                return types.ServerResult(
                    types.CallToolResult(
                        content=[types.TextContent(type="text", text=text)],
                        isError=True,
                    )
                )
            log.info("remote %s (%s) called %s", who.name, who.role, name)
        return await call_tool(req)

    server.request_handlers[types.ListToolsRequest] = gated_list
    server.request_handlers[types.CallToolRequest] = gated_call
