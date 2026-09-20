# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Select complete configuration snapshots without merging vendor options."""
from openjiuwen.harness_protocol.construction import AgentExecutionSpec


def resolve_execution_spec(*, explicit: AgentExecutionSpec | None = None,
                           project: AgentExecutionSpec | None = None,
                           default: AgentExecutionSpec | None = None) -> AgentExecutionSpec:
    """Use explicit > project > default; never silently default to Native."""
    for candidate in (explicit, project, default):
        if candidate is not None:
            if not isinstance(candidate, AgentExecutionSpec):
                raise TypeError("execution candidates must be AgentExecutionSpec instances")
            return candidate
    raise ValueError("no execution configuration supplied")
