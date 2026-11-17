"""docs/stability.md against the code: every stable surface it names exists, and every one that exists is named."""

import re
from datetime import datetime
from pathlib import Path

import pytest

from print_warden import cli, config, ha_entities

DOC = (Path(__file__).parent.parent / "docs" / "stability.md").read_text()


def _section(title: str) -> str:
    m = re.search(
        rf"^## {re.escape(title)}.*?$(.*?)(?=^## |\Z)", DOC, re.MULTILINE | re.DOTALL
    )
    assert m, f"no section {title!r}"
    return m[1]


def _first_column(title: str, after: str | None = None) -> list[str]:
    text = _section(title)
    if after:
        text = text.split(after, 1)[1]
    rows = []
    for line in text.splitlines():
        if not line.startswith("| `"):
            if rows:
                break
            continue
        rows.append(line.split("|")[1].strip().strip("`"))
    return rows


async def test_mcp_tools_and_their_arguments():
    from print_warden import server

    tools = {t.name: t for t in await server.mcp.list_tools()}
    rows = {}
    for line in _section("MCP tools").splitlines():
        if m := re.match(r"^\| `(\w+)` \| (.*) \|$", line):
            rows[m[1]] = m[2]
    assert set(rows) == set(tools)
    for name, args in rows.items():
        schema = tools[name].inputSchema
        required = set(schema.get("required", []))
        want = [
            f"`{a}`" if a in required else f"`{a}?`"
            for a in schema.get("properties", {})
        ]
        assert args == (", ".join(want) or "—"), name


def test_cli_verbs():
    assert _first_column("CLI verbs") == ["print-warden", *cli.VERBS]


def test_healthz_fields():
    from print_warden import server

    report = server.health_report()
    assert _first_column("`/healthz`", "Top level") == list(report)
    per = _first_column("`/healthz`", "Per printer")
    assert per == list(next(iter(report["printers"].values())))


def test_config_sections():
    assert _first_column("Config") == list(config.SCHEMA)


def test_entity_suffixes():
    rows = ha_entities.printer_entities(
        "p", {"moonraker_connected": True}, None, None, 0, "clear", "PLA", datetime.now().astimezone(),
        [{"item": "nozzle", "used_pct": 1, "hours": 1, "days": 1, "every_hours": 9, "every_days": None, "due": False}],
    )  # fmt: skip
    suffixes = [r[0].removeprefix("sensor.print_warden_p_") for r in rows]
    assert [
        s if not s.startswith("maint_") else "maint_<item>" for s in suffixes
    ] == _first_column("Home Assistant entities")


def test_notification_verbs():
    src = (
        Path(__file__).parent.parent / "src" / "print_warden" / "server.py"
    ).read_text()
    body = src.split("async def _on_ha_action", 1)[1].split("\nasync def ", 1)[0]
    verbs = re.findall(r'verb == "([A-Z]+)"', body)
    for group in re.findall(r"verb in \(([^)]*)\)", body):
        verbs += re.findall(r'"([A-Z]+)"', group)
    assert _first_column("Notification actions") == verbs


@pytest.mark.parametrize("path", ["printers.example.yaml"])
def test_the_example_config_uses_only_known_keys(path):
    assert config.unknown_in(Path(__file__).parent.parent / path) == []
