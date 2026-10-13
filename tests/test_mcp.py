"""The MCP server: in-memory client tests and one stdio test."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters

from reep.cli.schemas import envelope_schema
from reep.config import Settings
from reep.mcp import MAX_LIMIT, TOOLS, create_server
from reep.register import Register
from tests.conftest import make_settings
from tests.support.fixture_release import (
    MERGED,
    PALMER,
    STAMP_1,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(reg: Register, store_s1: Settings) -> Any:
    return create_server(lambda: reg, settings=store_s1)


async def _call(server: Any, tool: str, **args: Any) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, args)


def _ok(result: Any, command: str) -> dict[str, Any]:
    assert result.is_error is False, result.content
    doc = result.structured_content
    jsonschema.validate(doc, envelope_schema(command))
    return dict(doc)


def _error(result: Any) -> str:
    assert result.is_error is True
    return str(result.content[0].text)


async def test_tools_are_listed_read_only(server: Any) -> None:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert client.instructions is not None
        assert "triple" in client.instructions
    assert [t.name for t in tools] == list(TOOLS)
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False
        assert tool.description
    resolve = next(t for t in tools if t.name == "resolve")
    assert resolve.input_schema["required"] == ["provider", "namespace", "external_id"]


async def test_resolve(server: Any) -> None:
    doc = _ok(
        await _call(
            server,
            "resolve",
            provider="transfermarkt",
            namespace="spieler",
            external_id="568177",
        ),
        "resolve",
    )
    assert doc["release_stamp"] == STAMP_1
    assert doc["backend"] == "local"
    assert doc["data"]["status"] == "found"
    assert doc["data"]["reep_id"] == PALMER


async def test_translate(server: Any) -> None:
    doc = _ok(
        await _call(
            server,
            "translate",
            provider="transfermarkt",
            namespace="spieler",
            external_id="568177",
            to="fbref",
        ),
        "translate",
    )
    assert [t["external_id"] for t in doc["data"]["targets"]] == ["dc7f8a28"]


async def test_current_and_entity(server: Any) -> None:
    doc = _ok(await _call(server, "current", reep_id=MERGED), "current")
    assert doc["data"]["status"] == "redirected"
    assert doc["data"]["current_id"] == PALMER
    doc = _ok(await _call(server, "entity", reep_id=PALMER), "show")
    assert doc["data"]["label"] == "Cole Palmer"
    assert len(doc["data"]["bridges"]) == 6


async def test_search_is_bounded(server: Any) -> None:
    doc = _ok(await _call(server, "search", query="Cole Palmer", limit=1), "search")
    assert len(doc["data"]) == 1
    assert doc["truncated"] is True
    assert doc["next_offset"] == 1
    doc = _ok(
        await _call(server, "search", query="Chelsea", entity_type="team"), "search"
    )
    assert {m["entity_type"] for m in doc["data"]} == {"team"}
    message = _error(await _call(server, "search", query="Palmer", limit=500))
    assert f"between 1 and {MAX_LIMIT}" in message


async def test_providers(server: Any) -> None:
    doc = _ok(await _call(server, "providers", limit=2), "providers")
    assert len(doc["data"]) == 2
    assert doc["truncated"] is True
    assert doc["next_offset"] == 2
    doc = _ok(await _call(server, "providers", provider="transfermarkt"), "providers")
    assert {p["namespace"] for p in doc["data"]} == {
        "schiedsrichter",
        "spieler",
        "trainer",
        "verein",
    }
    assert doc["truncated"] is False
    message = _error(await _call(server, "providers", provider="nope"))
    assert message.startswith("Error executing tool providers: invalid_argument")


async def test_status(server: Any) -> None:
    doc = _ok(await _call(server, "status"), "status")
    assert doc["backend"] == "local"
    assert doc["release_stamp"] == STAMP_1
    assert doc["data"]["catalog"]["stamp"] == STAMP_1


async def test_errors_carry_the_code_and_hint(server: Any) -> None:
    message = _error(
        await _call(
            server,
            "resolve",
            provider="transfermarkt",
            namespace="player",
            external_id="568177",
        )
    )
    assert "invalid_namespace" in message
    assert "Hint: Valid namespaces for transfermarkt" in message
    message = _error(await _call(server, "current", reep_id="568177"))
    assert "invalid_reep_id" in message


async def test_missing_catalogue_is_a_tool_error(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "cache")
    server = create_server(lambda: Register.local(settings=settings), settings=settings)
    message = _error(
        await _call(
            server, "resolve", provider="opta", namespace="person", external_id="p1"
        )
    )
    assert "catalog_missing" in message
    assert "reep catalog sync" in message
    doc = _ok(await _call(server, "status"), "status")
    assert doc["backend"] is None


async def test_stdio_server(store_s1: Settings) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "reep", "mcp"],
        env={
            "REEP_CACHE_DIR": str(store_s1.cache_dir),
            "REEP_CONFIG_DIR": str(store_s1.config_dir),
            "PATH": os.environ.get("PATH", ""),
        },
    )
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        assert sorted(t.name for t in tools) == sorted(TOOLS)
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)
        result = await client.call_tool(
            "resolve",
            {
                "provider": "transfermarkt",
                "namespace": "spieler",
                "external_id": "568177",
            },
        )
    doc = _ok(result, "resolve")
    assert doc["data"]["reep_id"] == PALMER
