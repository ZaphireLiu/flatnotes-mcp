"""FastMCP server: authentication, authorization, tools and resources.

The server is intentionally thin. All the interesting policy lives in two
places:

* :class:`HashedTokenVerifier` turns an inbound bearer token into an
  :class:`AccessToken` whose scopes are the flat ``{server}:{scope}`` pairs the
  token was granted (already filtered by this replica's ``MCP_EXPOSURE``).
* :func:`build_server` resolves the optional ``server`` tool argument against
  those scopes and enforces the required operation for each tool.

flatnotes credentials are never placed on the access token, never returned by
a tool and never logged.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from . import __version__
from .client import FlatnotesClient, FlatnotesError
from .config import (
    ConfigError,
    ServerConfig,
    Settings,
    TokenConfig,
    load_credentials,
    load_settings_from_env,
    sha256_hex,
)

logger = logging.getLogger(__name__)

SortField = Literal["score", "title", "lastModified"]
SortOrder = Literal["asc", "desc"]

INSTRUCTIONS = """\
Access one or more flatnotes instances (a flat, Markdown-based note app).

Notes are addressed by title. Call `list_servers` first to see which instances
this token may use and which operations it allows. When the token can reach
more than one instance you must pass `server` explicitly, because note titles
may collide across instances.
"""


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
class HashedTokenVerifier(TokenVerifier):
    """Verify inbound tokens against SHA-256 digests, then attach scopes.

    Only digests are stored on disk, so a leaked configuration file cannot be
    replayed. Plaintext tokens cannot be recovered from the configuration and
    must be recorded where they were generated.
    """

    def __init__(self, tokens: list[TokenConfig], visible_server_ids: set[str]) -> None:
        super().__init__()
        self._entries = [(t.token_sha256, t) for t in tokens]
        self._visible = set(visible_server_ids)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        digest = sha256_hex(token)
        matched = None
        for stored, entry in self._entries:
            if hmac.compare_digest(stored, digest):
                matched = entry
        if matched is None:
            logger.warning("rejected an unknown inbound token")
            return None

        scopes = [
            f"{server_id}:{scope}"
            for server_id, server_scopes in matched.servers.items()
            if server_id in self._visible
            for scope in server_scopes
        ]
        return AccessToken(
            token=token,
            client_id=matched.name,
            scopes=scopes,
            subject=matched.name,
        )


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
class Runtime:
    """Holds the per-replica state shared by every tool."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[ServerConfig], FlatnotesClient] | None = None,
    ) -> None:
        self.settings = settings
        self.visible = {s.id: s for s in settings.visible_servers()}
        self._clients: dict[str, FlatnotesClient] = {}
        self._client_factory = client_factory or self._default_client_factory

    @staticmethod
    def _default_client_factory(server: ServerConfig) -> FlatnotesClient:
        credentials = load_credentials(server)
        assert credentials is not None  # required=True above
        username, password = credentials
        return FlatnotesClient(server, username, password)

    def client(self, server_id: str) -> FlatnotesClient:
        client = self._clients.get(server_id)
        if client is None:
            server = self.visible[server_id]
            try:
                client = self._client_factory(server)
            except ConfigError as exc:
                raise ToolError(str(exc)) from exc
            self._clients[server_id] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # -- authorization ------------------------------------------------------ #
    def granted_scopes(self) -> dict[str, frozenset[str]]:
        """The ``{server: {operations}}`` map for the current request's token."""
        access = get_access_token()
        if access is None:
            raise ToolError(
                "not authenticated: send an 'Authorization: Bearer <token>' header"
            )
        granted: dict[str, set[str]] = {}
        for scope in access.scopes or []:
            server_id, _, operation = scope.partition(":")
            if not operation or server_id not in self.visible:
                continue
            granted.setdefault(server_id, set()).add(operation)
        return {k: frozenset(v) for k, v in granted.items()}

    def resolve(self, server: str | None, operation: str | tuple[str, ...]) -> str:
        """Resolve the optional ``server`` argument and check ``operation``.

        ``operation`` may be a single scope or a tuple of scopes, all of which
        must be granted for the resolved server.
        """
        operations = (operation,) if isinstance(operation, str) else tuple(operation)
        granted = self.granted_scopes()
        if not granted:
            raise ToolError(
                "this token grants no access to any server served by this replica "
                f"(visible servers: {', '.join(sorted(self.visible)) or 'none'})"
            )

        def check(server_id: str) -> str:
            missing = [op for op in operations if op not in granted[server_id]]
            if missing:
                wanted = ", ".join(repr(op) for op in missing)
                raise ToolError(
                    f"token lacks the {wanted} scope on server {server_id!r} "
                    f"(granted: {', '.join(sorted(granted[server_id]))})"
                )
            return server_id

        if server is not None:
            if server not in granted:
                raise ToolError(
                    f"unknown or unauthorised server {server!r}; available: "
                    f"{', '.join(sorted(granted))}"
                )
            return check(server)

        if len(granted) == 1:
            return check(next(iter(granted)))

        raise ToolError(
            "the 'server' argument is required when a token can reach multiple "
            f"servers; available: {', '.join(sorted(granted))}"
        )


