# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral construction and execution ports for product subagents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ParentExecutionContext:
    """Fixed parent facts supplied to one child construction attempt."""

    parent_session_id: str
    parent_subject_id: str
    parent_session: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.parent_session_id:
            raise ValueError("parent_session_id is required")
        if not self.parent_subject_id:
            raise ValueError("parent_subject_id is required")


@dataclass(frozen=True, slots=True)
class SubagentBuildRequest:
    """Provider-neutral identity and presentation for one child execution."""

    subagent_id: str
    subagent_type: str
    display_name: str
    role: str
    browser_capabilities: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.subagent_id or not self.subagent_type:
            raise ValueError("subagent id and type are required")


@dataclass(frozen=True, slots=True)
class SubagentTurnRequest:
    """One serialized product-subagent turn."""

    task_id: str
    query: str

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("subagent task_id is required")


@dataclass(frozen=True, slots=True)
class SubagentTurnResult:
    """Provider-neutral terminal result used by the runtime state machine."""

    output: str
    reasoning: str = ""
    is_error: bool = False
    error_code: str | None = None


ChunkCallback = Callable[[Any], Awaitable[None]]
ResultCallback = Callable[[SubagentTurnResult], Awaitable[None]]


class SubagentExecution(Protocol):
    """One constructed child execution, potentially spanning several turns."""

    async def run_turn(
        self,
        request: SubagentTurnRequest,
        *,
        on_chunk: ChunkCallback | None = None,
        on_result: ResultCallback,
    ) -> None:
        """Run one turn and settle it through ``on_result`` before finalization."""
        ...

    async def close(self, reason: str) -> None:
        """Idempotently release resources owned by this child execution."""
        ...


class SubagentExecutionFactory(Protocol):
    """Construct and restore child executions for one fixed parent provider."""

    async def create(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> SubagentExecution:
        ...

    async def can_restore(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> bool:
        ...


__all__ = [
    "ChunkCallback",
    "ParentExecutionContext",
    "ResultCallback",
    "SubagentBuildRequest",
    "SubagentExecution",
    "SubagentExecutionFactory",
    "SubagentTurnRequest",
    "SubagentTurnResult",
]
