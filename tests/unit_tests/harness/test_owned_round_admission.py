"""Original admission fences without real model or Provider processes."""
import asyncio
import copy
import pickle
from unittest.mock import MagicMock

import pytest

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import SendInputRequest


def test_private_origin_checker_preserves_host_and_copy_identity():
    host = object()
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError("original owner ended")
    source = ExecutionOrigin(host, _checker=check)
    assert source.host_value is host
    assert copy.copy(source) is copy.deepcopy(source) is source
    assert repr(source) == "ExecutionOrigin(<host>)"
    source._check_current()
    live[0] = False
    with pytest.raises(PermissionError):
        source._check_current()
    with pytest.raises(TypeError):
        pickle.dumps(source)
    assert ExecutionOrigin(object())._check_current() is None


@pytest.mark.parametrize("bad", [lambda: True, lambda: False, lambda: 1])
def test_private_origin_checker_requires_synchronous_none(bad):
    with pytest.raises(TypeError, match="synchronously"):
        ExecutionOrigin(object(), _checker=bad)._check_current()


def test_private_origin_checker_rejects_async_callback():
    async def check():
        return None
    with pytest.raises(TypeError, match="synchronously"):
        ExecutionOrigin(object(), _checker=check)._check_current()


@pytest.fixture
def agent(monkeypatch):
    value = DeepAgent(AgentCard(name="owned-round", description="synthetic"))
    value._interaction_started = True
    value._interaction_session = MagicMock()
    value._interaction_session.get_session_id.return_value = "session"
    monkeypatch.setattr(value, "_notify_work", lambda: None)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", ["send-lock", "readiness"])
async def test_send_rechecks_original_source_after_admission_wait(agent, waiting):
    entered, release = asyncio.Event(), asyncio.Event()
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError("original turn aborted")
    source = ExecutionOrigin(object(), _checker=check)
    if waiting == "readiness":
        class Ready:
            async def __aenter__(self):
                entered.set()
                await release.wait()
            async def __aexit__(self, *_):
                return None
        agent.set_fresh_input_context_factory(Ready)
    else:
        await agent._interaction_send_lock.acquire()
    with execution_origin_scope(source):
        sending = asyncio.create_task(agent.send_input(SendInputRequest("old", {"query": "old"})))
        if waiting == "readiness":
            await entered.wait()
        else:
            await asyncio.sleep(0)
    live[0] = False
    if waiting == "send-lock":
        agent._interaction_send_lock.release()
    release.set()
    with pytest.raises(PermissionError, match="original turn"):
        await sending
    assert agent._event_manager.next_work() is None
    # The ended source does not fence a different caller's input.
    with execution_origin_scope(ExecutionOrigin(object())):
        await agent.send_input(SendInputRequest("new", {"query": "new"}))
    assert agent._event_manager.next_work().request_id == "new"


@pytest.mark.asyncio
async def test_supervisor_publishes_original_record_before_facade_first_await(agent, monkeypatch):
    from unittest.mock import AsyncMock
    from openjiuwen.harness.schema.interaction import RoundOutcome, RoundWorkItem

    entered, release = asyncio.Event(), asyncio.Event()
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError("original turn fenced")
    source = ExecutionOrigin(object(), _checker=check)
    work = RoundWorkItem.user(request_id="original", inputs={"query": "query"}).with_execution_origin(source)
    agent._event_manager.push_user(work)
    original_execute = agent._execute_round
    async def delayed(work, *, _owned_round):
        entered.set()
        await release.wait()
        await original_execute(work, _owned_round=_owned_round)
    monkeypatch.setattr(agent, "_execute_round", delayed)
    monkeypatch.setattr(agent._interaction_output, "has_consumer", lambda: True)
    monkeypatch.setattr(agent, "_emit_round_boundary", AsyncMock(return_value=False))
    emitted = []
    monkeypatch.setattr(agent, "_emit_interaction_event", emitted.append)
    run = AsyncMock(return_value=RoundOutcome())
    monkeypatch.setattr(agent, "run_one_round", run)
    supervisor = asyncio.create_task(agent._supervisor_loop())
    try:
        await entered.wait()
        captured = agent._capture_owned_round(source, expected_work=work)
        assert captured is agent._active_interaction_round
        assert captured.work is work
        assert captured._facade_task is agent._interaction_round_task
        assert not captured._facade_task.done()
        with pytest.raises(Exception, match="identity changed"):
            agent._capture_owned_round(source, expected_work=copy.copy(work))
        assert agent._capture_owned_round(ExecutionOrigin(object())) is None
        live[0] = False
        release.set()
        await captured._facade_task
        run.assert_not_called()
        assert emitted and emitted[-1].payload["code"] == "round_execution_error"
        assert captured._facade_task.done()
    finally:
        release.set()
        supervisor.cancel()
        await supervisor


