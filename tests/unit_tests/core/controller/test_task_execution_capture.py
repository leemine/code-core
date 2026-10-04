"""Original dispatch capture survives status changes, never replacement tasks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.controller.config import ControllerConfig
from openjiuwen.core.controller.modules.task_manager import (
    TaskManager, TaskFilter, _current_task_execution, _task_execution_scope,
)
from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
from openjiuwen.core.controller.schema import Task, TaskStatus
from openjiuwen.core.controller.schema.event import InputEvent
from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin


async def setup():
    manager = TaskManager(ControllerConfig())
    source = ExecutionOrigin(object())
    task = Task(task_id="original", session_id="session", task_type="deep_agent_task",
                description="original", status=TaskStatus.SUBMITTED,
                inputs=[InputEvent.from_user_input("original").with_execution_origin(source)])
    await manager.add_task(task)
    capture = manager._capture_task_execution(manager.tasks[task.task_id])
    session = SimpleNamespace(get_session_id=lambda: "session", write_stream=AsyncMock())
    return manager, capture, session


@pytest.mark.asyncio
async def test_capture_is_internal_storage_identity_not_public_query_copy():
    manager, capture, session = await setup()
    query = (await manager.get_task(TaskFilter(task_id="original")))[0]
    assert query is not manager.tasks["original"]
    with pytest.raises(BaseError, match="replaced"):
        manager._capture_task_execution(query)
    with _task_execution_scope(capture, session):
        assert _current_task_execution(manager, "original", session) is capture
        await manager._set_task_execution_status(capture, TaskStatus.WORKING)
        manager.tasks["original"].outputs = []
        manager.tasks["original"].error_message = "runtime diagnostic"
        read = await manager._read_task_execution(capture)
        assert read.status is TaskStatus.WORKING
        assert read.inputs[0].execution_origin is capture.origin


@pytest.mark.asyncio
async def test_replacement_cannot_receive_old_status_or_error():
    manager, capture, session = await setup()
    replacement = (await manager.get_task(TaskFilter(task_id="original")))[0]
    replacement.description = "new dispatch"
    await manager.update_task(replacement)
    with _task_execution_scope(capture, session):
        with pytest.raises(BaseError, match="record changed"):
            await manager._set_task_execution_status(capture, TaskStatus.FAILED, error_message="old failure")
        with pytest.raises(BaseError, match="record changed"):
            await manager._read_task_execution(capture)
    assert manager.tasks["original"].status is TaskStatus.SUBMITTED
    assert manager.tasks["original"].error_message is None
    new = manager._capture_task_execution(manager.tasks["original"])
    assert (await manager._read_task_execution(new)).description == "new dispatch"


@pytest.mark.asyncio
async def test_source_replacement_and_in_place_input_change_are_rejected():
    manager, capture, _ = await setup()
    manager.tasks["original"].inputs[0] = manager.tasks["original"].inputs[0].with_execution_origin(ExecutionOrigin(object()))
    with pytest.raises(BaseError, match="record changed"):
        await manager._read_task_execution(capture)
    manager.tasks["original"].inputs = capture.snapshot.model_copy(deep=True).inputs
    manager.tasks["original"].description = "changed in place"
    with pytest.raises(BaseError, match="record changed"):
        await manager._read_task_execution(capture)


@pytest.mark.asyncio
async def test_inherited_expired_scope_never_falls_back_to_legacy():
    manager, capture, session = await setup()
    release = asyncio.Event()
    async def late():
        await release.wait()
        return _current_task_execution(manager, "original", session)
    with _task_execution_scope(capture, session):
        child = asyncio.create_task(late())
    release.set()
    with pytest.raises(BaseError, match="scope is no longer valid"):
        await child
    assert _current_task_execution(manager, "original", session) is None


@pytest.mark.asyncio
async def test_wrapper_preserves_execute_task_override_and_exact_scope():
    manager, capture, session = await setup()
    seen = []
    class CustomScheduler(TaskScheduler):
        async def execute_task(self, task_id, actual_session):
            seen.append(_current_task_execution(self._task_manager, task_id, actual_session))
            await self._set_execution_status(task_id, actual_session, TaskStatus.COMPLETED)
    scheduler = CustomScheduler(ControllerConfig(), manager, Mock(), Mock(), Mock(), Mock())
    scheduler._ensure_session_completion_signal = AsyncMock()
    await scheduler._execute_task_wrapper("original", session, _capture=capture)
    assert seen == [capture]
    assert manager.tasks["original"].status is TaskStatus.COMPLETED
    assert _current_task_execution(manager, "original", session) is None


@pytest.mark.asyncio
async def test_wrapper_rejects_same_id_replacement_before_original_execute():
    manager, capture, session = await setup()
    scheduler = TaskScheduler(ControllerConfig(), manager, Mock(), Mock(), Mock(), Mock())
    replacement = (await manager.get_task(TaskFilter(task_id="original")))[0]
    replacement.description = "successor"
    await manager.update_task(replacement)
    with pytest.raises(BaseError, match="record changed"):
        await scheduler._execute_task_wrapper("original", session, _capture=capture)
    assert manager.tasks["original"].status is TaskStatus.SUBMITTED
    assert manager.tasks["original"].error_message is None
    session.write_stream.assert_not_called()


@pytest.mark.asyncio
async def test_pause_then_existing_resume_intent_gets_new_dispatch_capture():
    from openjiuwen.core.controller.modules.intent_recognizer import EventHandlerWithIntentRecognition
    from openjiuwen.core.controller.schema import Intent, IntentType

    manager, capture, session = await setup()
    await manager._set_task_execution_status(capture, TaskStatus.PAUSED)
    handler = object.__new__(EventHandlerWithIntentRecognition)
    handler._task_manager = manager
    await handler._process_resume_task_intent(
        Intent(intent_type=IntentType.RESUME_TASK, target_task_id="original", event=InputEvent.from_user_input("resume")), session,
    )
    assert manager.tasks["original"].status is TaskStatus.SUBMITTED
    with pytest.raises(BaseError, match="record changed"):
        await manager._read_task_execution(capture)
    resumed = manager._capture_task_execution(manager.tasks["original"])
    assert resumed.stored is not capture.stored
    with _task_execution_scope(resumed, session):
        await manager._set_task_execution_status(resumed, TaskStatus.WORKING)
        assert _current_task_execution(manager, "original", session) is resumed


@pytest.mark.asyncio
async def test_add_receipt_never_adopts_replacement_during_return_wait():
    manager = TaskManager(ControllerConfig())
    original_add = manager._add_tasks
    stored, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        value = await original_add(*args, **kwargs)
        stored.set()
        await release.wait()
        return value
    manager._add_tasks = delayed
    task = Task(task_id="same", session_id="s", task_type="fixture", status=TaskStatus.SUBMITTED)
    adding = asyncio.create_task(manager._add_task_execution(task))
    await stored.wait()
    original = manager.tasks["same"]
    replacement = task.model_copy(update={"description": "successor"})
    await manager.update_task(replacement)
    release.set()
    receipt = await adding
    assert receipt.stored is original
    with pytest.raises(BaseError, match="record changed"):
        await manager._set_task_execution_status(receipt, TaskStatus.CANCELED)
    assert manager.tasks["same"].description == "successor"
    assert manager.tasks["same"].status is TaskStatus.SUBMITTED


@pytest.mark.asyncio
async def test_old_wrapper_finally_does_not_remove_new_running_owner():
    manager, capture, session = await setup()
    owner = TaskScheduler(ControllerConfig(), manager, Mock(), Mock(), Mock(), Mock())
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(*_):
        entered.set()
        await release.wait()
    owner.execute_task = execute
    owner._ensure_session_completion_signal = AsyncMock()
    old = asyncio.create_task(owner._execute_task_wrapper("original", session, _capture=capture))
    await entered.wait()
    new = asyncio.create_task(asyncio.Event().wait())
    owner._running_tasks["original"] = (None, new)
    replacement = capture.snapshot.model_copy(update={"description": "new"})
    await manager.update_task(replacement)
    try:
        release.set()
        await old
        assert owner._running_tasks["original"][1] is new
        owner._ensure_session_completion_signal.assert_not_called()
    finally:
        new.cancel()
        await asyncio.gather(new, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_scan_receipt_rejects_replacement_before_wrapper_execute():
    manager, _, session = await setup()
    owner = TaskScheduler(ControllerConfig(schedule_interval=60), manager, Mock(), Mock(), Mock(), Mock())
    owner._sessions["session"] = session
    wrapper_entered, release = asyncio.Event(), asyncio.Event()
    original_wrapper = owner._execute_task_wrapper
    async def delayed(task_id, actual_session, *, _capture):
        wrapper_entered.set()
        await release.wait()
        await original_wrapper(task_id, actual_session, _capture=_capture)
    owner._execute_task_wrapper = delayed
    await owner.start()
    try:
        await asyncio.wait_for(wrapper_entered.wait(), 1)
        original_wrapper_task = owner._owned_execution_tasks["original"]
        await owner._task_manager.update_task(Task(task_id="original", session_id="session", task_type="new", status=TaskStatus.PAUSED))
        release.set()
        result = await asyncio.gather(original_wrapper_task, return_exceptions=True)
        assert isinstance(result[0], BaseError)
        assert manager.tasks["original"].status is TaskStatus.PAUSED
        assert manager.tasks["original"].error_message is None
        session.write_stream.assert_not_called()
    finally:
        release.set()
        await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["task", "session"])
async def test_completion_query_wait_cannot_finish_a_replacement(monkeypatch, change):
    manager, capture, session = await setup()
    owner = TaskScheduler(ControllerConfig(), manager, Mock(), Mock(), Mock(), Mock())
    owner._sessions["session"] = session
    entered, release = asyncio.Event(), asyncio.Event()
    async def all_done(*_):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(owner, "_are_all_tasks_completed", all_done)
    async def complete():
        with _task_execution_scope(capture, session):
            await owner._ensure_session_completion_signal("session")
    finishing = asyncio.create_task(complete())
    await entered.wait()
    other = SimpleNamespace(get_session_id=lambda: "session", write_stream=AsyncMock())
    if change == "task":
        await manager.update_task(capture.snapshot.model_copy(update={"description": "new"}))
    else:
        owner._sessions["session"] = other
    release.set()
    await finishing
    session.write_stream.assert_not_called()
    other.write_stream.assert_not_called()
