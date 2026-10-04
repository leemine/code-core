"""Real Round controller/manager/scheduler/executor; only model and transport IO are synthetic."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.controller.config import ControllerConfig
from openjiuwen.core.controller.modules.task_manager import TaskManager
from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
from openjiuwen.core.controller.schema.event import EventType
from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness.task_loop.task_loop_controller import TaskLoopController
from openjiuwen.harness.task_loop.task_loop_event_executor import DEEP_TASK_TYPE, build_deep_executor
from tests.unit_tests.harness.test_deep_agent_event_executor import _make_agent, FakeSession


async def pipeline(monkeypatch):
    agent = _make_agent()
    agent._interaction_started = True
    session = FakeSession("owned-pipeline")
    session.write_stream = AsyncMock()
    agent._interaction_session = session
    handler = agent.event_handler
    config = ControllerConfig(schedule_interval=60)
    manager = TaskManager(config)
    loop = TaskLoopController()
    loop._card = agent.card
    loop._event_handler = handler
    agent._loop_controller = loop
    handler.task_manager = manager
    events = []
    async def publish(_id, actual_session, event):
        events.append(event)
        inputs = SimpleNamespace(event=event, session=actual_session)
        if event.event_type == EventType.INPUT:
            return await handler.handle_input(inputs)
        if event.event_type == EventType.TASK_COMPLETION:
            return await handler.handle_task_completion(inputs)
        if event.event_type == EventType.TASK_FAILED:
            return await handler.handle_task_failed(inputs)
        raise AssertionError(event.event_type)
    queue = SimpleNamespace(publish_event=publish)
    loop._event_queue = queue
    scheduler = TaskScheduler(config, manager, Mock(), Mock(), queue, agent.card)
    manager.set_on_task_submitted(scheduler._submit_event.set)
    scheduler._sessions[session.get_session_id()] = session
    scheduler._task_executor_registry.add_task_executor(DEEP_TASK_TYPE, build_deep_executor(agent))
    scheduler._ensure_session_completion_signal = AsyncMock()
    loop._task_scheduler = scheduler
    handler.task_scheduler = scheduler
    monkeypatch.setattr(agent, "prepare_interaction_task_loop", AsyncMock(return_value=(agent.loop_coordinator, loop)))
    monkeypatch.setattr(agent, "_notify_work", lambda: None)
    monkeypatch.setattr(agent._interaction_output, "has_consumer", lambda: True)
    monkeypatch.setattr(agent, "_emit_round_boundary", AsyncMock(return_value=False))
    monkeypatch.setattr(agent, "_write_round_result_to_stream", AsyncMock())
    monkeypatch.setattr(agent, "_has_remaining_tasks", lambda *_: False)
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError("original owner revoked")
    source = ExecutionOrigin(object(), _checker=check)
    await scheduler.start()
    with execution_origin_scope(source):
        await agent.send_input(SendInputRequest("original", {"query": "synthetic"}))
    work = agent.event_manager.next_work()
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_actual_pipeline_preserves_origin_and_waits_wrapper_tail(monkeypatch):
    case = await pipeline(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def tail(*_):
        entered.set()
        await release.wait()
    case.scheduler._ensure_session_completion_signal = tail
    executing = asyncio.create_task(case.agent._execute_round(case.work))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        owned = case.agent._capture_owned_round(case.source)
        assert owned._submission_task.done()
        assert owned._scheduler_wrapper is not None and not owned._scheduler_wrapper.done()
        assert not executing.done()
        case.agent._write_round_result_to_stream.assert_not_called()
        release.set()
        await asyncio.wait_for(executing, 2)
        assert owned._scheduler_wrapper.done()
        assert case.agent.react_agent.invoke_calls[0]["query"] == "synthetic"
        assert all(event.execution_origin is case.source for event in case.events)
        case.agent._write_round_result_to_stream.assert_awaited_once()
    finally:
        release.set()
        if not executing.done():
            executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)
        await case.scheduler.stop()


@pytest.mark.asyncio
async def test_actual_handler_waiting_for_insert_rechecks_original_owner(monkeypatch):
    case = await pipeline(monkeypatch)
    entered = asyncio.Event()
    original_add = case.manager._add_tasks
    async def adding(*args, **kwargs):
        entered.set()
        return await original_add(*args, **kwargs)
    monkeypatch.setattr(case.manager, "_add_tasks", adding)
    await case.manager._lock.acquire()
    executing = asyncio.create_task(case.agent._execute_round(case.work))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        owned = case.agent._capture_owned_round(case.source)
        assert owned._submission_task is not None and not owned._submission_task.done()
        case.live[0] = False
        case.manager._lock.release()
        await asyncio.wait_for(executing, 2)
        assert case.agent.react_agent.invoke_calls == []
        assert owned._task_capture.stored.status.value == "canceled"
    finally:
        if case.manager._lock.locked():
            case.manager._lock.release()
        if not executing.done():
            executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)
        await case.scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "replace"])
async def test_actual_executor_rail_wait_never_invokes_for_changed_origin(monkeypatch, change):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
    from openjiuwen.core.controller.schema.task import TaskStatus

    case = await pipeline(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original_fire = AgentCallbackContext.fire
    async def fire(ctx, event, *args, **kwargs):
        if event is AgentCallbackEvent.BEFORE_TASK_ITERATION:
            entered.set()
            await release.wait()
        return await original_fire(ctx, event, *args, **kwargs)
    monkeypatch.setattr(AgentCallbackContext, "fire", fire)
    executing = asyncio.create_task(case.agent._execute_round(case.work))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        owned = case.agent._capture_owned_round(case.source)
        if change == "revoke":
            case.live[0] = False
        else:
            original = case.manager.tasks[owned.task_id]
            await case.manager.update_task(original.model_copy(update={"description": "replacement", "status": TaskStatus.PAUSED}))
        release.set()
        if change == "replace":
            # A replacement must not receive an old failure event. Its old
            # completion future may therefore stay unresolved until cancellation.
            for _ in range(20):
                await asyncio.sleep(0)
                if owned._scheduler_wrapper.done():
                    break
            executing.cancel()
        await asyncio.wait_for(executing, 2)
        assert case.agent.react_agent.invoke_calls == []
        if change == "replace":
            assert case.manager.tasks[owned.task_id].description == "replacement"
            assert case.manager.tasks[owned.task_id].status is TaskStatus.PAUSED
            assert case.manager.tasks[owned.task_id].error_message is None
    finally:
        release.set()
        if not executing.done():
            executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)
        await case.scheduler.stop()


@pytest.mark.asyncio
async def test_replaced_before_handler_receipt_return_keeps_exit_unknown(monkeypatch):
    from openjiuwen.core.controller.schema.task import TaskStatus
    from openjiuwen.harness.deep_agent import _OwnedRoundExitUnconfirmed
    case = await pipeline(monkeypatch)
    inserted, release = asyncio.Event(), asyncio.Event()
    original_add = case.manager._add_tasks
    async def delayed(*args, **kwargs):
        captures = await original_add(*args, **kwargs)
        inserted.set()
        await release.wait()
        return captures
    monkeypatch.setattr(case.manager, "_add_tasks", delayed)
    finish = AsyncMock()
    monkeypatch.setattr(case.agent._interaction_output, "finish_current", finish)
    executing = asyncio.create_task(case.agent._execute_round(case.work))
    try:
        await asyncio.wait_for(inserted.wait(), 2)
        owned = case.agent._capture_owned_round(case.source)
        original = case.manager.tasks[owned.task_id]
        await case.manager.update_task(original.model_copy(update={"description": "replacement", "status": TaskStatus.PAUSED}))
        release.set()
        with pytest.raises(_OwnedRoundExitUnconfirmed):
            await asyncio.wait_for(executing, 2)
        assert case.agent._capture_owned_round(case.source) is owned
        assert owned._task_capture.stored is original
        case.agent._write_round_result_to_stream.assert_not_called()
        case.agent._emit_round_boundary.assert_not_called()
        finish.assert_not_called()
        assert case.agent.react_agent.invoke_calls == []
        assert case.manager.tasks[owned.task_id].status is TaskStatus.PAUSED
    finally:
        release.set()
        await asyncio.gather(executing, return_exceptions=True)
        await case.scheduler.stop()


@pytest.mark.asyncio
async def test_actual_multiple_followups_reuse_original_turn_source(monkeypatch):
    case = await pipeline(monkeypatch)
    original_invoke = case.agent.react_agent.invoke
    async def invoke(*args, **kwargs):
        if not case.agent.react_agent.invoke_calls:
            case.loop.enqueue_follow_up("second", origin=case.source)
            case.loop.enqueue_follow_up("third", origin=case.source)
        return await original_invoke(*args, **kwargs)
    monkeypatch.setattr(case.agent.react_agent, "invoke", invoke)
    try:
        work = case.work
        for _ in range(3):
            assert work.execution_origin is case.source
            await asyncio.wait_for(case.agent._execute_round(work), 2)
            work = case.agent.event_manager.next_work()
        assert work is None
        assert [call["query"] for call in case.agent.react_agent.invoke_calls] == ["synthetic", "second", "third"]
        assert all(event.execution_origin is case.source for event in case.events)
    finally:
        await case.scheduler.stop()


@pytest.mark.asyncio
async def test_unstarted_receipt_cas_blocks_already_scanned_dispatch(monkeypatch):
    from openjiuwen.core.controller.schema.task import TaskStatus
    case = await pipeline(monkeypatch)
    scanned = asyncio.Event()
    original_scan = case.manager._capture_submitted_tasks
    async def scan():
        tasks = await original_scan()
        if tasks:
            scanned.set()
        return tasks
    monkeypatch.setattr(case.manager, "_capture_submitted_tasks", scan)
    await case.scheduler._lock.acquire()
    owned = case.agent._prepare_owned_round(case.work)
    owned._submission_task = asyncio.create_task(case.loop.submit_round(
        case.session, "synthetic", task_id=owned.task_id, origin=case.source,
    ))
    try:
        await asyncio.wait_for(owned._submission_task, 1)
        await asyncio.wait_for(scanned.wait(), 1)
        assert owned._task_capture.stored.status is TaskStatus.SUBMITTED
        await case.agent._drain_owned_round(owned, cancel=True)
        assert owned._task_capture.stored.status is TaskStatus.CANCELED
        case.scheduler._lock.release()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert case.scheduler._owned_execution_tasks == {}
        assert case.agent.react_agent.invoke_calls == []
    finally:
        if case.scheduler._lock.locked():
            case.scheduler._lock.release()
        await case.scheduler.stop()