def test_event_manager_never_clears_equal_work_from_another_source():
    from openjiuwen.harness.schema.interaction import RoundWorkItem
    from openjiuwen.harness.task_loop.event_manager import EventManager

    first = RoundWorkItem.user(request_id="same", inputs={"query": "same"}).with_execution_origin(ExecutionOrigin(object()))
    second = first.with_execution_origin(ExecutionOrigin(object()))
    assert first == second and first is not second
    manager = EventManager()
    manager.push_user(second)
    assert manager.next_work() is second
    manager.mark_started(first)
    assert manager._dequeued is second
    manager.mark_started(second)
    manager.mark_finished(first)
    assert manager._active is second
    manager.mark_finished(second)
    assert manager._active is None


@pytest.mark.asyncio
async def test_original_submission_cancel_does_not_release_late_handler(agent):
    from openjiuwen.harness.schema.interaction import RoundWorkItem
    started, release = asyncio.Event(), asyncio.Event()
    async def submit():
        started.set()
        await release.wait()
    source = ExecutionOrigin(object(), _checker=lambda: None)
    work = RoundWorkItem.user(request_id="request", inputs={"query": "q"}).with_execution_origin(source)
    owned = agent._prepare_owned_round(work)
    owned._submission_task = asyncio.create_task(submit())
    await started.wait()
    draining = asyncio.create_task(agent._drain_owned_round(owned, _cleanup=True))
    await asyncio.sleep(0)
    draining.cancel()
    await asyncio.sleep(0)
    assert not draining.done() and not owned._submission_task.cancelled()
    assert agent._capture_owned_round(source) is owned
    release.set()
    await asyncio.wait_for(draining, 1)
    assert owned._submission_task.done()


@pytest.mark.asyncio
async def test_cancel_original_wrapper_joins_tail_without_repeat_cancel(agent):
    from openjiuwen.harness.schema.interaction import RoundWorkItem
    running, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellations = []
    async def wrapper():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append("second cancellation")
                raise
    source = ExecutionOrigin(object(), _checker=lambda: None)
    work = RoundWorkItem.user(request_id="request", inputs={"query": "q"}).with_execution_origin(source)
    owned = agent._prepare_owned_round(work)
    owned._scheduler_wrapper = asyncio.create_task(wrapper())
    await running.wait()
    draining = asyncio.create_task(agent._drain_owned_round(owned, cancel=True, _cleanup=True))
    await cleanup.wait()
    draining.cancel()
    await asyncio.sleep(0)
    assert not draining.done() and not owned._scheduler_wrapper.done()
    release.set()
    await asyncio.wait_for(draining, 1)
    assert owned._scheduler_wrapper.cancelled() and cancellations == []


def test_all_followups_keep_same_origin_and_fence_only_that_owner(agent):
    from openjiuwen.harness.schema.interaction import RoundWorkItem
    from openjiuwen.harness.task_loop.loop_queues import LoopQueues
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError("original turn ended")
    original = ExecutionOrigin(object(), _checker=check)
    other = ExecutionOrigin(object(), _checker=lambda: None)
    queues = LoopQueues()
    for text in ("one", "two"):
        queues.push_follow_up(text, origin=original)
    source, batch = queues.drain_sourced_follow_up()
    assert source is original and batch == ["one", "two"]
    live[0] = False
    with pytest.raises(PermissionError):
        queues.push_follow_up("late", origin=original)
    with pytest.raises(PermissionError):
        agent._event_manager.push_user(RoundWorkItem.user(request_id="late", inputs={"query": "q"}).with_execution_origin(original))
    queues.push_follow_up("new", origin=other)
    assert queues.drain_sourced_follow_up() == (other, ["new"])
