# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider registration and construction without Native manifest imports."""
from typing import Literal

from openjiuwen.harness_protocol import HarnessProtocol, HarnessProvider
from openjiuwen.harness_protocol.construction import AgentExecutionSpec
from openjiuwen.harness_protocol.errors import UnsupportedHarnessCapabilityError
from openjiuwen.harness_protocol.models import JsonObject

HarnessProviderName = Literal["native", "native_v2", "claudecode", "codex", "dsh"]
PROVIDER_NAMES: tuple[HarnessProviderName, ...] = ("native", "native_v2", "claudecode", "codex", "dsh")


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
    raise ValueError(f"unknown harness provider {provider!r}; expected one of {', '.join(PROVIDER_NAMES)}")



def compile_execution(spec: AgentExecutionSpec) -> JsonObject:
    """Return validated provider configuration without starting an execution.

    Mode translation is deliberately not guessed. Until provider-specific
    mode compilation lands, explicit mode requests fail during configuration.
    """
    if spec.requested_mode is not None:
        raise UnsupportedHarnessCapabilityError("execution mode compilation is not yet supported")
    return spec.provider_config


def create_execution(spec: AgentExecutionSpec) -> HarnessProtocol:
    """Construct one unstarted harness using the shared provider registry."""
    config = compile_execution(spec)
    return resolve_provider(spec.provider_id).create(config)
