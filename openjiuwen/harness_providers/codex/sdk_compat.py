# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fail-closed, per-client compatibility seams for the optional Codex SDK."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from types import MethodType
from typing import Any

from pydantic import RootModel

from openjiuwen.harness_protocol import HarnessProtocolError


def isolate_process_environment(client: Any, sdk: Any) -> None:
    """Use the configured environment verbatim when starting this SDK client.

    SDK 0.144.4 always merges os.environ in CodexClient.start(). Replace only
    that instance's launcher; retain its binary resolver, pipes, reader, router
    and close implementation. Never mutate os.environ or SDK module globals.
    """
    transport = getattr(getattr(client, "_client", None), "_sync", None)
    module = getattr(sdk, "client", None)
    required = ("start", "_start_stderr_drain_thread", "_start_reader_thread")
    helpers = ("_resolve_codex_bin", "_installed_codex_path_dirs", "_prepend_path_dirs")
    if (
        transport is None
        or any(not callable(getattr(transport, name, None)) for name in required)
        or any(not callable(getattr(module, name, None)) for name in helpers)
        or not hasattr(transport, "_proc")
        or getattr(transport, "config", None) is None
    ):
        raise HarnessProtocolError("Codex SDK cannot enforce an isolated process environment")
    if transport._proc is not None:
        raise HarnessProtocolError("Codex environment isolation must be installed before process startup")

    def start_isolated(self: Any) -> None:
        if self._proc is not None:
            return
        config = self.config
        path_dirs: tuple = ()
        if config.launch_args_override is not None:
            args = list(config.launch_args_override)
        else:
            args = [str(module._resolve_codex_bin(config))]
            if config.codex_bin is None:
                path_dirs = module._installed_codex_path_dirs()
            for override in config.config_overrides:
                args.extend(("--config", override))
            args.extend(("app-server", "--listen", "stdio://"))
        env = dict(config.env or {})
        # The SDK must not fall back to the parent's PATH when env omitted it.
        env.setdefault("PATH", "")
        module._prepend_path_dirs(env, path_dirs)
        self._proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=config.cwd, env=env, bufsize=1,
        )
        self._start_stderr_drain_thread()
        self._start_reader_thread()

    transport.start = MethodType(start_isolated, transport)
    if getattr(transport.start, "__func__", None) is not start_isolated:
        raise HarnessProtocolError("Codex SDK did not retain the isolated process launcher")


def _permission_fingerprint(
    config: dict[str, Any], profile: str | None, sandbox: str, cwd: str | None, source_fingerprint: str | None = None,
) -> str:
    """Fingerprint only the selected security policy and its inheritance chain."""
    policies = config.get("permissions") or {}
    if not isinstance(policies, dict):
        raise HarnessProtocolError("Codex permission profiles must be a mapping")
    selected = {}
    current = profile
    while current and not current.startswith(":"):
        if current in selected or not isinstance(policies.get(current), dict):
            raise HarnessProtocolError("Codex permission profile is missing or has cyclic inheritance")
        selected[current] = policies[current]
        current = policies[current].get("extends")
        if current is not None and not isinstance(current, str):
            raise HarnessProtocolError("Codex permission profile parent must be a string")
    if current not in (None, ":read-only", ":workspace"):
        raise HarnessProtocolError("Codex host approvals require a restricted permission profile")
    policy = {"profile": profile, "profiles": selected, "cwd": os.path.abspath(cwd or os.getcwd())}
    if source_fingerprint is not None:
        policy["startup_sources"] = source_fingerprint
    if profile is None:
        policy.update(sandbox=sandbox, workspace=config.get("sandbox_workspace_write"))
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


