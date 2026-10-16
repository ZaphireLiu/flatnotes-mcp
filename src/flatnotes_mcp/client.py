"""Async HTTP client for a single flatnotes instance.

Every flatnotes instance the MCP talks to gets its own :class:`FlatnotesClient`,
owning:

* its own base URL, credentials and ``httpx`` connection pool,
* a cached bearer token obtained from ``POST /api/token``,
* transparent re-authentication on ``401``.

The credentials never leave this process - they are read from a read-only
mounted ``.env`` file and are never echoed back through a tool result.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Self
from urllib.parse import quote

import httpx

from .config import ServerConfig

logger = logging.getLogger(__name__)

_ATTACHMENT_FIELD = "file"
# Refresh the bearer token a little early so an in-flight request cannot race
# the expiry.
_EXPIRY_SLACK_SECONDS = 60


class FlatnotesError(Exception):
    """An error returned by (or while talking to) a flatnotes instance."""

    def __init__(self, status_code: int | None, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        if self.status_code is None:
            return self.message
        return f"flatnotes responded {self.status_code}: {self.message}"


def _jwt_exp(token: str) -> float | None:
    """Best-effort extraction of the ``exp`` claim from a JWT.

    flatnotes issues JWTs, but we never verify the signature (that is the
    instance's job) - we only read ``exp`` to know when to re-authenticate. If
    the token is not a decodable JWT we return ``None`` and rely on 401 retries.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 - malformed tokens fall back to 401 retries
        return None


class FlatnotesClient:
    """A thin, typed wrapper around the flatnotes HTTP API."""

    def __init__(
        self,
        server: ServerConfig,
        username: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.server = server
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_expires_at: float | None = None
        self._auth_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=server.base_url,
            timeout=timeout,
            transport=transport,
            verify=server.verify_tls,
            follow_redirects=True,
        )

    # -- lifecycle ---------------------------------------------------------- #
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # -- authentication ----------------------------------------------------- #
    async def _login(self) -> str:
        response = await self._client.post(
            "/api/token",
            json={"username": self._username, "password": self._password},
        )
        if response.status_code == 401:
            raise FlatnotesError(
                401,
                f"authentication with flatnotes server {self.server.id!r} failed "
                "(check the instance's FLATNOTES_USERNAME / FLATNOTES_PASSWORD)",
            )
        self._raise_for_status(response)
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise FlatnotesError(
                None,
                f"flatnotes server {self.server.id!r} returned no access_token",
            )
        return token

    async def _get_token(self, *, force: bool = False) -> str:
        async with self._auth_lock:
            fresh = (
                self._token is not None
                and not force
                and (
                    self._token_expires_at is None
                    or time.time() < self._token_expires_at - _EXPIRY_SLACK_SECONDS
                )
            )
            if fresh:
                return self._token  # type: ignore[return-value]
            token = await self._login()
            self._token = token
            self._token_expires_at = _jwt_exp(token)
            return token

    # -- request plumbing --------------------------------------------------- #
    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        detail: str
        try:
            payload = response.json()
            detail = (
                payload.get("detail")
                if isinstance(payload, dict)
                else json.dumps(payload)
            )
            if not isinstance(detail, str):
                detail = json.dumps(detail)
        except Exception:  # noqa: BLE001 - non-JSON error bodies are common
            detail = response.text.strip() or response.reason_phrase
        raise FlatnotesError(response.status_code, detail)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
    ) -> httpx.Response:
        token = await self._get_token()
        response = await self._client.request(
            method,
            url,
            params=params,
            json=json_body,
            files=files,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            # Token expired or revoked server-side: re-authenticate once.
            logger.info(
                "flatnotes server %r rejected the cached token; re-authenticating",
                self.server.id,
            )
            token = await self._get_token(force=True)
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
                files=files,
                headers={"Authorization": f"Bearer {token}"},
            )
        self._raise_for_status(response)
        return response

    @staticmethod
    def _note_path(title: str) -> str:
        return f"/api/notes/{quote(title, safe='')}"

    # -- API surface -------------------------------------------------------- #
    async def health(self) -> str:
        response = await self._client.get("/health")
        self._raise_for_status(response)
        return response.text

    async def auth_check(self) -> str:
        response = await self._request("GET", "/api/auth-check")
        return response.text

    async def search(
        self,
        term: str,
        *,
        sort: str = "score",
        order: str = "desc",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"term": term, "sort": sort, "order": order}
        if limit is not None:
            params["limit"] = limit
        response = await self._request("GET", "/api/search", params=params)
        return response.json()

    async def list_recent(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return await self.search(
            "*", sort="lastModified", order="desc", limit=limit
        )

    async def get_tags(self) -> list[str]:
        response = await self._request("GET", "/api/tags")
        return response.json()

    async def get_note(self, title: str) -> dict[str, Any]:
        response = await self._request("GET", self._note_path(title))
        return response.json()

    async def create_note(self, title: str, content: str) -> dict[str, Any]:
        response = await self._request(
            "POST", "/api/notes", json_body={"title": title, "content": content}
        )
        return response.json()

    async def update_note(
        self,
        title: str,
        *,
        new_title: str | None = None,
        new_content: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if new_title is not None:
            body["newTitle"] = new_title
        if new_content is not None:
            body["newContent"] = new_content
        response = await self._request(
            "PATCH", self._note_path(title), json_body=body
        )
        return response.json()

    async def delete_note(self, title: str) -> None:
        await self._request("DELETE", self._note_path(title))

    async def upload_attachment(
        self, filename: str, content: bytes, *, content_type: str | None = None
    ) -> dict[str, Any]:
        files = {
            _ATTACHMENT_FIELD: (filename, content, content_type or "application/octet-stream")
        }
        response = await self._request("POST", "/api/attachments", files=files)
        return response.json()

    async def download_attachment(self, filename: str) -> tuple[bytes, str | None]:
        response = await self._request(
            "GET", f"/api/attachments/{quote(filename, safe='')}"
        )
        return response.content, response.headers.get("content-type")
