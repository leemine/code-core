# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in host source admission, separate from the CLI's command sandbox.

This rejects unsupported startup sources; it is not a process filesystem ACL.
The host supplies authorized roots and owns the isolated configuration homes.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from openjiuwen.harness_protocol import (
    HarnessContext,
    HarnessProtocolError,
    HostCapability,
    McpServerConfig,
    McpTransport,
)
from openjiuwen.harness_providers.codex.config import CodexHarnessConfig

# Restricted startup accepts only configuration with understood source behavior.
_CONFIG_KEYS = frozenset({
    "default_permissions", "permissions", "model", "model_provider", "model_providers",
    "approval_policy", "approvals_reviewer", "sandbox_mode", "sandbox_workspace_write",
    "model_reasoning_effort", "model_reasoning_summary", "model_verbosity", "service_tier",
    "project_doc_max_bytes", "allow_login_shell", "features",
})
_DISABLED_FEATURES = frozenset({
    "auth_elicitation", "memories", "mentions_v2", "plugins", "remote_control", "remote_plugin", "tool_suggest",
})
_FEATURE_KEYS = frozenset({"enable_request_compression", "default_mode_request_user_input"}) | _DISABLED_FEATURES
RESTRICTED_STARTUP_OVERRIDES = ("project_doc_max_bytes=0", "allow_login_shell=false") + tuple(
    f"features.{name}=false" for name in sorted(_DISABLED_FEATURES)
)
# 0.144.4 materializes these defaults even when no source supplied them. Only
# these exact inert values are accepted; an explicit source still cannot set them.
_EFFECTIVE_DEFAULTS = {
    "mcp_servers": {}, "plugins": {}, "marketplaces": {}, "profiles": {},
    "project_doc_fallback_filenames": [], "hide_agent_reasoning": False,
    "history": {"max_bytes": None, "persistence": "save-all"},
    "shell_environment_policy": dict.fromkeys((
        "exclude", "experimental_use_profile", "ignore_default_excludes", "include_only", "inherit", "set",
    )),
}
_BEARER_TOKEN_RE = re.compile(r"Bearer [A-Za-z0-9._~-]{32,}")
_EFFECTIVE_MCP_KEYS = frozenset({
    "url", "http_headers", "required", "startup_timeout_sec",
    "default_tools_approval_mode", "enabled_tools", "disabled_tools",
    "enabled", "environment_id", "tool_timeout_sec",
})


def _loopback_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1"}
        and port is not None
        and parsed.path.startswith("/")
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _validate_managed_mcp_server(server: McpServerConfig) -> None:
    headers = dict(server.headers)
    authorization = headers.get("Authorization", "")
    if (
        server.transport is not McpTransport.HTTP
        or not isinstance(server.url, str)
        or not _loopback_http_url(server.url)
        or set(headers) != {"Authorization"}
        or _BEARER_TOKEN_RE.fullmatch(authorization) is None
    ):
        raise HarnessProtocolError(
            "Codex restricted startup admits only authenticated loopback HTTP MCP"
        )


def _validate_effective_managed_mcp_servers(
    value: Any,
    expected_names: tuple[str, ...],
) -> None:
    if not isinstance(value, dict) or not value:
        raise HarnessProtocolError("Codex restricted startup did not retain managed MCP servers")
    if set(value) != set(expected_names):
        raise HarnessProtocolError("Codex effective MCP server set changed")
    for name, server in value.items():
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            raise HarnessProtocolError("Codex effective MCP server configuration is invalid")
        unsupported = sorted(set(server) - _EFFECTIVE_MCP_KEYS)
        if unsupported:
            raise HarnessProtocolError(
                f"Codex effective MCP server has unsupported settings: {unsupported}"
            )
        if not _loopback_http_url(server.get("url")):
            raise HarnessProtocolError("Codex effective MCP server is not loopback HTTP")
        headers = server.get("http_headers")
        if (
            not isinstance(headers, dict)
            or set(headers) != {"Authorization"}
            or _BEARER_TOKEN_RE.fullmatch(headers.get("Authorization", "")) is None
        ):
            raise HarnessProtocolError("Codex effective MCP server lost loopback authentication")
        if server.get("required") is not True:
            raise HarnessProtocolError("Codex managed MCP server must remain required")
        if server.get("default_tools_approval_mode") != "prompt":
            raise HarnessProtocolError("Codex managed MCP tools require prompt approval")
        if (
            server.get("enabled") is not True
            or server.get("environment_id") != "local"
            or server.get("tool_timeout_sec") is not None
        ):
            defaults = {
                key: server.get(key)
                for key in ("enabled", "environment_id", "tool_timeout_sec")
            }
            raise HarnessProtocolError(
                f"Codex managed MCP server changed its effective defaults: {defaults}"
            )