async def connect_with_host_approvals(
    *, client: Any, sdk: Any, options: dict[str, Any],
    resume_thread_id: str | None, raw_events: bool, expected_fingerprint: str | None = None,
    source_fingerprint: str | None = None,
    allow_native_plugins: bool = False,
    managed_mcp_names: tuple[str, ...] = (),
) -> tuple[Any, str, str]:
    """Negotiate host review and confirm the effective named or legacy policy.

    The locked SDK's generated response drops activePermissionProfile. Retain
    the raw JSON privately; do not add vendor fields to the shared protocol.
    """
    initialize = getattr(client, "_ensure_initialized", None)
    request = getattr(getattr(client, "_client", None), "request", None)
    thread_type = getattr(sdk, "AsyncThread", None)
    if not callable(initialize) or not callable(request) or not callable(thread_type):
        raise HarnessProtocolError("Codex SDK cannot enforce host approval policy")
    if "approval_mode" in options or "sandbox" in options:
        raise HarnessProtocolError("Codex host approval policy conflicts with permission bypass")
    thread_config = dict(options.get("config", {}))
    sandbox = thread_config.get("sandbox_mode", "read-only")
    if sandbox not in ("read-only", "workspace-write"):
        raise HarnessProtocolError("Codex host approvals require a restricted sandbox")
    # Define named profiles through the server configuration, not per-thread
    # overlays whose merged provenance cannot be read back by config/read.
    if any(key == "permissions" or key.startswith("permissions.") for key in thread_config):
        raise HarnessProtocolError("Codex permission profiles must be defined in server configuration")
    await initialize()
    config_response = await request(
        "config/read", {"cwd": options.get("cwd"), "includeLayers": False}, response_model=RootModel[dict],
    )
    config = config_response.model_dump(mode="json").get("config")
    if not isinstance(config, dict):
        raise HarnessProtocolError("Codex did not return its effective permission configuration")
    if source_fingerprint is not None:
        from openjiuwen.harness_providers.codex.source_policy import validate_source_config
        validate_source_config(
            config,
            effective=True,
            cwd=options.get("cwd"),
            allow_native_plugins=allow_native_plugins,
            managed_mcp_names=managed_mcp_names,
        )
    profile = thread_config.get("default_permissions", config.get("default_permissions"))
    if "default_permissions" in thread_config and profile is None:
        raise HarnessProtocolError("Codex default_permissions must name a permission profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise HarnessProtocolError("Codex default_permissions must name a permission profile")
    if profile is not None and any(
        source.get(key) is not None
        for source in (config, thread_config) for key in ("sandbox_mode", "sandbox_workspace_write")
    ):
        raise HarnessProtocolError("Codex named permissions conflict with legacy sandbox configuration")
    security_config = {**config, **thread_config}
    fingerprint = _permission_fingerprint(security_config, profile, sandbox, options.get("cwd"), source_fingerprint)
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise HarnessProtocolError("Codex permission configuration changed since session activation")
    params = {"config": thread_config, "approvalPolicy": "untrusted", "approvalsReviewer": "user"}
    if profile is None:
        params["sandbox"] = sandbox
    else:
        params["permissionProfile"] = profile
    for key, wire_key in (
        ("cwd", "cwd"), ("model", "model"), ("model_provider", "modelProvider"),
        ("developer_instructions", "developerInstructions"),
    ):
        if key in options:
            params[wire_key] = options[key]
    if resume_thread_id is None:
        params["ephemeral"] = options.get("ephemeral", False)
        params["experimentalRawEvents"] = raw_events
    else:
        params["threadId"] = resume_thread_id
    response = await request(
        "thread/resume" if resume_thread_id else "thread/start", params, response_model=RootModel[dict],
    )
    effective = response.model_dump(mode="json")
    actual_sandbox = effective.get("sandbox") or {}
    if not isinstance(actual_sandbox, dict):
        raise HarnessProtocolError("Codex did not confirm the required host approval and sandbox policy")
    actual_profile = effective.get("activePermissionProfile")
    policy_matches = (
        isinstance(actual_profile, dict) and actual_profile.get("id") == profile
        if profile is not None else
        actual_profile is None and actual_sandbox.get("type") == (
            "readOnly" if sandbox == "read-only" else "workspaceWrite"
        )
    )
    if (
        effective.get("approvalPolicy") != "untrusted"
        or effective.get("approvalsReviewer") != "user"
        or actual_sandbox.get("type") not in ("readOnly", "workspaceWrite")
        or not policy_matches
    ):
        raise HarnessProtocolError("Codex did not confirm the required host approval and sandbox policy")
    thread_id = (effective.get("thread") or {}).get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise HarnessProtocolError("Codex did not return a thread identity")
    if resume_thread_id is not None and thread_id != resume_thread_id:
        raise HarnessProtocolError("Codex resumed a different thread under host approval policy")
    return thread_type(client, thread_id), str(effective.get("model") or ""), fingerprint
