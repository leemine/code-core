# coding: utf-8
"""Host-neutral Goal control and explicit attempt settlement contracts."""

from __future__ import annotations

import asyncio

import pytest

from openjiuwen.harness.goal import (
    GoalAssessment,
    GoalAssessmentStatus,
    GoalAttemptDriver,
    GoalEvaluator,
    GoalManager,
    GoalRecord,
    GoalStatus,
    GoalStopConfig,
    GoalStopStrategy,
    TokenUsage,
)


class MemoryStore:
    session_id = "session-1"

    def __init__(self):
        self.data = None
        self.commits = 0

    def load(self):
        return GoalRecord.from_dict(self.data) if self.data is not None else None

    def save(self, record):
        self.data = record.to_dict()

    def clear(self):
        self.data = None

    async def commit(self):
        self.commits += 1


class HostExecution:
    """Controlled existing owner; no Native stream, model, clocks or subprocess."""

    def __init__(self):
        self.available = True
        self.pending = None
        self.running = None
        self.queued = 0
        self.updates = []
        self.cancelled = []

    def is_available(self):
        return self.available

    def ensure_work(self, record):
        if self.pending is not None or self.running is not None:
            return False
        self.pending = record
        self.queued += 1
        return True

    def discard_work(self, *, session_id, goal_id):
        if self.pending is not None and self.pending.goal_id == goal_id:
            self.pending = None

    def has_running_attempt(self, *, goal_id):
        return self.running is not None and self.running.goal_id == goal_id

    async def cancel_attempt(self, *, goal_id, reason):
        self.cancelled.append((goal_id, reason))
        if self.running is not None and self.running.goal_id == goal_id:
            self.running = None

    def goal_updated(self, record):
        self.updates.append(record)


def setup(*, store=None, host=None, config=None):
    store = store or MemoryStore()
    host = host or HostExecution()
    manager = GoalManager(store=store, control_lock=asyncio.Lock(), execution=host)
    driver = GoalAttemptDriver(manager, GoalEvaluator(config or GoalStopConfig(strategy=GoalStopStrategy.AGENT_REPORT)))
    return manager, driver, host, store


async def start(manager, driver, host, **kwargs):
    goal = await manager.set("finish with evidence", **kwargs)
    host.running, host.pending = host.pending, None
    started = await driver.begin(goal_id=goal.goal_id, revision=goal.revision, attempt_index=1)
    assert started.attempt_count == 1
    return started


def finish_args(record, status=GoalAssessmentStatus.CONTINUE):
    return dict(
        goal_id=record.goal_id,
        revision=record.revision,
        attempt_index=record.attempt_count,
        outcome="completed",
        agent_report=GoalAssessment(status, "verified evidence"),
    )


@pytest.mark.asyncio
async def test_public_host_runs_without_native_output_and_only_manager_writes():
    manager, driver, host, _ = setup()
    goal = await start(manager, driver, host)
    assert host.queued == 1
    assert await driver.begin(goal_id=goal.goal_id, revision=goal.revision, attempt_index=1) is None
    host.running = None  # Existing owner confirms exit before admitting continuation.
    updated = await driver.finish(**finish_args(goal))
    assert updated.status is GoalStatus.ACTIVE
    assert updated.last_assessed_attempt == 1
    assert host.queued == 2
    host.updates[-1].objective = "mutated consumer snapshot"
    assert (await manager.get()).objective == goal.objective


@pytest.mark.asyncio
async def test_concurrent_duplicate_terminal_usage_and_continuation_commit_once():
    manager, driver, host, store = setup()
    goal = await start(manager, driver, host)
    host.running = None
    before = store.commits
    args = finish_args(goal)
    args["usage"] = TokenUsage(input_tokens=7, output_tokens=3)
    results = await asyncio.gather(driver.finish(**args), driver.finish(**args))
    assert sum(result is not None for result in results) == 1
    assert store.commits == before + 1
    assert host.queued == 2
    assert (await manager.get()).token_usage.total_tokens == 10


@pytest.mark.asyncio
async def test_settlement_receipt_survives_reconstruction_without_automatic_replay():
    manager, driver, host, store = setup()
    goal = await start(manager, driver, host)
    host.running = None
    await driver.finish(**finish_args(goal), usage=TokenUsage(input_tokens=9))
    manager2, driver2, host2, _ = setup(store=store)
    assert host2.queued == 0
    assert await driver2.finish(**finish_args(goal), usage=TokenUsage(input_tokens=9)) is None
    assert (await manager2.get()).token_usage.total_tokens == 9
    assert host2.queued == 0


@pytest.mark.asyncio
async def test_old_record_loads_without_receipt_and_does_not_start():
    store = MemoryStore()
    record = GoalRecord.create(session_id=store.session_id, objective="old goal")
    record.attempt_count = 2
    store.data = record.to_dict()
    del store.data["last_assessed_attempt"]
    manager, _, host, _ = setup(store=store)
    assert (await manager.get()).last_assessed_attempt == 0
    assert (await manager.get()).attempt_count == 2
    assert host.queued == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["interrupted", "cancelled", "stream_closed"])
