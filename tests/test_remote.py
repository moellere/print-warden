import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from print_warden import config, remote, server
from print_warden.config import OAuthConfig

ISSUER = "https://id.example.com/application/o/print-warden/"
RESOURCE = "https://prints.example.com/mcp"
CFG = OAuthConfig(
    issuer=ISSUER,
    resource=RESOURCE,
    audience="warden-client",
    operator_groups=("print-operators",),
    queue_groups=("print-family",),
)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC = {"host": "prints.example.com"}
MCP_HEADERS = {**PUBLIC, "accept": "application/json, text/event-stream"}


def token(groups=("print-family",), key=KEY, **over):
    claims = {
        "iss": ISSUER,
        "aud": "warden-client",
        "sub": "u1",
        "preferred_username": "robin",
        "groups": list(groups),
        "exp": int(time.time()) + 300,
        **over,
    }
    return jwt.encode(claims, key, algorithm="RS256")


async def local_app(scope, receive, send):
    await PlainTextResponse("local")(scope, receive, send)


@pytest.fixture(scope="module")
def client():
    mcp = FastMCP(
        "test",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @mcp.tool()
    def submit_job(model: str) -> str:
        return f"queued {model}"

    @mcp.tool()
    def start_print(job_id: str) -> str:
        return f"started {job_id}"

    remote.install_tool_gate(mcp)
    checker = remote.TokenChecker(CFG, key_for=lambda _t: KEY.public_key())
    gate = remote.RemoteGate(local_app, mcp.streamable_http_app(), CFG, checker)
    with TestClient(
        mcp.streamable_http_app()
    ):  # runs the session manager for the gate's inner app
        yield TestClient(gate)


def rpc(client, method, params=None, bearer=None, headers=MCP_HEADERS):
    h = dict(headers)
    if bearer:
        h["authorization"] = f"Bearer {bearer}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=h)


def test_local_requests_go_to_the_local_app(client):
    r = client.post("/mcp", json={}, headers={"host": "10.0.0.5:8710"})
    assert (r.status_code, r.text) == (200, "local")
    assert client.get("/status", headers={"host": "warden.lan"}).text == "local"


def test_a_proxied_request_is_remote_whatever_its_host(client):
    r = client.post(
        "/mcp",
        json={},
        headers={"host": "10.0.0.5:8710", "x-forwarded-for": "203.0.113.9"},
    )
    assert r.status_code == 401


def test_remote_without_a_token_is_challenged_with_the_metadata_url(client):
    r = rpc(client, "tools/list")
    assert r.status_code == 401
    assert (
        r.headers["www-authenticate"]
        == 'Bearer resource_metadata="https://prints.example.com/.well-known/oauth-protected-resource/mcp"'
    )


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ],
)
def test_metadata_names_the_resource_and_the_issuer(client, path):
    meta = client.get(path, headers=PUBLIC).json()
    assert meta["resource"] == RESOURCE
    assert meta["authorization_servers"] == [ISSUER]
    assert meta["scopes_supported"] == ["openid", "profile"]


@pytest.mark.parametrize(
    "path", ["/status", "/upload", "/stage", "/files/jobs/x.gcode", "/healthz"]
)
def test_remote_reaches_nothing_but_mcp(client, path):
    assert (
        client.get(
            path, headers={**PUBLIC, "authorization": f"Bearer {token()}"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "bad",
    [
        lambda: token(aud="someone-else"),
        lambda: token(iss="https://evil.example.com/"),
        lambda: token(exp=int(time.time()) - 10),
        lambda: token(key=OTHER_KEY),
        lambda: "not-a-jwt",
    ],
)
def test_a_bad_token_is_refused(client, bad):
    r = rpc(client, "tools/list", bearer=bad())
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_an_account_in_no_group_is_forbidden(client):
    r = rpc(client, "tools/list", bearer=token(groups=("everyone",)))
    assert r.status_code == 403
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]


def test_queue_role_sees_and_calls_only_queue_tools(client):
    tools = rpc(client, "tools/list", bearer=token()).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["submit_job"]
    ok = rpc(
        client,
        "tools/call",
        {"name": "submit_job", "arguments": {"model": "cube.stl"}},
        bearer=token(),
    )
    assert ok.json()["result"]["content"][0]["text"] == "queued cube.stl"
    no = rpc(
        client,
        "tools/call",
        {"name": "start_print", "arguments": {"job_id": "j1"}},
        bearer=token(),
    )
    result = no.json()["result"]
    assert result["isError"] is True
    assert "for the operator" in result["content"][0]["text"]


def test_operator_role_has_every_tool(client):
    bearer = token(groups=("print-operators", "print-family"))
    tools = rpc(client, "tools/list", bearer=bearer).json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["start_print", "submit_job"]
    r = rpc(
        client,
        "tools/call",
        {"name": "start_print", "arguments": {"job_id": "j1"}},
        bearer=bearer,
    )
    assert r.json()["result"]["content"][0]["text"] == "started j1"


async def test_queue_tools_exist_and_leave_out_the_operator_only_ones():
    names = {t.name for t in await server.mcp.list_tools()}
    assert remote.QUEUE_TOOLS <= names
    assert not remote.QUEUE_TOOLS & {
        "start_print",
        "waive_blockers",
        "mark_bed_clear",
        "capture_bed_reference",
    }


def test_oauth_config_is_parsed_and_validated():
    raw = {
        "issuer": ISSUER,
        "resource": RESOURCE + "/",
        "audience": "c",
        "queue_groups": ["print-family"],
    }
    cfg = config._oauth(raw)
    assert cfg is not None
    assert (cfg.resource, cfg.queue_groups, cfg.scopes) == (
        RESOURCE,
        ("print-family",),
        ("openid", "profile"),
    )
    assert config._oauth(None) is None
    with pytest.raises(ValueError, match="audience"):
        config._oauth({"issuer": ISSUER, "resource": RESOURCE, "queue_groups": ["x"]})
    with pytest.raises(ValueError, match="nobody could sign in"):
        config._oauth({"issuer": ISSUER, "resource": RESOURCE, "audience": "c"})
