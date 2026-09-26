# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compatibility adapter for the existing Native interaction supervisor."""

from __future__ import annotations

from typing import Awaitable, Callable

from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.schema.interaction import InteractionEvent, RoundWorkItem
from openjiuwen.harness.task_loop.event_manager import EventManager


class NativeGoalExecutionAdapter:
    """Preserve Native output attachment and EventManager ownership."""

    def __init__(
        self,
        *,
        event_manager: EventManager,
        has_output_stream: Callable[[], bool],
        cancel_active_round: Callable[..., Awaitable[None]],
        emit_event: Callable[[InteractionEvent], None],
        notify_work: Callable[[], None],
        language: str = "cn",
    ) -> None:
        self._event_manager = event_manager
        self._has_output_stream = has_output_stream
        self._cancel_active_round = cancel_active_round
        self._emit_event = emit_event
        self._notify_work = notify_work
        self._language = language

    def is_available(self) -> bool:
        """Keep Native's existing output-consumer admission requirement."""
        return self._has_output_stream()

    def ensure_work(self, record: GoalRecord) -> bool:
        """Queue through the existing supervisor's deduplicating EventManager."""
        from openjiuwen.harness.prompts.sections.goal import build_goal_task_query

        queued = self._event_manager.push_goal(
            RoundWorkItem.goal(
                inputs={"query": build_goal_task_query(record, self._language)},
                goal_id=record.goal_id,
                revision=record.revision,
                session_id=record.session_id,
            )
        )
        if queued:
            self._notify_work()
        return queued

    def discard_work(self, *, session_id: str, goal_id: str) -> None:
        """Remove only pending work for this session and goal."""
        self._event_manager.discard_goal_work(session_id=session_id, goal_id=goal_id)

    def has_running_attempt(self, *, goal_id: str) -> bool:
        """Include dequeued work so pause/resume cannot schedule a duplicate."""
        return self._event_manager.has_running_goal(goal_id=goal_id)

    async def cancel_attempt(self, *, goal_id: str, reason: str) -> None:
        """Use Native's existing cancellation and exit confirmation path."""
        await self._cancel_active_round(expected_run_kind="goal", expected_goal_id=goal_id, reason=reason)

    def goal_updated(self, record: GoalRecord | None) -> None:
        """Emit the existing interaction event and unchanged payload shape."""
        self._emit_event(InteractionEvent.goal_updated(record.to_dict() if record is not None else None))


__all__ = ["NativeGoalExecutionAdapter"]
