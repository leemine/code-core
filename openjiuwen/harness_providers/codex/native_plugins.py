# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Validation for host-authorized Codex native plugin snapshots.

The Codex CLI remains the plugin loader.  This module only admits a prepared,
session-isolated CODEX_HOME and verifies that the CLI loaded the exact packages
the host authorized.  It deliberately does not install, update, or translate
plugins.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from openjiuwen.harness_protocol import HarnessProtocolError

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IDENTITY_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SUPPORTED_COMPONENTS = frozenset({"skills", "mcp"})
_UNSUPPORTED_MANIFEST_COMPONENTS = ("hooks", "commands", "agents", "apps", "appTemplates")


@dataclass(frozen=True, slots=True)
class CodexNativePluginConfig:
    """One fixed, pre-installed native plugin selected by the host.

    ``source_type`` and ``source_locator`` identify the source reported by the
    native loader.  ``content_sha256`` covers the installed package directory,
    not credentials or mutable runtime state.
    """

    plugin_id: str
    source_type: str
    source_locator: str
    version: str
    content_sha256: str
    enabled: bool = True
    required_components: tuple[str, ...] = ("skills", "mcp")
    mcp_server_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("plugin_id", "source_type", "source_locator", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"Codex native plugin {name} must be a non-empty normalized string")
        if self.plugin_id.count("@") != 1 or any(not part for part in self.plugin_id.split("@")):
            raise ValueError("Codex native plugin_id must be '<name>@<marketplace>'")
        if any(not _IDENTITY_PART_RE.fullmatch(part) for part in self.plugin_id.split("@")):
            raise ValueError("Codex native plugin_id contains an unsupported path or identifier component")
        if not _IDENTITY_PART_RE.fullmatch(self.version):
            raise ValueError("Codex native plugin version must be a path-safe identifier")
        if self.source_type != "local":
            raise ValueError("Codex C1 native plugins currently require a prepared local marketplace source")
        if not Path(self.source_locator).is_absolute():
            raise ValueError("Codex local native plugin source_locator must be an absolute path")
        if not isinstance(self.content_sha256, str) or not _DIGEST_RE.fullmatch(self.content_sha256):
            raise ValueError("Codex native plugin content_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.enabled, bool):
            raise TypeError("Codex native plugin enabled must be a boolean")
        for name in ("required_components", "mcp_server_names"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)) or any(
                not isinstance(item, str) or not item or item != item.strip() for item in value
            ):
                raise TypeError(f"Codex native plugin {name} must be an array of normalized strings")
            if len(set(value)) != len(value):
                raise ValueError(f"Codex native plugin {name} must not contain duplicates")
            object.__setattr__(self, name, tuple(value))
        if any(not _IDENTITY_PART_RE.fullmatch(name) for name in self.mcp_server_names):
            raise ValueError("Codex native plugin MCP server names must be path-safe identifiers")
        unsupported = set(self.required_components) - _SUPPORTED_COMPONENTS
        if unsupported:
            raise ValueError(f"unsupported Codex native plugin components: {', '.join(sorted(unsupported))}")
        if "mcp" not in self.required_components and self.mcp_server_names:
            raise ValueError("Codex native plugin MCP server names require the mcp component")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CodexNativePluginConfig":
        if not isinstance(value, Mapping):
            raise TypeError("Codex native plugin configuration must be an object")
        known = {
            "plugin_id", "source_type", "source_locator", "version", "content_sha256",
            "enabled", "required_components", "mcp_server_names",
        }
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown Codex native plugin fields: {', '.join(unknown)}")
        return cls(**dict(value))  # type: ignore[arg-type]


class _PluginResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


def native_plugin_overrides(plugins: tuple[CodexNativePluginConfig, ...] | None) -> tuple[str, ...]:
    """Pin the global native plugin feature without configuring a plugin by hand."""
    if plugins is None:
        return ()
    enabled = any(plugin.enabled for plugin in plugins)
    return (f"features.plugins={'true' if enabled else 'false'}", "features.remote_plugin=false")


def _codex_home(env: Mapping[str, str]) -> Path:
    raw = env.get("CODEX_HOME")
    if not raw:
        raise HarnessProtocolError("managed Codex native plugins require an explicit isolated CODEX_HOME")
    path = Path(raw)
    if not path.is_absolute():
        raise HarnessProtocolError("managed Codex native plugin CODEX_HOME must be an absolute path")
    if not path.is_dir():
        raise HarnessProtocolError("managed Codex native plugin CODEX_HOME does not exist")
    return path.resolve()


