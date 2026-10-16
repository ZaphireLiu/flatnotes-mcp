"""Configuration loading and validation.

Two files drive the MCP server:

* ``servers.yaml`` - the (non sensitive) inventory of flatnotes instances.
* ``tokens.yaml``  - the (sensitive) inbound token -> (server, scope) matrix.

Both are validated eagerly at startup: a configuration that references an
unknown server, uses an unknown scope, or grants a scope that a server does
not declare as a capability is a hard error. This keeps the "mount as :ro"
hard boundary and the declared capabilities from silently disagreeing.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

# The three operations a token can be granted, in ascending order of power.
KNOWN_SCOPES: tuple[str, ...] = ("read", "write", "delete")

# Exposure classes. A replica advertises at most one class via MCP_EXPOSURE.
KNOWN_EXPOSURES: tuple[str, ...] = ("public", "internal")
EXPOSURE_ALL = "all"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(Exception):
    """Raised when the configuration is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class ServerConfig:
    """A single flatnotes instance."""

    id: str
    url: str
    description: str = ""
    exposure: str = "internal"
    credentials_file: str | None = None
    capabilities: tuple[str, ...] = ("read",)
    path_prefix: str = ""
    verify_tls: bool = True

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/") + self.path_prefix.rstrip("/")


@dataclass(frozen=True)
class TokenConfig:
    """An inbound MCP token and its per-server scopes."""

    name: str
    token_sha256: str
    servers: Mapping[str, tuple[str, ...]]

    @property
    def scopes(self) -> tuple[str, ...]:
        """The flat ``{server}:{scope}`` scope list placed on the access token."""
        return tuple(
            f"{server_id}:{scope}"
            for server_id, scopes in self.servers.items()
            for scope in scopes
        )


@dataclass
class Settings:
    """Everything the server needs to run, resolved from files + environment."""

    servers: list[ServerConfig]
    tokens: list[TokenConfig]
    exposure: str = EXPOSURE_ALL
    allow_delete: bool = True

    @property
    def servers_by_id(self) -> dict[str, ServerConfig]:
        return {server.id: server for server in self.servers}

    def visible_servers(self) -> list[ServerConfig]:
        """Servers this replica is allowed to talk to (MCP_EXPOSURE filter)."""
        if self.exposure == EXPOSURE_ALL:
            return list(self.servers)
        return [s for s in self.servers if s.exposure == self.exposure]


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _load_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - passthrough of OS error text
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


