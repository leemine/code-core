# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider registration and construction without Native manifest imports."""
from dataclasses import replace
from typing import Literal

from openjiuwen.harness_protocol import (
    ExecutionAuthorization,
    HarnessAuthorizationProvider,
    HarnessProtocol,
    HarnessProvider,
    ProviderCapability,
    ProviderCapabilityInventory,
    ProviderCapabilityKind,
    RuntimeSurface,
)
from openjiuwen.harness_protocol.construction import AgentExecutionSpec
from openjiuwen.harness_protocol.errors import UnsupportedHarnessCapabilityError
from openjiuwen.harness_protocol.models import JsonObject

HarnessProviderName = Literal["native", "native_v2", "claudecode", "codex", "dsh", "opencode"]
PROVIDER_NAMES: tuple[HarnessProviderName, ...] = ("native", "native_v2", "claudecode", "codex", "dsh", "opencode")

_EXTERNAL_CODE_CAPABILITIES = (
    "filesystem",
    "terminal",
    "git",
    "diff",
    "test",
    "review",
)


def resolve_provider(provider: str) -> HarnessProvider:
    """Return the provider SPI implementation registered under ``provider``."""

    if provider == "native":
        from openjiuwen.harness_providers.native import NativeHarnessProvider

        return NativeHarnessProvider()
    if provider == "native_v2":
        from openjiuwen.agent_teams.harness.protocol_adapter import NativeV2HarnessProvider

        return NativeV2HarnessProvider()
    if provider == "claudecode":
        from openjiuwen.harness_providers.claudecode import ClaudeCodeHarnessProvider

        return ClaudeCodeHarnessProvider()
    if provider == "codex":
        from openjiuwen.harness_providers.codex import CodexHarnessProvider

        return CodexHarnessProvider()
    if provider == "dsh":
        from openjiuwen.harness_providers.dsh import DshHarnessProvider

        return DshHarnessProvider()
    if provider == "opencode":
        from openjiuwen.harness_providers.opencode import OpenCodeHarnessProvider

        return OpenCodeHarnessProvider()
    raise ValueError(f"unknown harness provider {provider!r}; expected one of {', '.join(PROVIDER_NAMES)}")



def compile_execution(spec: AgentExecutionSpec) -> JsonObject:
    """Return validated provider configuration without starting an execution.

    Mode translation is deliberately not guessed. Until provider-specific
    mode compilation lands, explicit mode requests fail during configuration.
    """
    if spec.requested_mode is not None:
        raise UnsupportedHarnessCapabilityError("execution mode compilation is not yet supported")
    if spec.authorization is not None:
        provider = resolve_provider(spec.provider_id)
        if not isinstance(provider, HarnessAuthorizationProvider):
            raise UnsupportedHarnessCapabilityError(
                f"{spec.provider_id} does not support explicit execution authorization"
            )
        return provider.compile_authorization(spec.provider_config, spec.authorization)
    return spec.provider_config


def execution_authorization(spec: AgentExecutionSpec) -> ExecutionAuthorization:
    """Resolve the same decision used by native and host-managed product tools."""
    if spec.authorization is not None:
        # Validate support even when a host asks before constructing the engine.
        compile_execution(spec)
        return spec.authorization
    provider = resolve_provider(spec.provider_id)
    if isinstance(provider, HarnessAuthorizationProvider):
        return provider.legacy_authorization(spec.provider_config)
    return ExecutionAuthorization()


def configured_provider_capabilities(
    spec: AgentExecutionSpec,
) -> ProviderCapabilityInventory:
    """Compile the Provider-owned inventory without allocating a runtime.

    Portable Skill and native-plugin bytes are still independently validated
    by the owning Provider immediately before startup and, where applicable,
    before every Turn. This read-only declaration lets a host reject namespace
    collisions and freeze one product capability catalog before side effects.
    """

    config = compile_execution(spec)
    entries = [
        ProviderCapability(
            name,
            ProviderCapabilityKind.CATEGORY,
            frozenset({RuntimeSurface.CODE}),
        )
        for name in _EXTERNAL_CODE_CAPABILITIES
    ] if spec.provider_id in {"codex", "opencode"} else []
    if spec.provider_id == "codex":
        from openjiuwen.harness_providers.codex.config import CodexHarnessConfig
        from openjiuwen.harness_providers.skills import configured_skill_names

        parsed = CodexHarnessConfig.from_mapping(config)
        entries.extend(
            ProviderCapability(name, ProviderCapabilityKind.SKILL, source="portable_skill")
            for name in configured_skill_names(parsed.skills, conflict=parsed.skill_conflict)
        )
        for plugin in parsed.native_plugins or ():
            if not plugin.enabled:
                continue
            entries.append(
                ProviderCapability(
                    plugin.plugin_id,
                    ProviderCapabilityKind.PLUGIN,
                    source="native_plugin",
                )
            )
            entries.extend(
                ProviderCapability(
                    name,
                    ProviderCapabilityKind.MCP_SERVER,
                    source=plugin.plugin_id,
                )
                for name in plugin.mcp_server_names
            )
    elif spec.provider_id == "opencode":
        from openjiuwen.harness_providers.opencode.config import OpenCodeHarnessConfig
        from openjiuwen.harness_providers.skills import configured_skill_names

        parsed = OpenCodeHarnessConfig.from_mapping(config)
        entries.extend(
            ProviderCapability(name, ProviderCapabilityKind.SKILL, source="portable_skill")
            for name in configured_skill_names(parsed.skills, conflict=parsed.skill_conflict)
        )
        for plugin in parsed.native_plugins or ():
            if not plugin.enabled:
                continue
            entries.append(
                ProviderCapability(
                    plugin.plugin_id,
                    ProviderCapabilityKind.PLUGIN,
                    source="native_plugin",
                )
            )
            entries.extend(
                ProviderCapability(
                    name,
                    ProviderCapabilityKind.TOOL,
                    source=plugin.plugin_id,
                )
                for name in plugin.required_tools
            )
    return ProviderCapabilityInventory(spec.provider_id, tuple(entries))


def apply_legacy_full_access(spec: AgentExecutionSpec) -> AgentExecutionSpec:
    """Preserve the pre-authorization Web profile projection byte for byte.

    Compatibility only: the old facade affected Codex and no other provider.
    New profiles use explicit authorization and fail closed if unsupported.
    Keep authorization=None so old Binding digests and cold archives survive.
    """
    if spec.authorization is not None:
        raise ValueError("legacy projection requires authorization=None")
    if spec.provider_id != "codex":
        return spec
    from openjiuwen.harness_providers.codex.provider import legacy_full_access_config

    return replace(spec, provider_config=legacy_full_access_config(spec.provider_config))


def create_execution(spec: AgentExecutionSpec) -> HarnessProtocol:
    """Construct one unstarted harness using the shared provider registry."""
    config = compile_execution(spec)
    return resolve_provider(spec.provider_id).create(config)