def _package_root(codex_home: Path, plugin: CodexNativePluginConfig) -> Path:
    name, marketplace = plugin.plugin_id.split("@", 1)
    return (codex_home / "plugins" / "cache" / marketplace / name / plugin.version).resolve()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file() or path.is_symlink())
    if not files:
        raise HarnessProtocolError(f"Codex native plugin package is empty: {root}")
    for path in files:
        if path.is_symlink():
            raise HarnessProtocolError(f"Codex native plugin package contains a symlink: {path}")
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def native_plugin_content_digest(root: str | Path) -> str:
    """Return the deterministic package digest used by native plugin snapshots."""
    path = Path(root)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Codex native plugin package root must be an existing absolute directory")
    return _tree_digest(path.resolve())


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessProtocolError(f"cannot read Codex native plugin {label}: {path}") from exc
    if not isinstance(value, dict):
        raise HarnessProtocolError(f"Codex native plugin {label} must be an object: {path}")
    return value


def _manifest_path(root: Path, value: Any, *, default: str, label: str) -> Path:
    raw = default if value is None else value
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise HarnessProtocolError(f"Codex native plugin {label} path must be a relative string")
    resolved = (root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise HarnessProtocolError(f"Codex native plugin {label} path escapes the package")
    return resolved


def validate_native_plugin_packages(
    plugins: tuple[CodexNativePluginConfig, ...] | None,
    *,
    env: Mapping[str, str],
    protocol_mcp_names: tuple[str, ...] = (),
) -> str | None:
    """Validate prepared package bytes and C1 component boundaries."""
    if plugins is None:
        return None
    codex_home = _codex_home(env)
    if len({plugin.plugin_id for plugin in plugins}) != len(plugins):
        raise HarnessProtocolError("Codex native plugin IDs must be unique")
    native_mcp_names: set[str] = set()
    fingerprint = hashlib.sha256()
    for plugin in sorted(plugins, key=lambda item: item.plugin_id):
        source = Path(plugin.source_locator)
        if not source.is_dir():
            raise HarnessProtocolError(f"Codex native plugin source is unavailable: {plugin.plugin_id}")
        root = _package_root(codex_home, plugin)
        expected_parent = codex_home / "plugins" / "cache"
        if not root.is_relative_to(expected_parent) or not root.is_dir():
            raise HarnessProtocolError(
                f"Codex native plugin package is missing or outside CODEX_HOME: {plugin.plugin_id}"
            )
        actual_digest = _tree_digest(root)
        if actual_digest != plugin.content_sha256:
            raise HarnessProtocolError(f"Codex native plugin content digest changed: {plugin.plugin_id}")
        manifest = _load_json(root / ".codex-plugin" / "plugin.json", label="manifest")
        name, _ = plugin.plugin_id.split("@", 1)
        if manifest.get("name") != name or manifest.get("version") != plugin.version:
            raise HarnessProtocolError(f"Codex native plugin manifest identity mismatch: {plugin.plugin_id}")
        forbidden = [key for key in _UNSUPPORTED_MANIFEST_COMPONENTS if manifest.get(key)]
        if forbidden:
            raise HarnessProtocolError(
                f"Codex native plugin {plugin.plugin_id} requires unsupported C2 components: {', '.join(forbidden)}"
            )
        if "skills" in plugin.required_components:
            skills = _manifest_path(root, manifest.get("skills"), default="skills", label="Skills")
            if not skills.is_dir() or not any(skills.rglob("SKILL.md")):
                raise HarnessProtocolError(f"Codex native plugin has no required Skills: {plugin.plugin_id}")
        if "mcp" in plugin.required_components:
            mcp_path = _manifest_path(
                root,
                manifest.get("mcpServers"),
                default=".mcp.json",
                label="MCP configuration",
            )
            servers = _load_json(mcp_path, label="MCP configuration").get("mcpServers")
            if not isinstance(servers, dict) or not servers:
                raise HarnessProtocolError(f"Codex native plugin has no required MCP servers: {plugin.plugin_id}")
            actual_names = set(servers)
            if actual_names != set(plugin.mcp_server_names):
                raise HarnessProtocolError(f"Codex native plugin MCP server snapshot changed: {plugin.plugin_id}")
            overlap = native_mcp_names & actual_names
            if overlap:
                raise HarnessProtocolError(
                    f"Codex native plugin MCP server name conflict: {', '.join(sorted(overlap))}"
                )
            native_mcp_names.update(actual_names)
        snapshot = {
            "plugin_id": plugin.plugin_id,
            "source_type": plugin.source_type,
            "source_locator": str(Path(plugin.source_locator).resolve()),
            "version": plugin.version,
            "content_sha256": actual_digest,
            "enabled": plugin.enabled,
            "required_components": plugin.required_components,
            "mcp_server_names": plugin.mcp_server_names,
        }
        fingerprint.update(json.dumps(snapshot, sort_keys=True).encode())
    protocol_names = {name.replace("-", "_") for name in protocol_mcp_names}
    overlap = native_mcp_names & protocol_names
    if overlap:
        raise HarnessProtocolError(
            f"Codex native plugin MCP conflicts with a host MCP server: {', '.join(sorted(overlap))}"
        )
    return fingerprint.hexdigest()


def _source_locator(source: Mapping[str, Any]) -> tuple[str, str]:
    source_type = str(source.get("type") or "")
    for key in ("path", "url", "repo", "reference"):
        value = source.get(key)
        if isinstance(value, str) and value:
            if source_type == "local" and key == "path":
                value = str(Path(value).resolve())
            return source_type, value
    return source_type, ""


def _expected_source(plugin: CodexNativePluginConfig) -> tuple[str, str]:
    locator = plugin.source_locator
    if plugin.source_type == "local":
        locator = str(Path(locator).resolve())
    return plugin.source_type, locator


async def validate_native_plugin_inventory(
    client: Any,
    plugins: tuple[CodexNativePluginConfig, ...] | None,
    *,
    cwd: str | None,
) -> None:
    """Ask the native loader to confirm exact enablement and components."""
    if plugins is None:
        return
    await client._ensure_initialized()
    response = await client._client.request(
        "plugin/list",
        {"cwds": [cwd] if cwd else [], "marketplaceKinds": ["local"]},
        response_model=_PluginResponse,
    )
    payload = response.model_dump(mode="json")
    summaries: dict[str, Mapping[str, Any]] = {}
    for marketplace in payload.get("marketplaces") or []:
        if not isinstance(marketplace, Mapping):
            continue
        for summary in marketplace.get("plugins") or []:
            if isinstance(summary, Mapping) and isinstance(summary.get("id"), str):
                resolved = dict(summary)
                resolved["_marketplace_path"] = marketplace.get("path")
                summaries[summary["id"]] = resolved
    expected_ids = {plugin.plugin_id for plugin in plugins}
    unexpected_enabled = sorted(
        plugin_id for plugin_id, summary in summaries.items()
        if summary.get("installed") is True and summary.get("enabled") is True and plugin_id not in expected_ids
    )
    if unexpected_enabled:
        raise HarnessProtocolError(
            f"unapproved Codex native plugins are enabled: {', '.join(unexpected_enabled)}"
        )
    for plugin in plugins:
        summary = summaries.get(plugin.plugin_id)
        if (summary is None or summary.get("installed") is not True) and not plugin.enabled:
            continue
        if summary is None or summary.get("installed") is not True:
            raise HarnessProtocolError(f"required Codex native plugin is not installed: {plugin.plugin_id}")
        if summary.get("enabled") is not plugin.enabled:
            raise HarnessProtocolError(f"Codex native plugin enablement mismatch: {plugin.plugin_id}")
        actual_version = summary.get("localVersion") or summary.get("version")
        if actual_version != plugin.version:
            raise HarnessProtocolError(f"Codex native plugin version mismatch: {plugin.plugin_id}")
        source = summary.get("source")
        if not isinstance(source, Mapping) or _source_locator(source) != _expected_source(plugin):
            raise HarnessProtocolError(f"Codex native plugin source mismatch: {plugin.plugin_id}")
        if not plugin.enabled:
            continue
        read = await client._client.request(
            "plugin/read",
            {
                "marketplacePath": str(summary.get("_marketplace_path") or ""),
                "pluginName": plugin.plugin_id.split("@", 1)[0],
            },
            response_model=_PluginResponse,
        )
        detail = read.model_dump(mode="json").get("plugin") or {}
        if not isinstance(detail, Mapping):
            raise HarnessProtocolError(f"Codex native plugin details are unavailable: {plugin.plugin_id}")
        if detail.get("hooks"):
            raise HarnessProtocolError(f"Codex native plugin hooks are outside C1: {plugin.plugin_id}")
        if "skills" in plugin.required_components and not detail.get("skills"):
            raise HarnessProtocolError(f"Codex native plugin Skills were not loaded: {plugin.plugin_id}")
        if "mcp" in plugin.required_components and set(detail.get("mcpServers") or ()) != set(plugin.mcp_server_names):
            raise HarnessProtocolError(f"Codex native plugin MCP inventory changed: {plugin.plugin_id}")


async def validate_native_plugin_mcp_runtime(
    client: Any,
    plugins: tuple[CodexNativePluginConfig, ...] | None,
    *,
    thread_id: str,
) -> None:
    """Confirm every required native MCP server started on the new thread."""
    if plugins is None:
        return
    expected = {
        name
        for plugin in plugins
        if plugin.enabled and "mcp" in plugin.required_components
        for name in plugin.mcp_server_names
    }
    if not expected:
        return
    response = await client._client.request(
        "mcpServerStatus/list", {"threadId": thread_id}, response_model=_PluginResponse,
    )
    payload = response.model_dump(mode="json")
    available = {
        item.get("name") for item in payload.get("data") or []
        if isinstance(item, Mapping) and item.get("tools")
    }
    missing = sorted(expected - available)
    if missing:
        raise HarnessProtocolError(f"required Codex native plugin MCP servers did not start: {', '.join(missing)}")


__all__ = [
    "CodexNativePluginConfig",
    "native_plugin_content_digest",
    "native_plugin_overrides",
    "validate_native_plugin_inventory",
    "validate_native_plugin_mcp_runtime",
    "validate_native_plugin_packages",
]
