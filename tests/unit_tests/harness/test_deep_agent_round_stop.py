"""DeepAgent stop must confirm its own round facade, not just the scheduler."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import ActiveInteractionRound, RoundWorkItem


def make_agent(monkeypatch):
    agent = DeepAgent(AgentCard(name="round-stop", description="synthetic"))
    agent._interaction_started = True
    agent._interaction_session = SimpleNamespace(close_stream=AsyncMock())
    agent._loop_controller = SimpleNamespace(stop=AsyncMock(), unbind_session=AsyncMock(), task_scheduler=None)
    agent.abort = AsyncMock()
    agent._active_interaction_round = ActiveInteractionRound(
        work=RoundWorkItem.user(request_id="original", inputs={"query": "synthetic"}), task_id=""
    )
    monkeypatch.setattr(agent._interaction_output, "shutdown", AsyncMock())
    monkeypatch.setattr(agent, "_notify_work", lambda: None)
    return agent


@pytest.mark.asyncio
async def test_stop_cannot_return_before_round_finally_exits(monkeypatch):
    agent = make_agent(monkeypatch)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()

    original = asyncio.create_task(work())
    agent._interaction_round_task = original
    await entered.wait()
    stopping = asyncio.create_task(agent.stop())
    try:
        await cancelled.wait()
        await asyncio.sleep(0)
        assert not stopping.done(), "stop returned while its original round still owns live cleanup"
        agent.loop_controller.stop.assert_not_awaited()
        release.set()
        await stopping
        assert original.done()
        assert not agent._interaction_started
    finally:
        release.set()
        await asyncio.gather(original, stopping, return_exceptions=True)


async def pending_round(agent):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(work())
    agent._interaction_round_task = task
    await entered.wait()
    return task, cancelled, release


@pytest.mark.asyncio
@pytest.mark.parametrize("active_present", [False, True])
async def test_timeout_retains_original_round_and_resources_until_retry(monkeypatch, active_present):
    from openjiuwen.core.controller.modules import task_scheduler

    monkeypatch.setattr(task_scheduler, "_STOP_TIMEOUT_SECONDS", 0.02)
    agent = make_agent(monkeypatch)
    task, cancelled, release = await pending_round(agent)
    if not active_present:
        agent._active_interaction_round = None
    original_session, original_controller = agent._interaction_session, agent.loop_controller
    try:
        with pytest.raises(BaseError, match="owned round exit is unconfirmed"):
            await agent.stop()
        assert cancelled.is_set() and not task.done()
        assert task in agent._stopping_interaction_round_tasks
        assert agent._interaction_started
        assert agent._interaction_session is original_session
        assert agent.loop_controller is original_controller
        original_session.close_stream.assert_not_awaited()
        original_controller.stop.assert_not_awaited()
        original_controller.unbind_session.assert_not_awaited()
        with pytest.raises(RuntimeError, match="interaction_terminated"):
            await agent.start(session=original_session)
        # A second timeout must not cancel the Task a second time during finally.
        with pytest.raises(BaseError, match="owned round exit is unconfirmed"):
            await agent.stop()
        assert task.cancelling() == 1 and not task.done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await agent.stop()
        assert not agent._interaction_started
        assert not agent._stopping_interaction_round_tasks
        original_controller.stop.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_caller_cancel_keeps_task_even_if_supervisor_clears_pointer(monkeypatch):
    agent = make_agent(monkeypatch)
    task, cancelled, release = await pending_round(agent)
    stopping = asyncio.create_task(agent.stop())
    try:
        await cancelled.wait()
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        agent._interaction_round_task = None
        assert task in agent._stopping_interaction_round_tasks and not task.done()
        agent._interaction_session.close_stream.assert_not_awaited()
        again = asyncio.create_task(agent.stop())
        await asyncio.sleep(0)
        assert not again.done() and task.cancelling() == 1
        release.set()
        await again
        assert task.done() and not agent._stopping_interaction_round_tasks
    finally:
        release.set()
        await asyncio.gather(task, stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_round_is_captured_before_first_shutdown_await(monkeypatch):
    agent = make_agent(monkeypatch)
    task, _, release = await pending_round(agent)
    shutdown_entered = asyncio.Event()

    async def shutdown():
        shutdown_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent._interaction_output, "shutdown", shutdown)
    stopping = asyncio.create_task(agent.stop())
    try:
        await shutdown_entered.wait()
        assert task in agent._stopping_interaction_round_tasks
        agent._interaction_round_task = None
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        monkeypatch.setattr(agent._interaction_output, "shutdown", AsyncMock())
        release.set()
        await agent.stop()
        assert task.done()
    finally:
        release.set()
        await asyncio.gather(task, stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_stop_callers_do_not_repeat_cancel_or_touch_foreign_task(monkeypatch):
    agent = make_agent(monkeypatch)
    original, cancelled, release = await pending_round(agent)
    foreign_release = asyncio.Event()
    foreign = asyncio.create_task(foreign_release.wait())
    first, second = asyncio.create_task(agent.stop()), asyncio.create_task(agent.stop())
    try:
        await cancelled.wait()
        await asyncio.sleep(0)
        assert original.cancelling() == 1
        assert not foreign.done() and not foreign.cancelling()
        release.set()
        await asyncio.gather(first, second)
        agent.loop_controller.stop.assert_awaited_once()
    finally:
        release.set()
        foreign_release.set()
        await asyncio.gather(original, first, second, foreign, return_exceptions=True)


@pytest.mark.asyncio
async def test_owned_round_self_stop_rejects_without_blocking_external_stop(monkeypatch):
    agent = make_agent(monkeypatch)
    entered, self_rejected = asyncio.Event(), asyncio.Event()

    async def work():
        await entered.wait()
        with pytest.raises(BaseError, match="cannot confirm its own stop"):
            await agent.stop()
        assert not agent._stopping_interaction_round_tasks
        self_rejected.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(work())
    agent._interaction_round_task = task
    entered.set()
    await self_rejected.wait()
    await agent.stop()
    assert task.cancelled() and not agent._interaction_started


@pytest.mark.asyncio
async def test_exited_round_cleanup_error_reports_once_then_retry_settles(monkeypatch):
    agent = make_agent(monkeypatch)
    entered = asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("synthetic cleanup failure")

    original = asyncio.create_task(work())
    agent._interaction_round_task = original
    await entered.wait()
    with pytest.raises(BaseError, match="owned interaction round failed during stop"):
        await agent.stop()
    assert original.done() and agent._interaction_started
    assert not agent._stopping_interaction_round_tasks
    assert agent._interaction_round_task is None
    agent.loop_controller.stop.assert_not_awaited()
    await agent.stop()
    assert not agent._interaction_started


@pytest.mark.asyncio
async def test_supervisor_late_promotion_cannot_create_round_after_stop_fence(monkeypatch):
    from openjiuwen.harness.schema.interaction import InteractionPhase

    agent = make_agent(monkeypatch)
    agent._interaction_phase = InteractionPhase.IDLE
    agent._active_interaction_round = None
    monkeypatch.setattr(agent._interaction_output, "has_consumer", lambda: True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def promote():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        agent._event_manager.push_user(RoundWorkItem.user(request_id="late", inputs={"query": "late"}))

    monkeypatch.setattr(agent, "_promote_loop_follow_ups", promote)
    execute = AsyncMock()
    monkeypatch.setattr(agent, "_execute_round", execute)
    supervisor = asyncio.create_task(agent._supervisor_loop())
    agent._interaction_supervisor_task = supervisor
    await entered.wait()
    stopping = asyncio.create_task(agent.stop())
    await asyncio.sleep(0)
    assert agent.phase is InteractionPhase.TERMINATED
    release.set()
    await stopping
    execute.assert_not_awaited()
    assert agent._interaction_round_task is None