# --------------------------------------------------------------------------- #
# Server construction
# --------------------------------------------------------------------------- #
def build_server(
    settings: Settings,
    *,
    client_factory: Callable[[ServerConfig], FlatnotesClient] | None = None,
    name: str = "flatnotes",
) -> FastMCP:
    runtime = Runtime(settings, client_factory=client_factory)
    verifier = HashedTokenVerifier(settings.tokens, set(runtime.visible))

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        logger.info(
            "serving %d of %d flatnotes instance(s) (exposure=%s, allow_delete=%s)",
            len(runtime.visible),
            len(settings.servers),
            settings.exposure,
            settings.allow_delete,
        )
        try:
            yield
        finally:
            await runtime.aclose()

    mcp: FastMCP = FastMCP(
        name=name,
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=verifier,
        lifespan=lifespan,
    )

    # -- helpers ------------------------------------------------------------ #
    async def _run(server: str | None, operation: str | tuple[str, ...], call):
        server_id = runtime.resolve(server, operation)
        client = runtime.client(server_id)
        try:
            return await call(client)
        except FlatnotesError as exc:
            raise ToolError(f"[{server_id}] {exc}") from exc

    # -- tools -------------------------------------------------------------- #
    @mcp.tool
    async def list_servers() -> list[dict[str, Any]]:
        """List the flatnotes instances this token may use on this server.

        Returns each instance's `id`, `description` and the `operations`
        (read/write/delete) the token is allowed to perform on it. Pass an
        instance's `id` as the `server` argument of the other tools.
        """
        granted = runtime.granted_scopes()
        result = []
        for server_id, operations in sorted(granted.items()):
            config = runtime.visible[server_id]
            result.append(
                {
                    "id": server_id,
                    "description": config.description,
                    "exposure": config.exposure,
                    "operations": sorted(operations),
                }
            )
        return result

    @mcp.tool
    async def search_notes(
        term: str,
        server: str | None = None,
        sort: SortField = "score",
        order: SortOrder = "desc",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search notes.

        `term` supports flatnotes syntax: plain words, quoted phrases, `#tag`
        and `tag:name`. Results are truncated to `title`, `lastModified` and
        any match highlights - use `get_note` to read the content.
        """
        return await _run(
            server,
            "read",
            lambda client: client.search(
                term, sort=sort, order=order, limit=limit
            ),
        )

    @mcp.tool
    async def list_recent_notes(
        limit: int = 20, server: str | None = None
    ) -> list[dict[str, Any]]:
        """List the most recently modified notes (newest first)."""
        return await _run(
            server, "read", lambda client: client.list_recent(limit=limit)
        )

    @mcp.tool
    async def list_tags(server: str | None = None) -> list[str]:
        """List every tag present in the index of the selected instance."""
        return await _run(server, "read", lambda client: client.get_tags())

    @mcp.tool
    async def get_note(title: str, server: str | None = None) -> dict[str, Any]:
        """Read a note's full Markdown content by exact title."""
        return await _run(server, "read", lambda client: client.get_note(title))

    @mcp.tool
    async def create_note(
        title: str, content: str = "", server: str | None = None
    ) -> dict[str, Any]:
        """Create a new note. Fails if a note with that title already exists."""
        return await _run(
            server, "write", lambda client: client.create_note(title, content)
        )

    @mcp.tool
    async def update_note(
        title: str,
        server: str | None = None,
        new_title: str | None = None,
        new_content: str | None = None,
    ) -> dict[str, Any]:
        """Rename and/or replace the content of an existing note.

        Pass `new_title` to rename, `new_content` to overwrite the body (this
        replaces the whole note - use `append_note` to add to it), or both.
        """
        if new_title is None and new_content is None:
            raise ToolError("provide at least one of new_title or new_content")
        return await _run(
            server,
            "write",
            lambda client: client.update_note(
                title, new_title=new_title, new_content=new_content
            ),
        )

    @mcp.tool
    async def append_note(
        title: str, text: str, server: str | None = None
    ) -> dict[str, Any]:
        """Append `text` to the end of an existing note (read + update).

        Requires both the `read` and `write` scopes, because the note's current
        content is fetched before it is rewritten.
        """
        return await _run(
            server, ("read", "write"), lambda client: _append(client, title, text)
        )

    @mcp.tool
    async def upload_attachment(
        filename: str,
        content_b64: str,
        server: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a binary attachment (base64 encoded) and return its URL."""
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(f"content_b64 is not valid base64: {exc}") from exc
        return await _run(
            server,
            "write",
            lambda client: client.upload_attachment(
                filename, data, content_type=content_type
            ),
        )

    @mcp.tool
    async def download_attachment(
        filename: str, server: str | None = None
    ) -> dict[str, Any]:
        """Download an attachment (base64 encoded) by filename."""
        data, content_type = await _run(
            server, "read", lambda client: client.download_attachment(filename)
        )
        return {
            "filename": filename,
            "content_type": content_type,
            "content_b64": base64.b64encode(data).decode("ascii"),
        }

    if settings.allow_delete:

        @mcp.tool
        async def delete_note(title: str, server: str | None = None) -> str:
            """Permanently delete a note. This cannot be undone.

            Only available on replicas started with MCP_ALLOW_DELETE=true and
            for tokens that hold the `delete` scope on the target instance.
            """
            server_id = runtime.resolve(server, "delete")
            client = runtime.client(server_id)
            try:
                await client.delete_note(title)
            except FlatnotesError as exc:
                raise ToolError(f"[{server_id}] {exc}") from exc
            return f"deleted {title!r} on {server_id!r}"

    # -- resources ---------------------------------------------------------- #
    @mcp.resource("servers://list", mime_type="application/json")
    async def servers_resource() -> str:
        """The instances available to this token, as JSON."""
        granted = runtime.granted_scopes()
        payload = [
            {
                "id": server_id,
                "description": runtime.visible[server_id].description,
                "operations": sorted(operations),
            }
            for server_id, operations in sorted(granted.items())
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @mcp.resource("note://{server}/{title}", mime_type="text/markdown")
    async def note_resource(server: str, title: str) -> str:
        """A single note's Markdown content."""
        try:
            note = await _run(server, "read", lambda client: client.get_note(title))
        except ToolError as exc:
            raise ResourceError(str(exc)) from exc
        return note.get("content") or ""

    return mcp


async def _append(client: FlatnotesClient, title: str, text: str) -> dict[str, Any]:
    """Read a note, append ``text`` and write it back in one tool call."""
    note = await client.get_note(title)
    content = note.get("content") or ""
    if content and not content.endswith("\n"):
        content += "\n"
    return await client.update_note(title, new_content=content + text)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings_from_env()
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc

    mcp = build_server(settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # pragma: no cover - trivial
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("OK")

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    path = os.environ.get("MCP_PATH", "/mcp")
    mcp.run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":  # pragma: no cover
    main()
