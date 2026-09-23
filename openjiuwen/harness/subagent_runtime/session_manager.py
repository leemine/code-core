# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Subagent instance lifecycle management for one parent session."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from openjiuwen.harness.execution_subject import current_execution_subject
from openjiuwen.harness.subagent_runtime.activity import ActivityProjector
from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
from openjiuwen.harness.subagent_runtime.errors import (
    raise_subagent_not_found,
)
from openjiuwen.harness.subagent_runtime.instance import SubagentInstance
from openjiuwen.harness.subagent_runtime.models import (
    SubagentActivity,
    SubagentMessage,
    SubagentStatus,
    UserInputOp,
)
from openjiuwen.harness.subagent_runtime.native_execution import (
    NativeSubagentExecutionFactory,
)
from openjiuwen.harness.subagent_runtime.ports import (
    ParentExecutionContext,
    SubagentBuildRequest,
    SubagentExecutionFactory,
    SubagentTurnResult,
)
from openjiuwen.harness.subagent_runtime.transcript import TranscriptProjector


class SubagentSessionManager:
    """Create, index, and tear down subagent instances for one parent session."""

    def __init__(
        self,
        parent_agent: Any,
        config: SubagentRuntimeConfig,
        running_semaphore: asyncio.Semaphore,
        *,
        parent_session: Any | None = None,
        execution_factory: SubagentExecutionFactory | None = None,
        status_change_handler: Callable[[str, SubagentStatus], Awaitable[None]] | None = None,
        activity_handler: Callable[[SubagentActivity], None] | None = None,
        transcript_handler: Callable[[SubagentMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._running_semaphore = running_semaphore
        self._parent_session = parent_session
        self._execution_factory = execution_factory or NativeSubagentExecutionFactory(
            parent_agent,
            parent_session_getter=lambda: self._parent_session,
        )
        self._status_change_handler = status_change_handler
        self._activity_handler = activity_handler
        self._transcript_handler = transcript_handler
        self._instances: dict[str, SubagentInstance] = {}
        self._projectors: dict[str, ActivityProjector] = {}
        self._transcript_projectors: dict[str, TranscriptProjector] = {}

    @property
    def execution_factory(self) -> SubagentExecutionFactory:
        return self._execution_factory

    def set_parent_session(self, session: Any | None) -> None:
        self._parent_session = session

    def _parent_context(self, parent_session_id: str) -> ParentExecutionContext:
        parent_subject = current_execution_subject()
        return ParentExecutionContext(
            parent_session_id=parent_session_id,
            parent_subject_id=(
                parent_subject.subject_id if parent_subject is not None else "main"
            ),
            parent_session=self._parent_session,
        )

    async def create(
        self,
        *,
        subagent_type: str,
        subagent_id: str,
        parent_session_id: str,
        display_name: str,
        role: str,
        browser_capabilities: list[str] | None = None,
    ) -> SubagentInstance:
        request = SubagentBuildRequest(
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            display_name=display_name,
            role=role,
            browser_capabilities=(
                tuple(browser_capabilities)
                if browser_capabilities is not None
                else None
            ),
        )
        context = self._parent_context(parent_session_id)
        execution = await self._execution_factory.create(
            request,
            context,
        )

        async def on_status_changed(status: SubagentStatus) -> None:
            if self._status_change_handler is not None:
                await self._status_change_handler(subagent_id, status)

        projector = ActivityProjector(subagent_id=subagent_id, config=self._config)
        self._projectors[subagent_id] = projector
        transcript_projector = TranscriptProjector(
            subagent_id=subagent_id,
            parent_session_id=parent_session_id,
        )
        self._transcript_projectors[subagent_id] = transcript_projector
        instance_holder: dict[str, SubagentInstance] = {}

        async def on_turn_stream_start(op: UserInputOp) -> None:
            if self._transcript_handler is None:
                return
            message = transcript_projector.begin_turn(op.task_id, op.query)
            await self._transcript_handler(message)

        async def on_turn_stream_end(op: UserInputOp, result: SubagentTurnResult) -> None:
            if self._activity_handler is not None:
                for activity in projector.flush_pending(op.task_id):
                    self._activity_handler(activity)
            if self._transcript_handler is None:
                return
            message = transcript_projector.end_turn(op.task_id, result)
            await self._transcript_handler(message)

        async def on_chunk(chunk: Any) -> None:
            instance = instance_holder.get("instance")
            if instance is None:
                return
            task_id = instance.current_task_id or ""
            if self._activity_handler is not None:
                for activity in projector.project(chunk, task_id=task_id):
                    self._activity_handler(activity)
            if self._transcript_handler is not None:
                message = transcript_projector.project(chunk, task_id=task_id)
                if message is not None:
                    await self._transcript_handler(message)

        instance = SubagentInstance(
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            display_name=display_name,
            role=role,
            parent_session_id=parent_session_id,
            parent_subject_id=context.parent_subject_id,
            execution=execution,
            running_semaphore=self._running_semaphore,
            turn_timeout_s=self._config.turn_timeout_s,
            on_status_changed=on_status_changed,
            on_chunk=on_chunk if (self._activity_handler is not None or self._transcript_handler is not None) else None,
            on_turn_stream_start=on_turn_stream_start if self._transcript_handler else None,
            on_turn_stream_end=on_turn_stream_end
            if (self._activity_handler is not None or self._transcript_handler is not None)
            else None,
        )
        instance_holder["instance"] = instance
        await instance.start_worker()

        self._instances[subagent_id] = instance
        return instance

    def find(self, subagent_id: str) -> SubagentInstance | None:
        return self._instances.get(subagent_id)

    def get(self, subagent_id: str) -> SubagentInstance:
        instance = self.find(subagent_id)
        if instance is None:
            raise_subagent_not_found(subagent_id)
        return instance

    async def remove(
        self,
        subagent_id: str,
        *,
        reason: str = "manual",
    ) -> SubagentInstance | None:
        instance = self._instances.get(subagent_id)
        if instance is None:
            return None
        await instance.shutdown(reason)
        if self._instances.get(subagent_id) is instance:
            self._instances.pop(subagent_id, None)
            self._projectors.pop(subagent_id, None)
            self._transcript_projectors.pop(subagent_id, None)
        return instance

    def list_ids(self) -> list[str]:
        return list(self._instances.keys())

    async def restore(
        self,
        *,
        subagent_type: str,
        subagent_id: str,
        parent_session_id: str,
        display_name: str,
        role: str,
        browser_capabilities: list[str] | None = None,
    ) -> SubagentInstance:
        """Rebuild a subagent instance through the bound provider factory."""
        existing = self.find(subagent_id)
        if existing is not None and not existing.is_closed():
            return existing

        request = SubagentBuildRequest(
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            display_name=display_name,
            role=role,
            browser_capabilities=(
                tuple(browser_capabilities)
                if browser_capabilities is not None
                else None
            ),
        )
        if not await self._execution_factory.can_restore(
            request,
            self._parent_context(parent_session_id),
        ):
            raise_subagent_not_found(subagent_id)

        return await self.create(
            subagent_type=subagent_type,
            subagent_id=subagent_id,
            parent_session_id=parent_session_id,
            display_name=display_name,
            role=role,
            browser_capabilities=browser_capabilities,
        )