async def test_noncompletion_keeps_state_and_usage_without_next_attempt(outcome):
    manager, driver, host, store = setup()
    goal = await start(manager, driver, host)
    before = store.data.copy()
    args = finish_args(goal, GoalAssessmentStatus.COMPLETE)
    args["outcome"] = outcome
    with pytest.raises(ValueError, match="accumulate_usage"):
        await driver.finish(**args, usage=TokenUsage(input_tokens=9))
    assert await driver.finish(**args) is None
    assert store.data == before
    assert host.queued == 1
    assert host.running.goal_id == goal.goal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (GoalAssessmentStatus.CONTINUE, GoalStatus.PAUSED),
        (GoalAssessmentStatus.COMPLETE, GoalStatus.COMPLETED),
        (GoalAssessmentStatus.BLOCKED, GoalStatus.BLOCKED),
    ],
)
async def test_pause_allows_finishing_attempt_but_no_continuation(status, expected):
    manager, driver, host, _ = setup()
    goal = await start(manager, driver, host)
    paused = await manager.pause()
    assert paused.revision == goal.revision
    updated = await driver.finish(**finish_args(goal, status), usage=TokenUsage(input_tokens=4))
    assert updated.status is expected
    assert updated.token_usage.total_tokens == 4
    assert host.queued == 1


@pytest.mark.asyncio
async def test_inflight_resume_keeps_attempt_and_idle_resume_rejects_old_revision():
    manager, driver, host, _ = setup()
    goal = await start(manager, driver, host)
    await manager.pause()
    assert (await manager.resume()).revision == goal.revision
    assert host.queued == 1
    await manager.pause()
    await driver.finish(**finish_args(goal))
    host.running = None
    resumed = await manager.resume()
    assert resumed.revision == goal.revision + 1
    assert await driver.finish(**finish_args(goal)) is None
    assert host.queued == 2


@pytest.mark.asyncio
async def test_old_attempt_with_same_revision_cannot_write_current_attempt():
    manager, driver, host, _ = setup()
    first = await start(manager, driver, host)
    host.running = None
    await driver.finish(**finish_args(first))
    second = await driver.begin(goal_id=first.goal_id, revision=first.revision, attempt_index=2)
    assert second.revision == first.revision
    assert await driver.finish(**finish_args(first, GoalAssessmentStatus.COMPLETE)) is None
    await manager.accumulate_usage(goal_id=first.goal_id, revision=first.revision, attempt_index=1, input_tokens=99)
    assert (await manager.get()).token_usage.total_tokens == 0
    assert (await driver.finish(**finish_args(second, GoalAssessmentStatus.COMPLETE))).status is GoalStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace", "clear"])
async def test_old_goal_result_cannot_write_after_replace_or_clear(operation):
    manager, driver, host, _ = setup()
    goal = await start(manager, driver, host)
    if operation == "replace":
        new = await manager.set("replacement", overwrite_confirmed=True)
    else:
        await manager.clear()
        new = None
    assert await driver.finish(**finish_args(goal, GoalAssessmentStatus.COMPLETE)) is None
    current = await manager.get()
    assert current.goal_id == new.goal_id if new else current is None
    assert host.cancelled[-1][0] == goal.goal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits,usage,reason",
    [
        ({"max_attempts": 1}, None, "max_attempts_exhausted"),
        ({"token_budget": 5}, TokenUsage(input_tokens=5), "token_budget_exhausted"),
    ],
)
async def test_budget_and_attempt_limit_block_continue_without_heartbeat_resume(limits, usage, reason):
    manager, driver, host, _ = setup()
    goal = await start(manager, driver, host, **limits)
    result = await driver.finish(**finish_args(goal), usage=usage)
    assert result.status is GoalStatus.BLOCKED
    assert reason in result.last_assessment.evidence
    host.running = None
    # Host availability changes alone cannot resume terminal Goal state.
    host.available = False
    host.available = True
    assert manager.ensure_active_goal_work_locked() is False
    assert host.queued == 1


@pytest.mark.asyncio
async def test_verified_completion_wins_over_exhausted_budget():
    manager, driver, host, _ = setup(config=GoalStopConfig(strategy=GoalStopStrategy.HYBRID))
    goal = await start(manager, driver, host, max_attempts=1)
    result = await driver.finish(
        **finish_args(goal, GoalAssessmentStatus.COMPLETE),
        transcript_response='{"status":"complete","evidence":"tests pass"}',
    )
    assert result.status is GoalStatus.COMPLETED


@pytest.mark.asyncio
async def test_execution_failure_blocks_and_duplicate_failure_is_ignored():
    manager, driver, host, _ = setup()
    goal = await start(manager, driver, host)
    args = finish_args(goal)
    args.update(outcome="failed", execution_error="executor exited")
    result = await driver.finish(**args)
    assert result.status is GoalStatus.BLOCKED
    assert "executor exited" in result.last_assessment.evidence
    assert await driver.finish(**args) is None


def test_two_execution_owners_are_rejected():
    with pytest.raises(ValueError, match="mutually exclusive"):
        GoalManager(
            store=MemoryStore(), control_lock=asyncio.Lock(), execution=HostExecution(), has_output_stream=lambda: True
        )
