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
from openjiuwen.harness_providers.native_plugin_snapshot import (
    NativePluginSnapshotError,
    native_plugin_tree_digest,
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IDENTITY_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SUPPORTED_COMPONENTS = frozenset({"skills", "mcp", "hooks"})
_UNSUPPORTED_MANIFEST_COMPONENTS = ("commands", "agents", "apps", "appTemplates")
_HOOK_EVENTS = {
    "PreToolUse": "preToolUse",
    "PermissionRequest": "permissionRequest",
    "PostToolUse": "postToolUse",
    "PreCompact": "preCompact",
    "PostCompact": "postCompact",
    "SessionStart": "sessionStart",
    "UserPromptSubmit": "userPromptSubmit",
    "SubagentStart": "subagentStart",
    "SubagentStop": "subagentStop",
    "Stop": "stop",
}


@dataclass(frozen=True, slots=True)
class CodexNativeHookConfig:
    """One host-reviewed command hook at its exact Codex trust hash.

    Codex owns trust persistence.  The host never writes that private store;
    it only requires ``hooks/list`` to report the definition the user reviewed
    as trusted before a Session may start.
    """

    key: str
    event_name: str
    current_hash: str
    command: str
    command_windows: str | None = None
    matcher: str | None = None
    async_mode: bool = False
    timeout_s: int = 600
    status_message: str | None = None
    additional_context_limit: int = 2500

    def __post_init__(self) -> None:
        for name in ("key", "event_name", "current_hash", "command"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value.encode("utf-8")) > 4096
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError(f"Codex native hook {name} must be a bounded normalized string")
        if self.event_name not in _HOOK_EVENTS.values():
            raise ValueError(f"unsupported Codex native hook event: {self.event_name}")
        if self.matcher is not None and (not isinstance(self.matcher, str) or len(self.matcher.encode("utf-8")) > 4096):
            raise TypeError("Codex native hook matcher must be a bounded string or null")
        if self.command_windows is not None and (
            not isinstance(self.command_windows, str)
            or not self.command_windows
            or len(self.command_windows.encode("utf-8")) > 4096
        ):
            raise TypeError("Codex native hook command_windows must be a bounded string or null")
        if not isinstance(self.async_mode, bool):
            raise TypeError("Codex native hook async_mode must be a boolean")
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, int) or self.timeout_s < 0:
            raise ValueError("Codex native hook timeout_s must be a non-negative integer")
        if self.status_message is not None and (
            not isinstance(self.status_message, str) or len(self.status_message.encode("utf-8")) > 4096
        ):
            raise TypeError("Codex native hook status_message must be a bounded string or null")
        if (
            isinstance(self.additional_context_limit, bool)
            or not isinstance(self.additional_context_limit, int)
            or self.additional_context_limit < 0
        ):
            raise ValueError("Codex native hook additional_context_limit must be a non-negative integer")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CodexNativeHookConfig":
        if not isinstance(value, Mapping):
            raise TypeError("Codex native hook configuration must be an object")
        known = {
            "key",
            "event_name",
            "current_hash",
            "command",
            "command_windows",
            "matcher",
            "async_mode",
            "timeout_s",
            "status_message",
            "additional_context_limit",
        }
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown Codex native hook fields: {', '.join(unknown)}")
        return cls(**dict(value))  # type: ignore[arg-type]


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
    hooks: tuple[CodexNativeHookConfig, ...] = ()

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
            raise ValueError("Codex native plugins currently require a prepared local marketplace source")
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
        if not isinstance(self.hooks, (list, tuple)) or any(
            not isinstance(hook, CodexNativeHookConfig) for hook in self.hooks
        ):
            raise TypeError("Codex native plugin hooks must be an array of CodexNativeHookConfig values")
        if len({hook.key for hook in self.hooks}) != len(self.hooks):
            raise ValueError("Codex native plugin hook keys must be unique")
        object.__setattr__(self, "hooks", tuple(self.hooks))
        if "hooks" not in self.required_components and self.hooks:
            raise ValueError("Codex native plugin hook snapshots require the hooks component")
        if "hooks" in self.required_components and not self.hooks:
            raise ValueError("Codex native plugin hooks component requires reviewed hook snapshots")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CodexNativePluginConfig":
        if not isinstance(value, Mapping):
            raise TypeError("Codex native plugin configuration must be an object")
        known = {
            "plugin_id",
            "source_type",
            "source_locator",
            "version",
            "content_sha256",
            "enabled",
            "required_components",
            "mcp_server_names",
            "hooks",
        }
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown Codex native plugin fields: {', '.join(unknown)}")
        values = dict(value)
        if "hooks" in values:
            raw_hooks = values["hooks"]
            if not isinstance(raw_hooks, (list, tuple)):
                raise TypeError("Codex native plugin hooks must be an array")
            values["hooks"] = tuple(CodexNativeHookConfig.from_mapping(hook) for hook in raw_hooks)
        return cls(**values)  # type: ignore[arg-type]


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
    try:
        return native_plugin_tree_digest(root)
    except NativePluginSnapshotError as exc:
        raise HarnessProtocolError(f"invalid Codex native plugin package: {exc}") from exc


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