# --------------------------------------------------------------------------- #
# servers.yaml
# --------------------------------------------------------------------------- #
def parse_servers(raw: Mapping, *, source: str = "servers.yaml") -> list[ServerConfig]:
    entries = raw.get("servers")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{source}: 'servers' must be a non-empty list")

    servers: list[ServerConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{source}: servers[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping")

        server_id = str(entry.get("id", "")).strip()
        if not server_id:
            raise ConfigError(f"{where}: 'id' is required")
        if not _ID_RE.match(server_id):
            raise ConfigError(
                f"{where}: 'id' must match {_ID_RE.pattern} (got {server_id!r})"
            )
        if server_id in seen:
            raise ConfigError(f"{where}: duplicate server id {server_id!r}")
        seen.add(server_id)

        url = str(entry.get("url", "")).strip()
        if not url:
            raise ConfigError(f"{where}: 'url' is required")
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"{where}: 'url' must start with http:// or https://")

        exposure = str(entry.get("exposure", "internal")).strip().lower()
        if exposure not in KNOWN_EXPOSURES:
            raise ConfigError(
                f"{where}: 'exposure' must be one of {KNOWN_EXPOSURES}, "
                f"got {exposure!r}"
            )

        raw_capabilities = entry.get("capabilities", ["read"])
        if not isinstance(raw_capabilities, list) or not raw_capabilities:
            raise ConfigError(f"{where}: 'capabilities' must be a non-empty list")
        capabilities: list[str] = []
        for scope in raw_capabilities:
            scope = str(scope).strip().lower()
            if scope not in KNOWN_SCOPES:
                raise ConfigError(
                    f"{where}: unknown capability {scope!r} "
                    f"(known: {', '.join(KNOWN_SCOPES)})"
                )
            if scope in capabilities:
                raise ConfigError(f"{where}: duplicate capability {scope!r}")
            capabilities.append(scope)
        # Order capabilities canonically so token checks read naturally.
        capabilities.sort(key=KNOWN_SCOPES.index)

        path_prefix = str(entry.get("path_prefix", "") or "").strip()
        if path_prefix and not path_prefix.startswith("/"):
            path_prefix = "/" + path_prefix
        path_prefix = path_prefix.rstrip("/")

        credentials_file = entry.get("credentials_file")
        if credentials_file is not None:
            credentials_file = str(credentials_file).strip() or None

        servers.append(
            ServerConfig(
                id=server_id,
                url=url.rstrip("/"),
                description=str(entry.get("description", "") or "").strip(),
                exposure=exposure,
                credentials_file=credentials_file,
                capabilities=tuple(capabilities),
                path_prefix=path_prefix,
                verify_tls=bool(entry.get("verify_tls", True)),
            )
        )
    return servers


# --------------------------------------------------------------------------- #
# tokens.yaml
# --------------------------------------------------------------------------- #
def parse_tokens(
    raw: Mapping,
    servers: Iterable[ServerConfig],
    *,
    source: str = "tokens.yaml",
) -> list[TokenConfig]:
    entries = raw.get("tokens")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{source}: 'tokens' must be a non-empty list")

    capabilities = {s.id: set(s.capabilities) for s in servers}
    tokens: list[TokenConfig] = []
    seen_names: set[str] = set()
    seen_hashes: set[str] = set()

    for index, entry in enumerate(entries):
        where = f"{source}: tokens[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping")

        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError(f"{where}: 'name' is required")
        if name in seen_names:
            raise ConfigError(f"{where}: duplicate token name {name!r}")
        seen_names.add(name)

        digest = str(entry.get("token_sha256", "")).strip().lower()
        if not _SHA256_RE.match(digest):
            raise ConfigError(
                f"{where}: 'token_sha256' must be a 64 character hex SHA-256 digest"
            )
        if digest in seen_hashes:
            raise ConfigError(f"{where}: token_sha256 collides with an earlier token")
        seen_hashes.add(digest)

        raw_servers = entry.get("servers")
        if not isinstance(raw_servers, dict) or not raw_servers:
            raise ConfigError(f"{where}: 'servers' must be a non-empty mapping")

        resolved: dict[str, tuple[str, ...]] = {}
        for server_id, raw_scopes in raw_servers.items():
            server_id = str(server_id)
            if server_id not in capabilities:
                raise ConfigError(
                    f"{where}: references unknown server {server_id!r} "
                    f"(known: {', '.join(sorted(capabilities))})"
                )
            if not isinstance(raw_scopes, list) or not raw_scopes:
                raise ConfigError(
                    f"{where}: scopes for {server_id!r} must be a non-empty list"
                )
            scopes: list[str] = []
            for scope in raw_scopes:
                scope = str(scope).strip().lower()
                if scope not in KNOWN_SCOPES:
                    raise ConfigError(
                        f"{where}: unknown scope {scope!r} for server {server_id!r}"
                    )
                if scope not in capabilities[server_id]:
                    raise ConfigError(
                        f"{where}: scope {scope!r} exceeds capabilities of "
                        f"{server_id!r} ({sorted(capabilities[server_id])})"
                    )
                if scope in scopes:
                    raise ConfigError(
                        f"{where}: duplicate scope {scope!r} for {server_id!r}"
                    )
                scopes.append(scope)
            scopes.sort(key=KNOWN_SCOPES.index)
            resolved[server_id] = tuple(scopes)

        tokens.append(TokenConfig(name=name, token_sha256=digest, servers=resolved))
    return tokens


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def parse_env_file(text: str) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` dotenv file (comments and blanks ignored)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_credentials(server: ServerConfig, *, required: bool = True) -> tuple[str, str] | None:
    """Return ``(username, password)`` for a flatnotes instance.

    Credentials are read from the instance's ``.env`` file (mounted read-only).
    Returns ``None`` when no credentials file is configured and ``required`` is
    false.
    """
    if not server.credentials_file:
        if required:
            raise ConfigError(
                f"server {server.id!r} has no credentials_file but one is required"
            )
        return None

    path = Path(server.credentials_file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot read credentials for server {server.id!r} "
            f"from {path}: {exc}"
        ) from exc

    values = parse_env_file(text)
    username = values.get("FLATNOTES_USERNAME") or values.get("USERNAME")
    password = values.get("FLATNOTES_PASSWORD") or values.get("PASSWORD")
    if not username or not password:
        raise ConfigError(
            f"credentials for server {server.id!r} ({path}) must define "
            "FLATNOTES_USERNAME and FLATNOTES_PASSWORD"
        )
    return username, password


def warn_if_world_readable(path: Path) -> str | None:
    """Return a warning string when a sensitive file is group/other readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        return (
            f"{path} is readable by group/other (mode {stat.S_IMODE(mode):04o}); "
            "restrict it to 600"
        )
    return None


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #
def load_settings(
    servers_file: str | os.PathLike[str],
    tokens_file: str | os.PathLike[str],
    *,
    exposure: str | None = None,
    allow_delete: bool | None = None,
) -> Settings:
    """Load, validate and merge ``servers.yaml`` + ``tokens.yaml``."""
    servers_raw = _load_yaml(Path(servers_file))
    servers = parse_servers(servers_raw, source=str(servers_file))

    tokens_raw = _load_yaml(Path(tokens_file))
    tokens = parse_tokens(tokens_raw, servers, source=str(tokens_file))

    resolved_exposure = (
        exposure if exposure is not None else os.environ.get("MCP_EXPOSURE", EXPOSURE_ALL)
    )
    resolved_exposure = (resolved_exposure or EXPOSURE_ALL).strip().lower()
    if resolved_exposure not in (*KNOWN_EXPOSURES, EXPOSURE_ALL):
        raise ConfigError(
            f"MCP_EXPOSURE must be one of {(*KNOWN_EXPOSURES, EXPOSURE_ALL)}, "
            f"got {exposure!r}"
        )

    resolved_allow_delete = (
        allow_delete
        if allow_delete is not None
        else _env_bool("MCP_ALLOW_DELETE", default=True)
    )

    return Settings(
        servers=servers,
        tokens=tokens,
        exposure=resolved_exposure,
        allow_delete=resolved_allow_delete,
    )


def load_settings_from_env() -> Settings:
    """Load settings using the standard ``MCP_*`` environment variables."""
    servers_file = os.environ.get("MCP_SERVERS_FILE", "config/servers.yaml")
    tokens_file = os.environ.get("MCP_TOKENS_FILE", "config/tokens.yaml")
    return load_settings(servers_file, tokens_file)


def sha256_hex(token: str) -> str:
    """Hash an inbound token exactly as it is stored in ``tokens.yaml``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def main() -> None:
    """Validate configuration and print a summary.

    Used as a fail-fast check (``flatnotes-mcp-check``) before starting the
    server or in CI. Exits non-zero when the configuration is invalid.
    """
    import sys

    try:
        settings = load_settings_from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    servers_file = os.environ.get("MCP_SERVERS_FILE", "config/servers.yaml")
    tokens_file = os.environ.get("MCP_TOKENS_FILE", "config/tokens.yaml")
    print(f"servers file: {servers_file}")
    print(f"tokens file:  {tokens_file}")
    print(f"MCP_EXPOSURE: {settings.exposure}")
    print(f"MCP_ALLOW_DELETE: {settings.allow_delete}")
    print(f"servers ({len(settings.servers)}):")
    visible_ids = {server.id for server in settings.visible_servers()}
    for server in settings.servers:
        visible = server.id in visible_ids
        print(
            f"  - {server.id}: {server.url} "
            f"[{','.join(server.capabilities)}] exposure={server.exposure}"
            f"{'' if visible else ' (hidden by MCP_EXPOSURE)'}"
        )
    print(f"tokens ({len(settings.tokens)}):")
    for token in settings.tokens:
        matrix = ", ".join(
            f"{sid}:{'+'.join(scopes)}" for sid, scopes in token.servers.items()
        )
        print(f"  - {token.name}: {matrix}")

    warning = warn_if_world_readable(Path(tokens_file))
    if warning:
        print(f"warning: {warning}", file=sys.stderr)

