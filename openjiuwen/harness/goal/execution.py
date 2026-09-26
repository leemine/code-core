# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host execution seam for goals; no scheduler or provider event consumer."""

from __future__ import annotations

from typing import Protocol

from openjiuwen.harness.goal.schema import GoalRecord


class GoalExecutionPort(Protocol):
    """Delegate work to the host's existing, single session execution owner.

    Methods run under GoalManager's shared control lock and must not re-enter
    it. ``ensure_work`` must deduplicate queued/dequeued/running work. Hosts
    validate their own generation, checkpoint and pending interactions before
    admitting work; availability alone never proves safe cold recovery.
    """

    def is_available(self) -> bool:
        """Whether the host currently admits goal work and state notifications."""

    def ensure_work(self, record: GoalRecord) -> bool:
        """Request work once through the existing runtime, returning if queued."""

    def discard_work(self, *, session_id: str, goal_id: str) -> None:
        """Discard pending work, without revoking a finishing attempt."""

    def has_running_attempt(self, *, goal_id: str) -> bool:
        """Whether the original host still owns a dequeued or active attempt."""

    async def cancel_attempt(self, *, goal_id: str, reason: str) -> None:
        """Cancel through the host's existing cancellation/exit confirmation."""

    def goal_updated(self, record: GoalRecord | None) -> None:
        """Project a committed state snapshot without becoming a state writer."""


__all__ = ["GoalExecutionPort"]
