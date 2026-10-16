"""Tests for the per-instance flatnotes HTTP client."""

from __future__ import annotations

import httpx
import pytest

from flatnotes_mcp.client import FlatnotesClient, FlatnotesError
from flatnotes_mcp.config import ServerConfig


class FakeFlatnotes:
    """A tiny in-memory flatnotes API served through httpx.MockTransport."""

    def __init__(self, *, username: str = "user", password: str = "pass") -> None:
        self.username = username
        self.password = password
        self.notes: dict[str, str] = {}
        self.attachments: dict[str, bytes] = {}
        self.valid_tokens: set[str] = set()
        self.token_calls = 0
        self.attach_field_names: list[str] = []
        self.requests: list[httpx.Request] = []

    # -- transport ---------------------------------------------------------- #
    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _issue(self) -> str:
        self.token_calls += 1
        token = f"token-{self.token_calls}"
        self.valid_tokens.add(token)
        return token

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/token":
            import json

            body = json.loads(request.content)
            if (body.get("username"), body.get("password")) != (
                self.username,
                self.password,
            ):
                return httpx.Response(401, json={"detail": "login failed"})
            return httpx.Response(200, json={"access_token": self._issue()})

        if path == "/health":
            return httpx.Response(200, text="OK")

        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return httpx.Response(
                401, json={"detail": "Invalid authentication credentials"}
            )

        if path == "/api/search":
            term = request.url.params.get("term")
            results = [
                {"title": title, "lastModified": 1.0}
                for title in self.notes
                if term == "*" or term in title
            ]
            return httpx.Response(200, json=results)

        if path == "/api/tags":
            return httpx.Response(200, json=["one", "two"])

        if path == "/api/notes" and request.method == "POST":
            import json

            body = json.loads(request.content)
            title = body["title"]
            if title in self.notes:
                return httpx.Response(409, json={"detail": "Note already exists"})
            self.notes[title] = body.get("content") or ""
            return httpx.Response(
                200, json={"title": title, "content": self.notes[title], "lastModified": 1.0}
            )

        if path.startswith("/api/notes/"):
            import json
            import urllib.parse

            title = urllib.parse.unquote(path[len("/api/notes/") :])
            if title not in self.notes:
                return httpx.Response(404, json={"detail": "Note not found"})
            if request.method == "GET":
                return httpx.Response(
                    200, json={"title": title, "content": self.notes[title], "lastModified": 1.0}
                )
            if request.method == "PATCH":
                body = json.loads(request.content)
                content = self.notes.pop(title)
                new_title = body.get("newTitle") or title
                self.notes[new_title] = body.get("newContent", content)
                return httpx.Response(
                    200,
                    json={
                        "title": new_title,
                        "content": self.notes[new_title],
                        "lastModified": 2.0,
                    },
                )
            if request.method == "DELETE":
                del self.notes[title]
                return httpx.Response(200)

        if path == "/api/attachments" and request.method == "POST":
            import email

            message = email.message_from_bytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            for part in message.walk():
                name = part.get_param("name", header="content-disposition")
                if name:
                    self.attach_field_names.append(name)
                    filename = part.get_filename()
                    self.attachments[filename] = part.get_payload(decode=True)
                    return httpx.Response(
                        200, json={"filename": filename, "url": f"attachments/{filename}"}
                    )
            return httpx.Response(400, json={"detail": "no file"})

        return httpx.Response(404, json={"detail": f"unhandled {request.method} {path}"})


def make_client(fake: FakeFlatnotes, **overrides) -> FlatnotesClient:
    server = ServerConfig(
        id="main",
        url="http://flatnotes:8080",
        capabilities=("read", "write", "delete"),
        **overrides,
    )
    return FlatnotesClient(
        server, fake.username, fake.password, transport=fake.transport
    )


async def test_authentication_is_cached():
    fake = FakeFlatnotes()
    async with make_client(fake) as client:
        assert await client.get_tags() == ["one", "two"]
        assert await client.get_tags() == ["one", "two"]
    assert fake.token_calls == 1


async def test_401_triggers_reauthentication():
    fake = FakeFlatnotes()
    async with make_client(fake) as client:
        await client.get_tags()
        # Invalidate server-side, as if the token expired.
        fake.valid_tokens.clear()
        assert await client.get_tags() == ["one", "two"]
    assert fake.token_calls == 2


async def test_bad_credentials_surface_a_clear_error():
    fake = FakeFlatnotes(username="other")
    server = ServerConfig(
        id="main", url="http://flatnotes:8080", capabilities=("read",)
    )
    client = FlatnotesClient(
        server, "user", "wrong-password", transport=fake.transport
    )
    async with client:
        with pytest.raises(FlatnotesError, match="authentication .* failed"):
            await client.get_tags()


async def test_error_detail_is_propagated():
    fake = FakeFlatnotes()
    async with make_client(fake) as client:
        with pytest.raises(FlatnotesError) as excinfo:
            await client.get_note("missing")
    assert excinfo.value.status_code == 404
    assert "Note not found" in excinfo.value.message


async def test_titles_are_url_encoded():
    fake = FakeFlatnotes()
    async with make_client(fake) as client:
        await client.create_note("a note #1", "hello")
        note = await client.get_note("a note #1")
    assert note["content"] == "hello"
    assert fake.requests[-1].url.raw_path == b"/api/notes/a%20note%20%231"


async def test_full_crud_roundtrip():
    fake = FakeFlatnotes()
    async with make_client(fake) as client:
        await client.create_note("n", "one")
        updated = await client.update_note("n", new_title="n2", new_content="two")
        assert updated["title"] == "n2"
        assert (await client.list_recent())[0]["title"] == "n2"
        await client.delete_note("n2")
        assert fake.notes == {}


async def test_attachment_is_uploaded_under_the_file_field():
    fake = FakeFlatnotes()
    async with make_client(fake) as client:
        result = await client.upload_attachment("pic.png", b"\x89PNG")
    assert result["filename"] == "pic.png"
    assert fake.attach_field_names == ["file"]
    assert fake.attachments["pic.png"] == b"\x89PNG"
