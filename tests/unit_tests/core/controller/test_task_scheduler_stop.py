"""Bounded shutdown of exact owned Tasks, including terminal event tail IO."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.controller.config import ControllerConfig
from openjiuwen.core.controller.modules import task_scheduler as module
from openjiuwen.core.controller.modules.task_manager import TaskFilter
from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
from openjiuwen.core.controller.schema import EventType, TaskStatus


def scheduler():
    manager = SimpleNamespace(get_task=AsyncMock(return_value=[]), update_task_status=AsyncMock())
    manager.tasks = {}

    async def capture():
        tasks = await manager.get_task(task_filter=TaskFilter(status=TaskStatus.SUBMITTED))
        manager.tasks = {task.task_id: task for task in tasks}
        return tasks

    manager._capture_task_execution = lambda _: None  # Legacy fake manager has no dispatch receipt.
    manager._capture_submitted_tasks = capture
    manager._is_submitted_task = lambda task: (
        manager.tasks.get(task.task_id) is task and task.status is TaskStatus.SUBMITTED
    )
    return TaskScheduler(ControllerConfig(), manager, Mock(), Mock(), Mock(), Mock())


async def owned_worker(owner, *, suppress_cancel=False):
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    count = []

    async def run():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            count.append("cancel")
            cancelled.set()
            if suppress_cancel:
                await release.wait()
            else:
                raise

    task = asyncio.create_task(run())
    owner._running_tasks["owned"] = (None, task)
    owner._owned_execution_tasks["owned"] = task
    task.add_done_callback(lambda done: owner._forget_owned_execution("owned", done))
    await started.wait()
    return task, cancelled, release, count


@pytest.mark.asyncio
async def test_stop_joins_owned_task_and_does_not_touch_unrelated_task():
    owner = scheduler()
    task, _, _, _ = await owned_worker(owner)
    unrelated = asyncio.create_task(asyncio.Event().wait())
    try:
        await owner.stop()
        assert task.done() and task.cancelled()
        assert not owner._stopping_tasks
        assert not owner._running_tasks
        assert not unrelated.done() and unrelated.cancelling() == 0
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_retains_owner_then_retry_finishes_and_start_is_blocked(monkeypatch):
    owner = scheduler()
    task, cancelled, release, count = await owned_worker(owner, suppress_cancel=True)
    monkeypatch.setattr(module, "_STOP_TIMEOUT_SECONDS", 0.02)
    try:
        with pytest.raises(BaseError, match="unconfirmed"):
            await owner.stop()
        assert cancelled.is_set() and not task.done()
        assert task in owner._stopping_tasks
        with pytest.raises(BaseError, match="retry stop before start"):
            await owner.start()
        with pytest.raises(BaseError, match="unconfirmed"):
            await owner.stop()
        assert count == ["cancel"] and task.cancelling() == 1
        release.set()
        await task
        await owner.stop()
        await owner.start()
        await owner.stop()
        assert not owner._stopping_tasks
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_caller_cancel_preserves_owned_handles_for_another_stop():
    owner = scheduler()
    task, cancelled, release, count = await owned_worker(owner, suppress_cancel=True)
    caller = asyncio.create_task(owner.stop())
    await cancelled.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    try:
        assert task in owner._stopping_tasks and not task.done()
        retry = asyncio.create_task(owner.stop())
        await asyncio.sleep(0)
        assert count == ["cancel"] and task.cancelling() == 1
        release.set()
        await retry
        assert task.done() and not owner._stopping_tasks
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_concurrent_stops_do_not_cancel_cleanup_twice():
    owner = scheduler()
    task, cancelled, release, count = await owned_worker(owner, suppress_cancel=True)
    first = asyncio.create_task(owner.stop())
    await cancelled.wait()
    second = asyncio.create_task(owner.stop())
    await asyncio.sleep(0)
    try:
        assert not second.done() and count == ["cancel"] and task.cancelling() == 1
        release.set()
        await asyncio.gather(first, second)
        assert not owner._stopping_tasks
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_cancelled_scheduler_waiter_does_not_reinject_task_cancellation():
    owner = scheduler()
    task, cancelled, release, _ = await owned_worker(owner, suppress_cancel=True)
    waiter = asyncio.create_task(owner._wait_all_tasks_complete())
    owner._scheduler_task = waiter
    await asyncio.sleep(0)
    try:
        stop = asyncio.create_task(owner.stop())
        await cancelled.wait()
        await asyncio.sleep(0)
        assert task.cancelling() == 1
        release.set()
        await stop
        assert waiter.cancelled() and task.done()
    finally:
        release.set()
        await task
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_task_error_during_stop_is_not_success():
    owner = scheduler()
    started = asyncio.Event()

    async def broken_cleanup():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("synthetic cleanup failed") from None

    task = asyncio.create_task(broken_cleanup())
    owner._running_tasks["owned"] = (None, task)
    await started.wait()
    with pytest.raises(BaseError, match="owned task failed"):
        await owner.stop()
    assert task.done() and not owner._stopping_tasks and not owner._running_tasks
    await owner.stop()
    await owner.start()
    await owner.stop()


@pytest.mark.asyncio
async def test_actual_schedule_rechecks_stop_after_task_query_await():
    owner = scheduler()
    entered, release = asyncio.Event(), asyncio.Event()
    item = SimpleNamespace(task_id="late", task_type="test", session_id="s")

    async def query(**_):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        return [item]

    owner._task_manager.get_task.side_effect = query
    owner._sessions["s"] = Mock()
    owner._execute_task_wrapper = AsyncMock()
    await owner.start()
    await entered.wait()
    await owner.stop()
    owner._execute_task_wrapper.assert_not_called()
    assert not owner._owned_execution_tasks


@pytest.mark.asyncio
async def test_stop_waits_for_terminal_publish_after_running_entry_was_removed(monkeypatch):
    owner = scheduler()
    published, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    item = SimpleNamespace(task_id="tail", task_type="test", session_id="s", status=TaskStatus.SUBMITTED)

    async def query(*, task_filter):
        return [item] if task_filter.task_id or item.status is TaskStatus.SUBMITTED else []

    async def update(_task_id, status, **_):
        item.status = status

    class Executor:
        async def execute_ability(self, *_):
            yield SimpleNamespace(payload=SimpleNamespace(type=EventType.TASK_COMPLETION))

    async def publish(*_):
        published.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    owner._task_manager.get_task.side_effect = query
    owner._task_manager.update_task_status.side_effect = update
    owner._sessions["s"] = SimpleNamespace(write_stream=AsyncMock(), get_session_id=lambda: "s")
    owner._task_executor_registry.add_task_executor("test", lambda _: Executor())
    owner._publish_task_event = publish
    owner._ensure_session_completion_signal = AsyncMock()
    monkeypatch.setattr(module, "_STOP_TIMEOUT_SECONDS", 0.02)
    await owner.start()
    await published.wait()
    try:
        assert not owner._running_tasks
        (task,) = owner._owned_execution_tasks.values()
        with pytest.raises(BaseError, match="unconfirmed"):
            await owner.stop()
        assert cancelled.is_set() and task in owner._stopping_tasks and not task.done()
        release.set()
        await task
        await owner.stop()
        assert not owner._owned_execution_tasks and not owner._stopping_tasks
    finally:
        release.set()
        await owner.stop()


@pytest.mark.asyncio
async def test_owned_self_stop_does_not_poison_later_external_cancellation():
    owner = scheduler()
    attempted = asyncio.Event()

    async def work():
        with pytest.raises(BaseError, match="cannot confirm its own"):
            await owner.stop()
        assert not owner._stopping_tasks
        attempted.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(work())
    owner._owned_execution_tasks["self-stop"] = task
    owner._running_tasks["self-stop"] = (None, task)
    await attempted.wait()
    await owner.stop()
    assert task.cancelled() and not owner._stopping_tasks
    await owner.start()
    await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_exact_owned_capture_survives_terminal_tail_and_id_reuse(cancelled):
    owner = scheduler()
    tail_entered, release_tail = asyncio.Event(), asyncio.Event()
    reused_started, release_reused, reuse_scan = asyncio.Event(), asyncio.Event(), asyncio.Event()
    item = SimpleNamespace(task_id="reused", task_type="test", session_id="s", status=TaskStatus.SUBMITTED)
    executions = []

    async def query(**_):
        if item.status is TaskStatus.SUBMITTED:
            if tail_entered.is_set():
                reuse_scan.set()
            return [item]
        return []

    async def execute(task_id, _session):
        executions.append(task_id)
        item.status = TaskStatus.CANCELED if cancelled else TaskStatus.COMPLETED
        if len(executions) == 1:
            if cancelled:
                raise asyncio.CancelledError
        else:
            reused_started.set()
            await release_reused.wait()

    async def completion(_session_id):
        if len(executions) == 1:
            tail_entered.set()
            await release_tail.wait()

    owner._task_manager.get_task.side_effect = query
    owner._sessions["s"] = SimpleNamespace(get_session_id=lambda: "s")
    owner.execute_task = execute
    owner._ensure_session_completion_signal = completion
    await owner.start()
    try:
        await asyncio.wait_for(tail_entered.wait(), 2)
        assert not owner._running_tasks
        original = owner._capture_owned_execution("reused")
        assert original is not None and not original.done()
        assert owner._capture_owned_execution("reused", expected_task=original) is original
        item.status = TaskStatus.SUBMITTED
        owner._submit_event.set()
        await asyncio.wait_for(reuse_scan.wait(), 2)
        # The query and guarded creation have no intervening await with a free lock.
        assert executions == ["reused"]
        assert owner._capture_owned_execution("reused") is original
        release_tail.set()
        await original
        assert owner._capture_owned_execution("reused", expected_task=original) is None
        owner._submit_event.set()
        await asyncio.wait_for(reused_started.wait(), 2)
        replacement = owner._capture_owned_execution("reused")
        assert replacement is not None and replacement is not original
        with pytest.raises(BaseError, match="identity changed"):
            owner._capture_owned_execution("reused", expected_task=original)
        owner._forget_owned_execution("reused", original)
        assert owner._capture_owned_execution("reused") is replacement
        release_reused.set()
        await replacement
        assert owner._capture_owned_execution("reused") is None
        assert not owner._owned_execution_tasks
    finally:
        release_tail.set()
        release_reused.set()
        await owner.stop()


@pytest.mark.asyncio
async def test_owned_capture_rejects_ambiguous_or_missing_live_owner():
    owner = scheduler()
    original = asyncio.create_task(asyncio.Event().wait())
    unrelated = asyncio.create_task(asyncio.Event().wait())
    try:
        owner._owned_execution_tasks["exact"] = original
        owner._running_tasks["exact"] = (None, unrelated)
        with pytest.raises(BaseError, match="identity changed"):
            owner._capture_owned_execution("exact")
        owner._running_tasks.clear()
        with pytest.raises(BaseError, match="identity changed"):
            owner._capture_owned_execution("exact", expected_task=unrelated)
        with pytest.raises(BaseError, match="identity changed"):
            owner._capture_owned_execution("absent", expected_task=original)
        assert owner._capture_owned_execution("absent") is None
        owner._forget_owned_execution("exact", original)
        assert owner._capture_owned_execution("exact") is original
        assert not unrelated.done() and unrelated.cancelling() == 0
    finally:
        original.cancel()
        unrelated.cancel()
        await asyncio.gather(original, unrelated, return_exceptions=True)
        owner._forget_owned_execution("exact", original)
    assert not owner._owned_execution_tasks
    assert owner._capture_owned_execution("exact", expected_task=original) is None


@pytest.mark.parametrize("task_id", ["", None, 0, []])
def test_owned_capture_requires_task_id(task_id):
    with pytest.raises(BaseError, match="non-empty task id"):
        scheduler()._capture_owned_execution(task_id)
