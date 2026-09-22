# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native DeepAgent adapter for the provider-neutral subagent execution ports."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.kv_cache.kv_cache_metadata import KV_CACHE_AFFINITY_PARENT_SESSION_ID_ENV
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.harness.kv_cache import kv_cache_subagent_lifecycle
from openjiuwen.harness.kv_cache.kv_cache_subagent_lifecycle import affinity_enabled
from openjiuwen.harness.subagent_lifecycle import (
    cleanup_subagent_task_resources,
    prepare_subagent_task_resources,
)
from openjiuwen.harness.subagent_runtime.ports import (
    ChunkCallback,
    ParentExecutionContext,
    ResultCallback,
    SubagentBuildRequest,
    SubagentExecution,
    SubagentTurnRequest,
    SubagentTurnResult,
)
from openjiuwen.harness.subagent_runtime.stream_output import TurnOutputAggregator


async def _close_session_quietly(session: Any) -> None:
    close_stream = getattr(session, "close_stream", None)
    if not callable(close_stream):
        return
    try:
        result = close_stream()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug(
            "Failed to close subagent session stream quietly: %s",
            exc,
            exc_info=True,
        )


class NativeSubagentExecution:
    """Adapt one DeepAgent child and its per-turn Sessions to the execution port."""

    def __init__(
        self,
        *,
        agent: Any,
        session_factory: Callable[[], Any],
        subagent_id: str,
        parent_session_id: str,
        include_parent_session_id: bool = False,
        on_turn_start: Callable[[Any], Awaitable[None]] | None = None,
        on_turn_finished: Callable[[Any, bool], Awaitable[None]] | None = None,
    ) -> None:
        self._agent = agent
        self._session_factory = session_factory
        self._subagent_id = subagent_id
        self._parent_session_id = parent_session_id
        self._include_parent_session_id = include_parent_session_id
        self._on_turn_start = on_turn_start
        self._on_turn_finished = on_turn_finished
        self._closed = False

    async def run_turn(
        self,
        request: SubagentTurnRequest,
        *,
        on_chunk: ChunkCallback | None = None,
        on_result: ResultCallback,
    ) -> None:
        if self._closed:
            raise RuntimeError("subagent execution is closed")
        session = self._session_factory()
        aggregator = TurnOutputAggregator()
        succeeded = False
        try:
            await session.pre_run()
            await prepare_subagent_task_resources(self._agent)
            if self._on_turn_start is not None:
                await self._on_turn_start(session)
            inputs = {
                "query": request.query,
                "conversation_id": self._subagent_id,
            }
            if self._include_parent_session_id:
                inputs["parent_session_id"] = self._parent_session_id
            stream = self._agent.stream(inputs, session=session)
            async with contextlib.aclosing(stream):
                async for chunk in stream:
                    aggregator.consume(chunk)
                    if on_chunk is not None:
                        await on_chunk(chunk)
            result = SubagentTurnResult(
                output=aggregator.output(),
                reasoning=aggregator.reasoning_text(),
                is_error=aggregator.is_error(),
            )
            await on_result(result)
            succeeded = not result.is_error
        finally:
            task = asyncio.create_task(self._finalize_turn(session, succeeded=succeeded))
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)

    async def close(self, reason: str) -> None:
        _ = reason
        self._closed = True

    async def _finalize_turn(self, session: Any, *, succeeded: bool) -> None:
        await cleanup_subagent_task_resources(self._agent)
        await _close_session_quietly(session)
        if self._on_turn_finished is not None:
            await self._on_turn_finished(session, succeeded)


class NativeSubagentExecutionFactory:
    """Default factory preserving the existing Native-to-Native child path."""

    def __init__(
        self,
        parent_agent: Any,
        *,
        parent_session_getter: Callable[[], Any | None] | None = None,
    ) -> None:
        self._parent_agent = parent_agent
        self._parent_session_getter = parent_session_getter

    async def create(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> SubagentExecution:
        browser_capabilities = (
            list(request.browser_capabilities)
            if request.browser_capabilities is not None
            else None
        )
        subagent = self._parent_agent.create_subagent(
            request.subagent_type,
            request.subagent_id,
            browser_capabilities,
        )
        use_affinity = affinity_enabled(self._parent_agent)
        envs: dict[str, Any] = {}
        if use_affinity:
            envs[KV_CACHE_AFFINITY_PARENT_SESSION_ID_ENV] = context.parent_session_id

        def session_factory() -> Any:
            parent_session = (
                self._parent_session_getter()
                if self._parent_session_getter is not None
                else context.parent_session
            )
            return create_agent_session(
                session_id=request.subagent_id,
                card=subagent.card,
                envs=envs,
                parent_session_id=context.parent_session_id,
                kv_cache_runtime=(
                    parent_session.get_kv_cache_runtime()
                    if parent_session is not None
                    else None
                ),
            )

        on_turn_start = None
        on_turn_finished = None
        if use_affinity:
            async def on_turn_start(session: Any) -> None:
                await kv_cache_subagent_lifecycle.prepare_subagent(
                    session,
                    subagent_type=request.subagent_type,
                )

            async def on_turn_finished(session: Any, succeeded: bool) -> None:
                await kv_cache_subagent_lifecycle.finish_subagent(
                    session,
                    subagent_type=request.subagent_type,
                    succeeded=succeeded,
                )

        return NativeSubagentExecution(
            agent=subagent,
            session_factory=session_factory,
            subagent_id=request.subagent_id,
            parent_session_id=context.parent_session_id,
            include_parent_session_id=use_affinity,
            on_turn_start=on_turn_start,
            on_turn_finished=on_turn_finished,
        )

    async def can_restore(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> bool:
        _ = context
        checkpointer = CheckpointerFactory.get_checkpointer()
        return await checkpointer.session_exists(request.subagent_id)


__all__ = ["NativeSubagentExecution", "NativeSubagentExecutionFactory"]