def _hook_paths(root: Path, value: Any) -> tuple[Path, ...]:
    raw = "hooks/hooks.json" if value is None else value
    values = (raw,) if isinstance(raw, str) else tuple(raw) if isinstance(raw, list) else ()
    if not values or any(not isinstance(item, str) for item in values):
        raise HarnessProtocolError("managed Codex native plugin hooks require relative JSON path declarations")
    paths = tuple(_manifest_path(root, item, default="hooks/hooks.json", label="hooks") for item in values)
    if len(set(paths)) != len(paths):
        raise HarnessProtocolError("Codex native plugin hook paths must be unique")
    return paths


def _package_hook_definitions(root: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    definitions: list[dict[str, Any]] = []
    for path in _hook_paths(root, manifest.get("hooks")):
        configured = _load_json(path, label="hooks").get("hooks")
        if not isinstance(configured, Mapping):
            raise HarnessProtocolError(f"Codex native plugin hooks must contain a hooks object: {path}")
        for event, groups in configured.items():
            event_name = _HOOK_EVENTS.get(str(event))
            if event_name is None or not isinstance(groups, list):
                raise HarnessProtocolError(f"unsupported Codex native plugin hook event: {event}")
            for group in groups:
                if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
                    raise HarnessProtocolError("Codex native plugin hook matcher group is invalid")
                matcher = group.get("matcher")
                if matcher is not None and not isinstance(matcher, str):
                    raise HarnessProtocolError("Codex native plugin hook matcher must be a string")
                for handler in group["hooks"]:
                    if not isinstance(handler, Mapping) or handler.get("type") != "command":
                        raise HarnessProtocolError("Codex native plugins admit only command hook handlers")
                    command = handler.get("command")
                    if not isinstance(command, str) or not command.strip():
                        raise HarnessProtocolError("Codex native plugin hook command is required")
                    timeout = handler.get("timeout", handler.get("timeoutSec", 600))
                    async_mode = handler.get("async", False)
                    status_message = handler.get("statusMessage")
                    command_windows = handler.get("commandWindows")
                    additional_context_limit = handler.get("additionalContextLimit", 2500)
                    if (
                        isinstance(timeout, bool)
                        or not isinstance(timeout, int)
                        or timeout < 0
                        or not isinstance(async_mode, bool)
                        or status_message is not None
                        and not isinstance(status_message, str)
                        or command_windows is not None
                        and not isinstance(command_windows, str)
                        or isinstance(additional_context_limit, bool)
                        or not isinstance(additional_context_limit, int)
                        or additional_context_limit < 0
                    ):
                        raise HarnessProtocolError("Codex native plugin hook handler settings are invalid")
                    unsupported = set(handler) - {
                        "type",
                        "command",
                        "commandWindows",
                        "timeout",
                        "timeoutSec",
                        "async",
                        "statusMessage",
                        "additionalContextLimit",
                    }
                    if unsupported:
                        raise HarnessProtocolError(
                            f"unsupported Codex native plugin hook settings: {', '.join(sorted(unsupported))}"
                        )
                    definitions.append(
                        {
                            "event_name": event_name,
                            "command": command,
                            "command_windows": command_windows,
                            "matcher": matcher,
                            "async_mode": async_mode,
                            "timeout_s": timeout,
                            "status_message": status_message,
                            "additional_context_limit": additional_context_limit,
                        }
                    )
    return tuple(definitions)


def _configured_hook_definitions(plugin: CodexNativePluginConfig) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "event_name": hook.event_name,
            "command": hook.command,
            "command_windows": hook.command_windows,
            "matcher": hook.matcher,
            "async_mode": hook.async_mode,
            "timeout_s": hook.timeout_s,
            "status_message": hook.status_message,
            "additional_context_limit": hook.additional_context_limit,
        }
        for hook in plugin.hooks
    )