def _untrusted_project(cwd: str | None) -> dict[str, dict[str, str]]:
    if not cwd or not Path(cwd).is_absolute():
        raise HarnessProtocolError("Codex restricted startup requires an absolute project cwd")
    return {str(Path(cwd).resolve()): {"trust_level": "untrusted"}}


def restricted_startup_overrides(cwd: str | None, *, allow_native_plugins: bool = False) -> tuple[str, ...]:
    """Pin ephemeral cwd trust; never persist or admit user project trust tables."""
    project = next(iter(_untrusted_project(cwd)))
    overrides = tuple(
        override for override in RESTRICTED_STARTUP_OVERRIDES
        if not (allow_native_plugins and override == "features.plugins=false")
    )
    return (*overrides, f'projects={{{json.dumps(project)}={{trust_level="untrusted"}}}}')


def validate_source_config(
    values: Mapping[str, Any],
    *,
    effective: bool = False,
    cwd: str | None = None,
    allow_native_plugins: bool = False,
    managed_mcp_names: tuple[str, ...] = (),
) -> None:
    """Fail closed on config sources/extensions this restricted mode cannot admit."""
    allowed_keys = _CONFIG_KEYS | ({"plugins", "marketplaces"} if allow_native_plugins else set())
    disabled_features = _DISABLED_FEATURES - ({"plugins"} if allow_native_plugins else set())
    for key, value in values.items():
        if effective and value is None:
            continue
        if effective and key == "mcp_servers" and managed_mcp_names:
            _validate_effective_managed_mcp_servers(value, managed_mcp_names)
            continue
        if effective and key in _EFFECTIVE_DEFAULTS and value == _EFFECTIVE_DEFAULTS[key]:
            continue
        if effective and key == "projects" and value == _untrusted_project(cwd):
            continue
        if key not in allowed_keys:
            raise HarnessProtocolError(f"Codex restricted startup does not admit configuration key {key!r}")
        if key in {"plugins", "marketplaces"} and not isinstance(value, dict):
            raise HarnessProtocolError(f"Codex restricted startup requires {key} to be an object")
        if key == "project_doc_max_bytes" and value != 0:
            raise HarnessProtocolError("Codex restricted startup requires project_doc_max_bytes=0")
        if key == "allow_login_shell" and value is not False:
            raise HarnessProtocolError("Codex restricted startup requires allow_login_shell=false")
        if effective and key == "features" and isinstance(value, dict) and value.get("network_proxy") is None:
            value = {name: flag for name, flag in value.items() if name != "network_proxy"}
        if key == "features" and (not isinstance(value, dict) or set(value) - _FEATURE_KEYS):
            unknown = sorted(set(value) - _FEATURE_KEYS) if isinstance(value, dict) else ["<invalid>"]
            raise HarnessProtocolError(f"Codex restricted startup does not admit feature overrides: {unknown}")
        if key == "features" and any(value.get(name, False) is not False for name in disabled_features):
            raise HarnessProtocolError("Codex restricted startup requires source-loading features disabled")
    if effective and (
        values.get("project_doc_max_bytes") != 0 or not values.get("default_permissions")
        or values.get("projects") != _untrusted_project(cwd)
        or values.get("allow_login_shell") is not False
        or any((values.get("features") or {}).get(name) is not False for name in disabled_features)
    ):
        raise HarnessProtocolError("Codex restricted startup did not confirm permissions and disabled ambient loading")


