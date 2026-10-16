"""End-to-end tests for the MCP server: auth, scoping and tools.

The flatnotes HTTP API is replaced by an in-memory fake so these tests exercise
the MCP layer (token verification, the per-(token, server) scope matrix, the
``server`` argument resolution and the MCP_ALLOW_DELETE / MCP_EXPOSURE knobs)
without any network access.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import TOKENS, make_settings, mcp_client
from fastmcp import Client
from fastmcp.exceptions import ToolError

from flatnotes_mcp.client import FlatnotesError
from flatnotes_mcp.config import ServerConfig
from flatnotes_mcp.server import build_server


class FakeClient:
    """Minimal stand-in for FlatnotesClient backed by a shared dict."""

    def __init__(self, server: ServerConfig, store: dict[str, str]) -> None:
        self.server = server
        self.store = store
        self.attachments: dict[str, bytes] = {}
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def search(self, term, *, sort="score", order="desc", limit=None):
        titles = [t for t in sorted(self.store) if term == "*" or term in t]
        return [{"title": t, "lastModified": 1.0} for t in titles][:limit]

    async def list_recent(self, *, limit=None):
        return await self.search("*", sort="lastModified", order="desc", limit=limit)

    async def get_tags(self):
        return ["alpha"]

    async def get_note(self, title):
        if title not in self.store:
            raise FlatnotesError(404, "Note not found")
        return {"title": title, "content": self.store[title], "lastModified": 1.0}

    async def create_note(self, title, content):
        if title in self.store:
            raise FlatnotesError(409, "Note already exists")
        self.store[title] = content
        return {"title": title, "content": content, "lastModified": 1.0}

    async def update_note(self, title, *, new_title=None, new_content=None):
        if title not in self.store:
            raise FlatnotesError(404, "Note not found")
        content = new_content if new_content is not None else self.store[title]
        del self.store[title]
        final = new_title or title
        self.store[final] = content
        return {"title": final, "content": content, "lastModified": 2.0}

    async def delete_note(self, title):
        if title not in self.store:
            raise FlatnotesError(404, "Note not found")
        del self.store[title]

    async def upload_attachment(self, filename, data, *, content_type=None):
        self.attachments[filename] = data
        return {"filename": filename, "url": f"attachments/{filename}"}

    async def download_attachment(self, filename):
        return self.attachments[filename], "application/octet-stream"


class FakeStore:
    def __init__(self) -> None:
        self.per_server: dict[str, dict[str, str]] = {}
        self.clients: dict[str, FakeClient] = {}

    def factory(self, server: ServerConfig) -> FakeClient:
        store = self.per_server.setdefault(server.id, {})
        client = FakeClient(server, store)
        self.clients[server.id] = client
        return client


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def build(settings, store: FakeStore):
    return build_server(settings, client_factory=store.factory)


async def call(client: Client, name: str, arguments: dict[str, Any] | None = None):
    result = await client.call_tool(name, arguments or {})
    return result.data


async def tool_names(client: Client) -> set[str]:
    return {tool.name for tool in await client.list_tools()}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
async def test_unknown_token_is_rejected(settings, store):
    mcp = build(settings, store)
    with pytest.raises(ExceptionGroup) as excinfo:
        async with mcp_client(mcp, "not-a-real-token") as client:
            await client.list_tools()
    assert "Server returned an error response" in repr(excinfo.value)


async def test_valid_token_lists_servers(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["admin"]) as client:
        servers = await call(client, "list_servers")
    by_id = {s["id"]: s for s in servers}
    assert set(by_id) == {"main", "docs", "main-ro"}
    assert by_id["main"]["operations"] == ["delete", "read", "write"]
    assert by_id["main-ro"]["operations"] == ["read"]
    assert by_id["docs"]["description"] == "docs instance"


# --------------------------------------------------------------------------- #
# server argument resolution
# --------------------------------------------------------------------------- #
async def test_single_server_token_may_omit_server(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        await call(client, "create_note", {"title": "a", "content": "1"})
        recent = await call(client, "list_recent_notes")
    assert [n["title"] for n in recent] == ["a"]
    assert store.per_server["main"] == {"a": "1"}


async def test_multi_server_token_must_choose_a_server(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["admin"]) as client:
        with pytest.raises(ToolError, match="server.*required"):
            await call(client, "list_tags")
        tags = await call(client, "list_tags", {"server": "docs"})
    assert tags == ["alpha"]


async def test_unknown_server_is_rejected(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["admin"]) as client:
        with pytest.raises(ToolError, match="unknown or unauthorised"):
            await call(client, "list_tags", {"server": "nope"})


# --------------------------------------------------------------------------- #
# Scope enforcement
# --------------------------------------------------------------------------- #
async def test_read_only_token_write_is_denied(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["ro"]) as client:
        with pytest.raises(ToolError, match="lacks the 'write' scope"):
            await call(client, "create_note", {"title": "x", "content": "y"})
        with pytest.raises(ToolError, match="lacks the 'write' scope"):
            await call(client, "update_note", {"title": "x", "new_content": "y"})


async def test_read_write_token_can_create(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        note = await call(client, "create_note", {"title": "hello", "content": "world"})
        fetched = await call(client, "get_note", {"title": "hello"})
    assert note["title"] == "hello"
    assert fetched["content"] == "world"


async def test_scopes_are_per_server(settings, store):
    """docs-only can write to docs but cannot even read main."""
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["docs-only"]) as client:
        await call(client, "create_note", {"server": "docs", "title": "d", "content": "x"})
        with pytest.raises(ToolError, match="unknown or unauthorised"):
            await call(client, "get_note", {"server": "main", "title": "d"})


# --------------------------------------------------------------------------- #
# MCP_EXPOSURE
# --------------------------------------------------------------------------- #
async def test_exposure_filters_servers(store):
    settings = make_settings(exposure="public")
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["admin"]) as client:
        servers = await call(client, "list_servers")
        with pytest.raises(ToolError, match="unknown or unauthorised"):
            await call(client, "list_tags", {"server": "docs"})
    assert {s["id"] for s in servers} == {"main"}


async def test_token_with_no_visible_servers_gets_clear_error(store):
    settings = make_settings(exposure="public")
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["docs-only"]) as client:
        with pytest.raises(ToolError, match="grants no access"):
            await call(client, "list_tags")


# --------------------------------------------------------------------------- #
# MCP_ALLOW_DELETE
# --------------------------------------------------------------------------- #
async def test_delete_tool_is_absent_when_disabled(store):
    settings = make_settings(allow_delete=False)
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["admin"]) as client:
        assert "delete_note" not in await tool_names(client)


async def test_delete_requires_delete_scope(store):
    settings = make_settings(allow_delete=True)
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        with pytest.raises(ToolError, match="lacks the 'delete' scope"):
            await call(client, "delete_note", {"title": "nope"})


async def test_delete_succeeds_with_scope(store):
    settings = make_settings(allow_delete=True)
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["admin"]) as client:
        await call(client, "create_note", {"server": "docs", "title": "gone", "content": ""})
        message = await call(client, "delete_note", {"server": "docs", "title": "gone"})
    assert "deleted" in message
    assert store.per_server["docs"] == {}


# --------------------------------------------------------------------------- #
# Convenience tools
# --------------------------------------------------------------------------- #
async def test_append_note(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        await call(client, "create_note", {"title": "log", "content": "first"})
        note = await call(client, "append_note", {"title": "log", "text": "second"})
    assert note["content"] == "first\nsecond"


async def test_append_requires_read_scope(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["writer"]) as client:
        with pytest.raises(ToolError, match="lacks the 'read' scope"):
            await call(client, "append_note", {"title": "log", "text": "x"})


async def test_append_requires_text(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        with pytest.raises(ToolError, match="at least one of"):
            await call(client, "update_note", {"title": "log"})


async def test_upload_and_download_attachment(settings, store):
    import base64

    mcp = build(settings, store)
    payload = base64.b64encode(b"binary").decode()
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        uploaded = await call(
            client, "upload_attachment", {"filename": "f.bin", "content_b64": payload}
        )
        downloaded = await call(client, "download_attachment", {"filename": "f.bin"})
    assert uploaded["filename"] == "f.bin"
    assert base64.b64decode(downloaded["content_b64"]) == b"binary"


async def test_invalid_base64_is_rejected(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        with pytest.raises(ToolError, match="valid base64"):
            await call(client, "upload_attachment", {"filename": "f.bin", "content_b64": "!!"})


async def test_upstream_errors_are_prefixed_with_the_server(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        with pytest.raises(ToolError, match=r"\[main\].*Note not found"):
            await call(client, "get_note", {"title": "missing"})


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
async def test_servers_resource(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["ro"]) as client:
        contents = await client.read_resource("servers://list")
    assert "main" in contents[0].text


async def test_note_resource(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["rw"]) as client:
        await call(client, "create_note", {"title": "res", "content": "# hi"})
        contents = await client.read_resource("note://main/res")
    assert contents[0].text == "# hi"


async def test_note_resource_rejects_unauthorised_server(settings, store):
    mcp = build(settings, store)
    async with mcp_client(mcp, TOKENS["ro"]) as client:
        with pytest.raises(Exception, match="unknown or unauthorised"):
            await client.read_resource("note://main-ro/res")
