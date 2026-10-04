"""Actual TaskManager scan-to-dispatch admission across the scheduler lock."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.core.controller.config import ControllerConfig
from openjiuwen.core.controller.modules.task_manager import TaskFilter, TaskManager
from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
from openjiuwen.core.controller.schema import Task, TaskStatus


async def pending_scan(monkeypatch):
    config = ControllerConfig(schedule_interval=60)
    manager = TaskManager(config)
    original = Task(task_id="same-id", session_id="original", task_type="fixture", status=TaskStatus.SUBMITTED)
    await manager.add_task(original)
    stored = manager.tasks["same-id"]
    owner = TaskScheduler(config, manager, Mock(), Mock(), Mock(), Mock())
    old_session, new_session = object(), object()
    owner._sessions.update(original=old_session, replacement=new_session)
    queried, scanned, started, release = (asyncio.Event() for _ in range(4))
    calls = []
    real_capture = manager._capture_submitted_tasks

    async def observe_query():
        tasks = await real_capture()
        queried.set()
        return tasks

    class ScanEvent(asyncio.Event):
        def clear(self):
            super().clear()
            scanned.set()

    async def execute(task_id, session):
        calls.append((task_id, session))
        started.set()
        await release.wait()

    monkeypatch.setattr(manager, "_capture_submitted_tasks", observe_query)
    monkeypatch.setattr(owner, "_execute_task_wrapper", execute)
    owner._submit_event = ScanEvent()
    await owner._lock.acquire()
    await owner.start()
    await asyncio.wait_for(queried.wait(), 1)
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "replace", "remove", "unchanged"])
async def test_original_scan_rechecks_exact_submitted_task_after_lock(monkeypatch, change):
    case = await pending_scan(monkeypatch)
    owner, manager = case.owner, case.manager
    try:
        if change == "cancel":
            assert await owner.cancel_task("same-id")
            assert case.stored.status is TaskStatus.CANCELED
        elif change in {"replace", "remove"}:
            await manager.remove_task(TaskFilter(task_id="same-id"))
            if change == "replace":
                await manager.add_task(case.original.model_copy(update={"session_id": "replacement"}))
                replacement = (await manager.get_task(task_filter=TaskFilter(task_id="same-id")))[0]
                assert replacement is not case.stored and replacement.status is TaskStatus.SUBMITTED
        owner._lock.release()
        await asyncio.wait_for(case.scanned.wait(), 1)
        await asyncio.sleep(0)  # Let any illegally created wrapper enter its recorder.
        if change == "unchanged":
            assert case.calls == [("same-id", case.old_session)]
        else:
            assert case.calls == []
            assert not owner._running_tasks and not owner._owned_execution_tasks
        if change == "replace":
            owner._submit_event.set()
            await asyncio.wait_for(case.started.wait(), 1)
            assert case.calls == [("same-id", case.new_session)]
    finally:
        if owner._lock.locked():
            owner._lock.release()
        case.release.set()
        await owner.stop()


@pytest.mark.asyncio
async def test_public_query_stays_detached_and_private_capture_uses_original_order():
    manager = TaskManager(ControllerConfig())
    for task_id in ("one", "two", "three"):
        await manager.add_task(Task(task_id=task_id, session_id="s", task_type="fixture", status=TaskStatus.SUBMITTED))
    public = await manager.get_task(TaskFilter(status=TaskStatus.SUBMITTED))
    captured = await manager._capture_submitted_tasks()
    assert [task.task_id for task in public] == [task.task_id for task in captured]
    assert all(task is manager.tasks[task.task_id] for task in captured)
    assert all(task is not manager.tasks[task.task_id] for task in public)
    public[0].status = TaskStatus.CANCELED
    assert manager._is_submitted_task(captured[0])
    await manager.update_task_status(captured[0].task_id, TaskStatus.CANCELED)
    assert not manager._is_submitted_task(captured[0])
    snapshot = await manager.get_state()
    assert all(task is not manager.tasks[task_id] for task_id, task in snapshot.tasks.items())


@pytest.mark.asyncio
async def test_session_is_resolved_after_scheduler_lock_wait(monkeypatch):
    case = await pending_scan(monkeypatch)
    try:
        case.owner._sessions["original"] = case.new_session
        case.owner._lock.release()
        await asyncio.wait_for(case.started.wait(), 1)
        assert case.calls == [("same-id", case.new_session)]
    finally:
        if case.owner._lock.locked():
            case.owner._lock.release()
        case.release.set()
        await case.owner.stop()


@pytest.mark.asyncio
async def test_stop_during_real_capture_never_creates_wrapper(monkeypatch):
    config = ControllerConfig()
    manager = TaskManager(config)
    await manager.add_task(Task(task_id="pending", session_id="s", task_type="fixture", status=TaskStatus.SUBMITTED))
    owner = TaskScheduler(config, manager, Mock(), Mock(), Mock(), Mock())
    owner._sessions["s"] = object()
    captured, cancelled, release = (asyncio.Event() for _ in range(3))
    actual_capture = manager._capture_submitted_tasks
    calls = []

    async def delayed_capture():
        tasks = await actual_capture()
        captured.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return tasks

    async def execute(*args):
        calls.append(args)

    monkeypatch.setattr(manager, "_capture_submitted_tasks", delayed_capture)
    monkeypatch.setattr(owner, "_execute_task_wrapper", execute)
    await owner.start()
    await asyncio.wait_for(captured.wait(), 1)
    stop = asyncio.create_task(owner.stop())
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not stop.done()
        release.set()
        await asyncio.wait_for(stop, 2)
        assert calls == [] and not owner._owned_execution_tasks and not owner._running_tasks
    finally:
        release.set()
        await stop
        await owner.stop()
