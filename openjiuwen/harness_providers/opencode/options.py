# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-owned native options; no ambient config or credential discovery."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from openjiuwen.harness_protocol import HostCapability, McpTransport

from .errors import OpenCodeError

_PRODUCT_MCP_SERVER_NAME = "jiuwenswarm_product_tools"


def _literal(value, *, reason, allow_empty=False):
    if (
        not isinstance(value, str)
        or not allow_empty
        and not value
        or "{env:" in value
        or "{file:" in value
        or "\x00" in value
    ):
        raise OpenCodeError(reason, category="process_start_failed")
    return value


def _product_mcp_config(server):
    if server.transport is not McpTransport.HTTP or not server.url:
        raise OpenCodeError("unsupported_product_mcp_transport", category="process_start_failed")
    url = urlsplit(server.url)
    try:
        port = url.port
    except ValueError:
        raise OpenCodeError("unmanaged_mcp_endpoint", category="process_start_failed") from None
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or port is None
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise OpenCodeError("unmanaged_mcp_endpoint", category="process_start_failed")
    headers = dict(server.headers)
    authorization = headers.get("Authorization")
    if (
        set(headers) != {"Authorization"}
        or not isinstance(authorization, str)
        or not authorization.startswith("Bearer ")
        or len(authorization) <= len("Bearer ")
    ):
        raise OpenCodeError("invalid_mcp_authentication", category="process_start_failed")
    return {
        "type": "remote",
        "url": server.url,
        "headers": headers,
        "oauth": False,
    }


def _host_mcp_config(server):
    if server.transport is McpTransport.STDIO:
        command = [
            _literal(item, reason="invalid_mcp_command", allow_empty=index > 0)
            for index, item in enumerate(server.command)
        ]
        environment = {
            _literal(key, reason="invalid_mcp_environment"): _literal(
                value, reason="invalid_mcp_environment", allow_empty=True
            )
            for key, value in server.env.items()
        }
        return {
            "type": "local",
            "command": command,
            **({"environment": environment} if environment else {}),
        }
    if server.transport is not McpTransport.HTTP or not server.url:
        raise OpenCodeError("unsupported_mcp_transport", category="process_start_failed")
    raw_url = _literal(server.url, reason="invalid_mcp_endpoint")
    url = urlsplit(raw_url)
    try:
        port = url.port
    except ValueError:
        raise OpenCodeError("invalid_mcp_endpoint", category="process_start_failed") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})
        or port is not None
        and not 0 < port <= 65535
    ):
        raise OpenCodeError("invalid_mcp_endpoint", category="process_start_failed")
    headers = {
        _literal(key, reason="invalid_mcp_headers"): _literal(
            value, reason="invalid_mcp_headers", allow_empty=True
        )
        for key, value in server.headers.items()
    }
    return {
        "type": "remote",
        "url": raw_url,
        **({"headers": headers} if headers else {}),
        "oauth": False,
    }


def _native_mcp_config(mcp_servers, *, include_product=True):
    result = {}
    names = set()
    for server in mcp_servers:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", server.name) or server.name in names:
            raise OpenCodeError("invalid_mcp_server_name", category="process_start_failed")
        names.add(server.name)
        if server.name == _PRODUCT_MCP_SERVER_NAME:
            if include_product:
                result[server.name] = _product_mcp_config(server)
            continue
        result[server.name] = _host_mcp_config(server)
    return result


def native_config(
    config,
    host_capabilities=frozenset(),
    mcp_servers=(),
    *,
    skill_path: Path | None = None,
    plugin_specs: tuple[str, ...] = (),
    include_product_mcp=True,
):
    model = config.model
    if model is None:
        raise OpenCodeError("explicit_model_required", category="process_start_failed")
    if config.skills and skill_path is None:
        raise OpenCodeError("explicit_skill_path_required", category="process_start_failed")
    return {
        "autoupdate": False,
        "share": "disabled",
        "model": f"{model.provider}/{model.model}",
        "small_model": f"{model.provider}/{model.model}",
        "enabled_providers": [model.provider],
        "provider": {
            model.provider: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "OpenJiuwen managed model",
                "options": {"baseURL": model.api_base, "apiKey": model.api_key or "not-required"},
                "models": {
                    model.model: {"name": model.model, "tool_call": True, "limit": {"context": 32000, "output": 4096}}
                },
            }
        },
        "permission": {
            "*": "allow" if config.full_access else "ask",
            "task": "deny",
            # The native question tool creates the awaited question request;
            # it is not itself a host-approved side effect.
            "question": "allow" if HostCapability.USER_INPUT in host_capabilities else "deny",
        },
        "lsp": False,
        "formatter": False,
        "agent": {"title": {"disable": True}},
        "mcp": _native_mcp_config(mcp_servers, include_product=include_product_mcp),
        **(
            {"skills": {"paths": [str(skill_path)], "urls": []}}
            if config.skills and skill_path is not None
            else {}
        ),
        **({"plugin": list(plugin_specs)} if config.native_plugins is not None else {}),
    }


def environment(root, config, password, *, persistent_root=None, allow_native_plugins=False):
    persistent_root = persistent_root or root
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(persistent_root / "data"),
        "XDG_STATE_HOME": str(persistent_root / "state"),
        **{
            key: "true"
            for key in (
                "OPENCODE_DISABLE_AUTOUPDATE",
                "OPENCODE_DISABLE_MODELS_FETCH",
                "OPENCODE_DISABLE_PROJECT_CONFIG",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS",
                "OPENCODE_DISABLE_LSP_DOWNLOAD",
                "OPENCODE_DISABLE_FFF",
                "OPENCODE_DISABLE_CLAUDE_CODE",
            )
        },
        **(
            {}
            if allow_native_plugins
            else {"OPENCODE_PURE": "true"}
        ),
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_CONFIG_CONTENT": json.dumps(config),
        "npm_config_registry": "http://127.0.0.1:1",
        "npm_config_offline": "true",
        "npm_config_fetch_retries": "0",
    }


def validate_readback(actual, expected):
    normalized = {**expected, "agent": {"title": {"disable": True, "options": {}, "permission": {}}}}
    if not isinstance(actual, dict) or any(
        actual.get(key) != value for key, value in normalized.items() if key != "plugin"
    ):
        raise OpenCodeError("effective_config_mismatch", category="process_start_failed")
    if actual.get("skills") != expected.get("skills"):
        raise OpenCodeError("effective_skill_config_mismatch", category="process_start_failed")
    if actual.get("plugin", []) != expected.get("plugin", []):
        raise OpenCodeError("effective_plugin_config_mismatch", category="process_start_failed")
    if any(actual.get(key) for key in ("instructions", "command")):
        raise OpenCodeError("unadmitted_effective_source", category="process_start_failed")