def _definition_sort_key(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _is_path_inside(path: Path, root: Path) -> bool:
    return path.is_absolute() and path.resolve().is_relative_to(root)


def validate_native_plugin_packages(
    plugins: tuple[CodexNativePluginConfig, ...] | None,
    *,
    env: Mapping[str, str],
    protocol_mcp_names: tuple[str, ...] = (),
) -> str | None:
    """Validate prepared package bytes and admitted native component boundaries."""
    if plugins is None:
        return None
    codex_home = _codex_home(env)
    if len({plugin.plugin_id for plugin in plugins}) != len(plugins):
        raise HarnessProtocolError("Codex native plugin IDs must be unique")
    native_mcp_names: set[str] = set()
    native_hook_keys: set[str] = set()
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
                f"Codex native plugin {plugin.plugin_id} requires unsupported components: {', '.join(forbidden)}"
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
        if "hooks" in plugin.required_components:
            overlap = native_hook_keys & {hook.key for hook in plugin.hooks}
            if overlap:
                raise HarnessProtocolError(f"Codex native plugin hook key conflict: {', '.join(sorted(overlap))}")
            native_hook_keys.update(hook.key for hook in plugin.hooks)
            actual_hooks = _package_hook_definitions(root, manifest)
            expected_hooks = _configured_hook_definitions(plugin)
            if sorted(actual_hooks, key=_definition_sort_key) != sorted(expected_hooks, key=_definition_sort_key):
                raise HarnessProtocolError(f"Codex native plugin hook definition snapshot changed: {plugin.plugin_id}")
        elif manifest.get("hooks") or (root / "hooks/hooks.json").exists():
            raise HarnessProtocolError(
                f"Codex native plugin {plugin.plugin_id} declares hooks without an authorized snapshot"
            )
        snapshot = {
            "plugin_id": plugin.plugin_id,
            "source_type": plugin.source_type,
            "source_locator": str(Path(plugin.source_locator).resolve()),
            "version": plugin.version,
            "content_sha256": actual_digest,
            "enabled": plugin.enabled,
            "required_components": plugin.required_components,
            "mcp_server_names": plugin.mcp_server_names,
            "hooks": [
                {
                    "key": hook.key,
                    "event_name": hook.event_name,
                    "current_hash": hook.current_hash,
                    "command": hook.command,
                    "command_windows": hook.command_windows,
                    "matcher": hook.matcher,
                    "async_mode": hook.async_mode,
                    "timeout_s": hook.timeout_s,
                    "status_message": hook.status_message,
                    "additional_context_limit": hook.additional_context_limit,
                }
                for hook in plugin.hooks
            ],
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
    env: Mapping[str, str],
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
        plugin_id
        for plugin_id, summary in summaries.items()
        if summary.get("installed") is True and summary.get("enabled") is True and plugin_id not in expected_ids
    )
    if unexpected_enabled:
        raise HarnessProtocolError(f"unapproved Codex native plugins are enabled: {', '.join(unexpected_enabled)}")
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
        actual_hook_summaries = {
            (str(item.get("eventName") or ""), str(item.get("key") or ""))
            for item in detail.get("hooks") or ()
            if isinstance(item, Mapping)
        }
        expected_hook_summaries = {(hook.event_name, hook.key) for hook in plugin.hooks}
        if actual_hook_summaries != expected_hook_summaries:
            raise HarnessProtocolError(f"Codex native plugin hook inventory changed: {plugin.plugin_id}")
        if "skills" in plugin.required_components and not detail.get("skills"):
            raise HarnessProtocolError(f"Codex native plugin Skills were not loaded: {plugin.plugin_id}")
        if "mcp" in plugin.required_components and set(detail.get("mcpServers") or ()) != set(plugin.mcp_server_names):
            raise HarnessProtocolError(f"Codex native plugin MCP inventory changed: {plugin.plugin_id}")
    await _validate_native_hook_inventory(client, plugins, cwd=cwd, env=env)


async def _validate_native_hook_inventory(
    client: Any,
    plugins: tuple[CodexNativePluginConfig, ...],
    *,
    cwd: str | None,
    env: Mapping[str, str],
) -> None:
    expected = {hook.key: (plugin, hook) for plugin in plugins if plugin.enabled for hook in plugin.hooks}
    if not expected:
        return
    response = await client._client.request(
        "hooks/list",
        {"cwds": [cwd] if cwd else []},
        response_model=_PluginResponse,
    )
    data = response.model_dump(mode="json").get("data") or []
    if len(data) != 1 or not isinstance(data[0], Mapping):
        raise HarnessProtocolError("Codex native plugin hook inventory is unavailable")
    entry = data[0]
    if entry.get("errors") or entry.get("warnings"):
        raise HarnessProtocolError("Codex native plugin hook inventory contains errors or warnings")
    actual = {str(item.get("key") or ""): item for item in entry.get("hooks") or () if isinstance(item, Mapping)}
    if set(actual) != set(expected):
        raise HarnessProtocolError("Codex native plugin enabled hook set changed")
    codex_home = _codex_home(env)
    for key, (plugin, hook) in expected.items():
        item = actual[key]
        source_path = Path(str(item.get("sourcePath") or ""))
        # Source paths are read back from Codex and must remain inside the exact
        # package whose bytes were already hashed before process startup.
        root = _package_root(codex_home, plugin)
        if not _is_path_inside(source_path, root):
            raise HarnessProtocolError(f"Codex native plugin hook source changed: {plugin.plugin_id}")
        plugin_id = item.get("pluginId")
        if plugin_id not in {plugin.plugin_id, plugin.plugin_id.split("@", 1)[0]}:
            raise HarnessProtocolError(f"Codex native plugin hook identity changed: {plugin.plugin_id}")
        fields = {
            "eventName": hook.event_name,
            "handlerType": "command",
            "currentHash": hook.current_hash,
            "matcher": hook.matcher,
            "timeoutSec": hook.timeout_s,
            "statusMessage": hook.status_message,
            "isManaged": False,
            "enabled": True,
            "trustStatus": "trusted",
        }
        # Codex expands PLUGIN_ROOT/PLUGIN_DATA in the reported command.  The
        # immutable package snapshot above verifies the literal command while
        # currentHash binds this exact resolved definition to the user's native
        # trust decision, so comparing the expanded display form would be both
        # redundant and installation-path dependent.
        if any(item.get(name) != value for name, value in fields.items()):
            raise HarnessProtocolError(
                f"Codex native plugin hook is not the trusted reviewed definition: {plugin.plugin_id}:{key}"
            )


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
        "mcpServerStatus/list",
        {"threadId": thread_id},
        response_model=_PluginResponse,
    )
    payload = response.model_dump(mode="json")
    available = {
        item.get("name") for item in payload.get("data") or [] if isinstance(item, Mapping) and item.get("tools")
    }
    missing = sorted(expected - available)
    if missing:
        raise HarnessProtocolError(f"required Codex native plugin MCP servers did not start: {', '.join(missing)}")


__all__ = [
    "CodexNativeHookConfig",
    "CodexNativePluginConfig",
    "native_plugin_content_digest",
    "native_plugin_overrides",
    "validate_native_plugin_inventory",
    "validate_native_plugin_mcp_runtime",
    "validate_native_plugin_packages",
]
