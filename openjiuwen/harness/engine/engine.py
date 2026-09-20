# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Construction facade; providers continue to own all runtime state."""
from dataclasses import dataclass, field

from openjiuwen.harness.engine.config import ExecutionBinding
from openjiuwen.harness_protocol import HarnessProtocol
from openjiuwen.harness_protocol.construction import AgentExecutionSpec
from openjiuwen.harness_providers.construction import create_execution


@dataclass(frozen=True, slots=True)
class HarnessEngine:
    """One binding and its unstarted provider; no parallel state machine."""

    binding: ExecutionBinding
    harness: HarnessProtocol = field(repr=False)


def create_harness_engine(spec: AgentExecutionSpec, *, binding: ExecutionBinding) -> HarnessEngine:
    """Validate the binding before constructing, without starting the provider."""
    binding.validate_spec(spec)
    return HarnessEngine(binding, create_execution(spec))
