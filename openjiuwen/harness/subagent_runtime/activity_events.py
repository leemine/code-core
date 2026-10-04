# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Subagent activity payloads and parent-session stream emission."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
from openjiuwen.harness.subagent_runtime.models import SubagentActivity

SUBAGENT_ACTIVITY_EVENT_TYPE = "subagent_activity"
_MAX_CONSECUTIVE_FAILURES = 3


def build_activity_payload(activity: SubagentActivity) -> dict[str, Any]:
    """Build the external subagent activity payload."""
    return activity.to_dict()


async def emit_subagent_activity(
    session: Session,
    *,
    projection: dict[str, Any],
) -> None:
    """Write one subagent activity update to the parent session stream."""
    await session.write_stream(
        OutputSchema(
            type=SUBAGENT_ACTIVITY_EVENT_TYPE,
            index=0,
            payload={"subagent_activity": projection},
        )
    )


@dataclass(eq=False)
class _ActivityEmission:
    activity: SubagentActivity
    operation: Any
    done: asyncio.Event = field(default_factory=asyncio.Event)


class ActivityEmitter:
    """Bounded queue + background drain for subagent activity events."""

    def __init__(
        self,
        session: Session,
        *,
        config: SubagentRuntimeConfig,
    ) -> None:
        self._session = session
        self._config = config
        self._queue: asyncio.Queue[SubagentActivity | _ActivityEmission] = asyncio.Queue(
            maxsize=config.activity_queue_size,
        )
        self._drain_task: asyncio.Task[None] | None = None
        self._current_item: _ActivityEmission | SubagentActivity | None = None
        self._disabled = False
        self._consecutive_failures = 0
        self.dropped = 0

    @property
    def disabled(self) -> bool:
        return self._disabled

    def start(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_loop())

    def offer(self, activity: SubagentActivity) -> None:
        if self._disabled:
            return
        item = _ActivityEmission(activity, activity._operation) if activity._operation is not None else activity
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
                self._finish_item(dropped)
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self.dropped += 1

    def _finish_item(self, item):
        if isinstance(item, _ActivityEmission):
            item.done.set()
        self._queue.task_done()

    def _capture_origin_items(self, origin):
        values = tuple(self._queue._queue) + ((self._current_item,) if self._current_item is not None else ())
        result = []
        for item in values:
            if not isinstance(item, _ActivityEmission):
                raise RuntimeError("live activity has no proven operation origin")
            if item.operation.origin is origin:
                result.append(item)
        return tuple(result)

    async def _finish_original_items(self, expected, *, cancel):
        if asyncio.current_task() is self._drain_task:
            raise RuntimeError("activity producer cannot confirm its own exit")
        if cancel:
            for item in expected:
                for index, queued in enumerate(self._queue._queue):
                    if queued is item:
                        del self._queue._queue[index]
                        self._finish_item(item)
                        break
        for item in expected:
            await item.done.wait()

    async def close(self) -> None:
        if self._drain_task is None:
            return
        self._drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._drain_task
        self._drain_task = None

    async def _drain_loop(self) -> None:
        while True:
            item = await self._queue.get()
            self._current_item = item
            activity = item.activity if isinstance(item, _ActivityEmission) else item
            try:
                await emit_subagent_activity(
                    self._session,
                    projection=build_activity_payload(activity),
                )
                self._consecutive_failures = 0
            except Exception as exc:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self._disabled = True
                    logger.warning(
                        "[subagent_activity] emitter disabled after %s failures: %s",
                        self._consecutive_failures,
                        exc,
                    )
                    return
            finally:
                self._finish_item(item)
                if self._current_item is item:
                    self._current_item = None


__all__ = [
    "SUBAGENT_ACTIVITY_EVENT_TYPE",
    "ActivityEmitter",
    "build_activity_payload",
    "emit_subagent_activity",
]
