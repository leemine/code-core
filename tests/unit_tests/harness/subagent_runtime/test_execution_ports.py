# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Contract tests for provider-neutral product-subagent execution ports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
from openjiuwen.harness.subagent_runtime.models import SubagentStatusKind, UserInputOp
from openjiuwen.harness.subagent_runtime.ports import (
    ParentExecutionContext,
    SubagentBuildRequest,
    SubagentExecution,
    SubagentTurnRequest,
    SubagentTurnResult,
)
from openjiuwen.harness.subagent_runtime.session_manager import SubagentSessionManager
from openjiuwen.harness.tools.subagent._control_registry import get_subagent_control


@dataclass
class RecordingExecution:
    result: SubagentTurnResult = field(
        default_factory=lambda: SubagentTurnResult(output="port result")
    )
    requests: list[SubagentTurnRequest] = field(default_factory=list)
    close_reasons: list[str] = field(default_factory=list)

    async def run_turn(self, request, *, on_chunk=None, on_result) -> None:
        self.requests.append(request)
        if on_chunk is not None:
            await on_chunk({"type": "llm_output", "payload": {"content": "port"}})
        await on_result(self.result)

    async def close(self, reason: str) -> None:
        self.close_reasons.append(reason)


@dataclass
class RecordingFactory:
    execution: RecordingExecution = field(default_factory=RecordingExecution)
    restorable: bool = True
    creates: list[tuple[SubagentBuildRequest, ParentExecutionContext]] = field(
        default_factory=list
    )
    restore_checks: list[tuple[SubagentBuildRequest, ParentExecutionContext]] = field(
        default_factory=list
    )

    async def create(self, request, context) -> SubagentExecution:
        self.creates.append((request, context))
        return self.execution

    async def can_restore(self, request, context) -> bool:
        self.restore_checks.append((request, context))
        return self.restorable


class MissingResultExecution(RecordingExecution):
    async def run_turn(self, request, *, on_chunk=None, on_result) -> None:
        self.requests.append(request)


class DuplicateResultExecution(RecordingExecution):
    async def run_turn(self, request, *, on_chunk=None, on_result) -> None:
        self.requests.append(request)
        await on_result(self.result)
        await on_result(self.result)


def _manager(factory: RecordingFactory) -> SubagentSessionManager:
    return SubagentSessionManager(
        object(),
        SubagentRuntimeConfig(),
        asyncio.Semaphore(2),
        execution_factory=factory,
    )


@pytest.mark.asyncio
async def test_injected_factory_runs_without_deepagent_or_native_session() -> None:
    factory = RecordingFactory()
    manager = _manager(factory)

    instance = await manager.create(
        subagent_type="explore",
        subagent_id="child-1",
        parent_session_id="parent-1",
        display_name="Explorer",
        role="researcher",
        browser_capabilities=["navigate"],
    )
    await instance.enqueue(UserInputOp(query="inspect", task_id="task-1"))
    await asyncio.wait_for(instance._ops.join(), timeout=1.0)

    request, context = factory.creates[0]
    assert request == SubagentBuildRequest(
        subagent_id="child-1",
        subagent_type="explore",
        display_name="Explorer",
        role="researcher",
        browser_capabilities=("navigate",),
    )
    assert context.parent_session_id == "parent-1"
    assert context.parent_subject_id == "main"
    assert factory.execution.requests == [
        SubagentTurnRequest(task_id="task-1", query="inspect")
    ]
    assert instance.agent_status().kind is SubagentStatusKind.COMPLETED
    assert instance.last_output == "port result"

    await manager.remove("child-1", reason="test")
    assert factory.execution.close_reasons == ["test"]


@pytest.mark.asyncio
async def test_restore_availability_is_delegated_to_bound_factory() -> None:
    factory = RecordingFactory(restorable=False)
    manager = _manager(factory)

    with pytest.raises(Exception):
        await manager.restore(
            subagent_type="explore",
            subagent_id="child-1",
            parent_session_id="parent-1",
            display_name="Explorer",
            role="researcher",
        )

    assert len(factory.restore_checks) == 1
    assert factory.creates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution", "message"),
    [
        (MissingResultExecution(), "without a turn result"),
        (DuplicateResultExecution(), "more than once"),
    ],
)
async def test_execution_must_settle_exactly_once(execution, message: str) -> None:
    manager = _manager(RecordingFactory(execution=execution))
    instance = await manager.create(
        subagent_type="explore",
        subagent_id="child-invalid",
        parent_session_id="parent-1",
        display_name="Explorer",
        role="researcher",
    )
    try:
        await instance.enqueue(UserInputOp(query="inspect", task_id="task-invalid"))
        await asyncio.wait_for(instance._ops.join(), timeout=1.0)
        assert instance.agent_status().kind is SubagentStatusKind.ERRORED
        assert message in (instance.agent_status().message or "")
    finally:
        await manager.remove("child-invalid", reason="test")


def test_parent_context_and_build_request_reject_missing_identity() -> None:
    with pytest.raises(ValueError, match="parent_session_id"):
        ParentExecutionContext(parent_session_id="", parent_subject_id="main")
    with pytest.raises(ValueError, match="subagent id and type"):
        SubagentBuildRequest(
            subagent_id="",
            subagent_type="explore",
            display_name="Explorer",
            role="researcher",
        )


@pytest.mark.asyncio
async def test_control_registry_accepts_session_protocol_and_fixes_factory() -> None:
    owner = SimpleNamespace()
    session = type(
        "ParentSession",
        (),
        {
            "get_session_id": lambda self: "parent-1",
            "get_state": lambda self, key: None,
        },
    )()
    first = RecordingFactory()
    second = RecordingFactory()

    control = get_subagent_control(owner, session, execution_factory=first)
    try:
        assert control.execution_factory is first
        with pytest.raises(Exception, match="cannot change"):
            get_subagent_control(owner, session, execution_factory=second)
    finally:
        if control._activity_emitter is not None:
            await control._activity_emitter.close()
