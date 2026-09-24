# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-owned native options; no ambient config or credential discovery."""

import json

from openjiuwen.harness_protocol import HostCapability

from .errors import OpenCodeError


def native_config(config, host_capabilities=frozenset()):
    model = config.model
    if model is None:
        raise OpenCodeError("explicit_model_required", category="process_start_failed")
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
        "mcp": {},
    }


def environment(root, config, password, *, persistent_root=None):
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
                "OPENCODE_PURE",
                "OPENCODE_DISABLE_CLAUDE_CODE",
            )
        },
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_CONFIG_CONTENT": json.dumps(config),
        "npm_config_registry": "http://127.0.0.1:1",
        "npm_config_offline": "true",
        "npm_config_fetch_retries": "0",
    }


def validate_readback(actual, expected):
    normalized = {**expected, "agent": {"title": {"disable": True, "options": {}, "permission": {}}}}
    if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in normalized.items()):
        raise OpenCodeError("effective_config_mismatch", category="process_start_failed")
    if any(actual.get(key) for key in ("plugin", "instructions", "command", "skills")):
        raise OpenCodeError("unadmitted_effective_source", category="process_start_failed")