def _directory(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        raise HarnessProtocolError(f"Codex {label} must be an absolute non-root directory")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved == Path(resolved.anchor):
        raise HarnessProtocolError(f"Codex {label} must resolve to a non-root directory")
    return resolved


def _admit_tree(path: Path, roots: tuple[Path, ...], ancestors: frozenset[Path] = frozenset()) -> None:
    resolved = path.resolve(strict=True)
    if not any(resolved.is_relative_to(root) for root in roots):
        raise HarnessProtocolError(f"Codex startup source is outside authorized roots: {path}")
    if resolved.is_dir():
        if resolved in ancestors:
            raise HarnessProtocolError("Codex startup source contains a directory cycle")
        for child in resolved.iterdir():
            _admit_tree(child, roots, ancestors | {resolved})
    elif not resolved.is_file():
        raise HarnessProtocolError("Codex startup source contains a non-regular file")


def validate_startup_sources(config: CodexHarnessConfig, context: HarnessContext) -> str | None:
    """Inspect before copying skills or launching CLI; return a scope binding digest."""
    if config.startup_source_roots is None:
        return None
    if config.inherit_process_env or config.bypass_approvals_and_sandbox:
        raise HarnessProtocolError("Codex restricted startup requires isolated env and host approvals")
    if HostCapability.TOOL_APPROVAL not in context.host_capabilities or context.interactions is None:
        raise HarnessProtocolError("Codex restricted startup requires host tool approvals")
    if context.mcp_servers:
        if not config.mcp_required:
            raise HarnessProtocolError("Codex managed MCP servers must be required")
        if config.mcp_default_tools_approval_mode != "prompt":
            raise HarnessProtocolError("Codex managed MCP tools require prompt approval")
        for server in context.mcp_servers:
            _validate_managed_mcp_server(server)
    cwd = _directory(context.cwd or config.cwd or "", "cwd")
    roots = tuple(sorted({_directory(value, "startup source root") for value in config.startup_source_roots}))
    if not any(cwd.is_relative_to(root) for root in roots):
        raise HarnessProtocolError("Codex cwd must be inside authorized startup source roots")
    homes = {}
    for key in ("HOME", "CODEX_HOME"):
        value = config.env.get(key)
        if not value or (key in context.env and context.env[key] != value):
            raise HarnessProtocolError(f"Codex restricted startup requires fixed explicit {key}")
        homes[key] = _directory(value, key)
        if cwd.is_relative_to(homes[key]) or homes[key].is_relative_to(cwd):
            raise HarnessProtocolError("Codex restricted startup requires homes separate from cwd")
    home, codex_home = homes["HOME"], homes["CODEX_HOME"]
    if home.is_relative_to(codex_home) or codex_home.is_relative_to(home):
        raise HarnessProtocolError("Codex restricted startup requires separate HOME and CODEX_HOME")
    managed_plugins = config.native_plugins is not None
    validate_source_config(config.thread_config, allow_native_plugins=managed_plugins)
    for override in config.config_overrides:
        validate_source_config(tomllib.loads(override), allow_native_plugins=managed_plugins)
    parents = []
    for parent in (cwd, *cwd.parents):
        parents.append(parent)
        if (parent / ".git").exists():
            break
    # Reject executable/config-loading sources before app-server startup, even
    # when the CLI would ignore an untrusted project's configuration today.
    config_paths = {codex_home / "config.toml", home / ".codex/config.toml"}
    config_paths.update(parent / ".codex/config.toml" for parent in parents)
    config_paths.update((Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml")))
    for path in config_paths:
        if path.exists() or path.is_symlink():
            permitted = (codex_home, home, *roots)
            if not any(path.resolve(strict=True).is_relative_to(root) for root in permitted):
                raise HarnessProtocolError(f"Codex configuration source is outside authorized roots: {path}")
            validate_source_config(
                tomllib.loads(path.read_text(encoding="utf-8")),
                allow_native_plugins=managed_plugins,
            )
    scans = {home / ".agents/skills", home / ".codex/skills", codex_home / "skills"}
    for parent in parents:
        scans.update((parent / ".agents/skills", parent / ".codex/skills"))
        for unsupported in (parent / ".codex/hooks.json", parent / ".agents/plugins"):
            if unsupported.exists() or unsupported.is_symlink():
                raise HarnessProtocolError("Codex restricted startup does not admit project hooks or plugins")
    unsupported_roots = [codex_home / "hooks.json", home / ".agents/plugins"]
    if not managed_plugins:
        unsupported_roots.append(codex_home / "plugins")
    for unsupported in unsupported_roots:
        if unsupported.exists() or unsupported.is_symlink():
            raise HarnessProtocolError("Codex restricted startup does not admit ambient hooks or plugins")
    if managed_plugins:
        for plugin in config.native_plugins or ():
            _admit_tree(Path(plugin.source_locator), roots)
        plugin_root = codex_home / "plugins"
        if plugin_root.exists() or plugin_root.is_symlink():
            _admit_tree(plugin_root, roots)
    for scan in scans:
        if scan.exists() or scan.is_symlink():
            _admit_tree(scan, roots)
    for source in config.skills:
        if not Path(source.dir).is_absolute():
            raise HarnessProtocolError("Codex restricted skill sources must use absolute paths")
        _admit_tree(Path(source.dir), roots)
    scope = {"roots": [str(root) for root in roots], "cwd": str(cwd),
             "homes": {key: str(path) for key, path in homes.items()},
             "skills": [(str(Path(source.dir).resolve()), source.mode, source.enabled_skills)
                        for source in config.skills],
             "native_plugins": [
                 (plugin.plugin_id, plugin.source_type, str(Path(plugin.source_locator).resolve()),
                  plugin.version, plugin.content_sha256, plugin.enabled,
                  plugin.required_components, plugin.mcp_server_names)
                 for plugin in config.native_plugins or ()
             ] if managed_plugins else None,
             "managed_mcp_names": sorted(server.name for server in context.mcp_servers)}
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
