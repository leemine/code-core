# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral attempt settlement, delegated by the existing runtime."""

from __future__ import annotations

from typing import Literal

from openjiuwen.harness.goal.evaluation import GoalEvaluator
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.schema import (
    GoalAssessment,
    GoalAssessmentStatus,
    GoalRecord,
    GoalStatus,
    TokenUsage,
)


class GoalAttemptDriver:
    """Apply explicit attempt outcomes without owning an execution loop.

    The host serializes its event consumer and validates session/generation
    and checkpoint before calling this driver. It must not report a stream
    close as a completed turn. Usage supplied to ``finish`` is an attempt
    delta not already recorded by streaming usage callbacks, accepted only
    for completed/failed outcomes. Account usage on interruption/cancellation
    through the manager's streaming path, with the host's event deduplication.
    """

    def __init__(self, manager: GoalManager, evaluator: GoalEvaluator | None = None) -> None:
        self._manager = manager
        self._evaluator = evaluator or GoalEvaluator()

    async def begin(self, *, goal_id: str, revision: int, attempt_index: int) -> GoalRecord | None:
        """Start once after the host grants ownership; never infer cold recovery."""
        return await self._manager.begin_attempt(
            goal_id=goal_id,
            revision=revision,
            attempt_index=attempt_index,
        )

    async def finish(
        self,
        *,
        goal_id: str,
        revision: int,
        attempt_index: int,
        outcome: Literal["completed", "failed", "interrupted", "cancelled", "stream_closed"],
        agent_report: GoalAssessment | None = None,
        transcript_response: str | None = None,
        execution_error: str | None = None,
        usage: TokenUsage | None = None,
    ) -> GoalRecord | None:
        """Settle one confirmed terminal outcome, including optional usage once."""
        if outcome in ("interrupted", "cancelled", "stream_closed"):
            if usage is not None:
                raise ValueError("non-settling outcome usage must be accounted through accumulate_usage")
            return None
        if outcome not in ("completed", "failed"):
            raise ValueError(f"invalid goal attempt outcome: {outcome}")
        record = await self._manager.get()
        if (
            record is None
            or (record.goal_id, record.revision) != (goal_id, revision)
            or record.status not in (GoalStatus.ACTIVE, GoalStatus.PAUSED)
            or not 0 < attempt_index == record.attempt_count
            or record.last_assessed_attempt >= attempt_index
        ):
            return None
        if usage is not None:
            record.token_usage.accumulate(usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
        if outcome == "failed":
            assessment = GoalAssessment(
                status=GoalAssessmentStatus.BLOCKED,
                evidence=f"round_execution_error: {execution_error or 'goal attempt failed'}",
                remaining_work="Resolve the execution error before resuming the goal.",
                next_instruction=(
                    "Fix the underlying failure (for example model or API configuration), then resume the goal."
                ),
            )
        else:
            assessment = self._evaluator.assess(
                record=record,
                agent_report=agent_report,
                transcript_response=transcript_response,
            )
        # Recheck under the manager's control lock: replacement, pause, another
        # settlement or a newer attempt may have won while obtaining the snapshot.
        return await self._manager.apply_assessment(
            goal_id=goal_id,
            revision=revision,
            attempt_index=attempt_index,
            assessment=assessment,
            usage=usage,
        )


__all__ = ["GoalAttemptDriver"]
