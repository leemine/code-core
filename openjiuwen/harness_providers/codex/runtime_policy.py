# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Codex-private translation of provider-neutral runtime requirements."""

from __future__ import annotations

from dataclasses import dataclass, replace

from openjiuwen.harness_protocol import HarnessRuntimePolicy, SourceDiscovery, WorkspaceAccess
from openjiuwen.harness_protocol.errors import HarnessProtocolError
from openjiuwen.harness_providers.codex.config import CodexHarnessConfig


@dataclass(frozen=True, slots=True)
class CodexRuntimePolicy:
    config: CodexHarnessConfig
    expected_sandbox: str | None


def compile_runtime_policy(
    config: CodexHarnessConfig,
    policy: HarnessRuntimePolicy | None,
) -> CodexRuntimePolicy:
    """Narrow the frozen Provider config to one effective Surface policy."""

    if policy is None:
        return CodexRuntimePolicy(config, None)
    if policy.source_discovery is not SourceDiscovery.EXPLICIT_ONLY:
        raise HarnessProtocolError("Codex runtime policy requires explicit-only source discovery")
    if config.startup_source_roots is None or config.inherit_process_env:
        raise HarnessProtocolError(
            "Codex runtime policy requires isolated env and explicit startup source roots"
        )
    if policy.workspace_access is WorkspaceAccess.FULL_ACCESS:
        if not config.bypass_approvals_and_sandbox:
            raise HarnessProtocolError("Codex runtime policy exceeds the frozen authorization boundary")
        return CodexRuntimePolicy(config, None)

    sandbox = (
        "read-only"
        if policy.workspace_access is WorkspaceAccess.READ_ONLY
        else "workspace-write"
    )
    thread_config = dict(config.thread_config)
    configured = thread_config.get("sandbox_mode")
    if configured not in (None, sandbox):
        raise HarnessProtocolError("Codex runtime policy conflicts with the configured sandbox")
    # The app server receives the desired sandbox through thread/start. Do not
    # also inject the legacy sandbox_mode config key: an effective named
    # permission profile is mutually exclusive with that legacy setting and is
    # validated from the thread/start response instead.
    return CodexRuntimePolicy(
        replace(
            config,
            thread_config=thread_config,
            bypass_approvals_and_sandbox=False,
            mcp_default_tools_approval_mode="prompt",
        ),
        sandbox,
    )


__all__ = ["CodexRuntimePolicy", "compile_runtime_policy"]
