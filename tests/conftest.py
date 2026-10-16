"""Shared fixtures for the flatnotes-mcp test suite."""

from __future__ import annotations

import contextlib
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
import yaml
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from flatnotes_mcp.config import (
    ServerConfig,
    Settings,
    parse_servers,
    parse_tokens,
    sha256_hex,
)

# Plaintext tokens used throughout the tests. Their digests are written into a
# throwaway tokens.yaml.
TOKENS = {
    "admin": "a" * 64,
    "rw": "b" * 64,
    "ro": "c" * 64,
    "docs-only": "d" * 64,
    "writer": "e" * 64,
}

SERVERS_YAML = textwrap.dedent(
    """
    servers:
      - id: main
        url: http://flatnotes:8080
        description: main instance
        exposure: public
        capabilities: [read, write, delete]
      - id: docs
        url: http://flatnotes-docs:8080
        description: docs instance
        exposure: internal
        capabilities: [read, write, delete]
      - id: main-ro
        url: http://flatnotes-main-ro:8080
        description: readonly view
        exposure: internal
        capabilities: [read]
    """
)

TOKENS_YAML = textwrap.dedent(
    f"""
    tokens:
      - name: admin
        token_sha256: {sha256_hex(TOKENS["admin"])}
        servers:
          main: [read, write, delete]
          docs: [read, write, delete]
          main-ro: [read]
      - name: rw
        token_sha256: {sha256_hex(TOKENS["rw"])}
        servers:
          main: [read, write]
      - name: ro
        token_sha256: {sha256_hex(TOKENS["ro"])}
        servers:
          main: [read]
      - name: docs-only
        token_sha256: {sha256_hex(TOKENS["docs-only"])}
        servers:
          docs: [read, write]
      - name: writer
        token_sha256: {sha256_hex(TOKENS["writer"])}
        servers:
          docs: [write]
    """
)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "servers.yaml").write_text(SERVERS_YAML, encoding="utf-8")
    (tmp_path / "tokens.yaml").write_text(TOKENS_YAML, encoding="utf-8")
    return tmp_path


def make_settings(exposure: str = "all", allow_delete: bool = True, **_) -> Settings:
    servers = parse_servers(yaml.safe_load(SERVERS_YAML))
    tokens = parse_tokens(yaml.safe_load(TOKENS_YAML), servers)
    return Settings(
        servers=servers, tokens=tokens, exposure=exposure, allow_delete=allow_delete
    )


@pytest.fixture
def settings_factory():
    return make_settings


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def server_config(**overrides) -> ServerConfig:
    base = dict(
        id="main",
        url="http://flatnotes:8080",
        description="main",
        exposure="public",
        credentials_file=None,
        capabilities=("read", "write", "delete"),
    )
    base.update(overrides)
    return ServerConfig(**base)


@contextlib.asynccontextmanager
async def mcp_client(mcp, token: str | None = None) -> AsyncIterator[Client]:
    """Connect an MCP client to ``mcp`` over an in-process ASGI app.

    The in-memory FastMCP transport bypasses HTTP entirely, so it cannot
    exercise ``Authorization`` headers. Running the real Streamable HTTP app
    through ``httpx2.ASGITransport`` keeps the full auth path in the test.
    """
    app = mcp.http_app(path="/mcp", json_response=True)

    def factory(*, headers=None, auth=None, follow_redirects=None, timeout=None, **_):
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
            auth=auth,
            follow_redirects=True if follow_redirects is None else follow_redirects,
            timeout=timeout,
        )

    transport = StreamableHttpTransport(
        "http://testserver/mcp",
        auth=token if token is not None else "invalid-token",
        httpx_client_factory=factory,
    )
    async with app.router.lifespan_context(app), Client(transport) as client:
        yield client
