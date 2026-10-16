"""Tests for configuration parsing and validation."""

from __future__ import annotations

import pytest
import yaml

from flatnotes_mcp.config import (
    ConfigError,
    parse_env_file,
    parse_servers,
    parse_tokens,
    sha256_hex,
)

SERVERS = """
servers:
  - id: main
    url: http://flatnotes:8080
    capabilities: [read, write, delete]
"""


def _servers(text: str = SERVERS):
    return parse_servers(yaml.safe_load(text))


def _tokens(text: str, servers):
    return parse_tokens(yaml.safe_load(text), servers)


def test_parse_servers_defaults():
    (server,) = _servers()
    assert server.id == "main"
    assert server.exposure == "internal"
    assert server.capabilities == ("read", "write", "delete")
    assert server.base_url == "http://flatnotes:8080"


def test_capabilities_are_ordered_canonically():
    servers = _servers(
        """
        servers:
          - id: main
            url: http://x:8080
            capabilities: [delete, read, write]
        """
    )
    assert servers[0].capabilities == ("read", "write", "delete")


def test_path_prefix_is_normalised():
    servers = _servers(
        """
        servers:
          - id: main
            url: http://x:8080/
            path_prefix: notes/
        """
    )
    assert servers[0].base_url == "http://x:8080/notes"


@pytest.mark.parametrize(
    "text, message",
    [
        ("servers: []", "non-empty"),
        (
            "servers:\n  - url: http://x:8080\n",
            "'id' is required",
        ),
        (
            "servers:\n  - id: bad id\n    url: http://x:8080\n",
            "'id' must match",
        ),
        (
            "servers:\n  - id: a\n    url: ftp://x\n",
            "http://",
        ),
        (
            "servers:\n  - id: a\n    url: http://x\n    exposure: secret\n",
            "'exposure' must be",
        ),
        (
            "servers:\n  - id: a\n    url: http://x\n    capabilities: [admin]\n",
            "unknown capability",
        ),
        (
            "servers:\n  - id: a\n    url: http://x\n  - id: a\n    url: http://y\n",
            "duplicate server id",
        ),
    ],
)
def test_parse_servers_rejects_bad_input(text: str, message: str):
    with pytest.raises(ConfigError, match=message):
        _servers(text)


def test_parse_tokens_expands_scopes(monkeypatch):
    servers = _servers()
    tokens = _tokens(
        f"""
        tokens:
          - name: t
            token_sha256: {sha256_hex('secret')}
            servers:
              main: [read, write]
        """,
        servers,
    )
    assert tokens[0].scopes == ("main:read", "main:write")


def test_token_scope_must_fit_capabilities():
    servers = _servers(
        """
        servers:
          - id: ro
            url: http://x:8080
            capabilities: [read]
        """
    )
    with pytest.raises(ConfigError, match="exceeds capabilities"):
        _tokens(
            f"""
            tokens:
              - name: t
                token_sha256: {sha256_hex('secret')}
                servers:
                  ro: [write]
            """,
            servers,
        )


def test_token_unknown_server_is_rejected():
    with pytest.raises(ConfigError, match="unknown server"):
        _tokens(
            f"""
            tokens:
              - name: t
                token_sha256: {sha256_hex('secret')}
                servers:
                  nope: [read]
            """,
            _servers(),
        )


def test_token_digest_must_be_hex():
    with pytest.raises(ConfigError, match="token_sha256"):
        _tokens(
            """
            tokens:
              - name: t
                token_sha256: not-a-digest
                servers:
                  main: [read]
            """,
            _servers(),
        )


def test_duplicate_digest_is_rejected():
    digest = sha256_hex("secret")
    with pytest.raises(ConfigError, match="collides"):
        _tokens(
            f"""
            tokens:
              - name: a
                token_sha256: {digest}
                servers:
                  main: [read]
              - name: b
                token_sha256: {digest}
                servers:
                  main: [read]
            """,
            _servers(),
        )


def test_parse_env_file_handles_quotes_exports_and_comments():
    values = parse_env_file(
        """
        # comment
        FLATNOTES_USERNAME=alice
        export FLATNOTES_PASSWORD="s3 cret"
        FLATNOTES_AUTH_TYPE=password
        IGNORED
        """
    )
    assert values["FLATNOTES_USERNAME"] == "alice"
    assert values["FLATNOTES_PASSWORD"] == "s3 cret"
    assert "IGNORED" not in values
