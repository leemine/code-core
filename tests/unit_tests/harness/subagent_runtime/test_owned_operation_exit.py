"""Original operation provenance and real Native finalizer ownership."""
import asyncio
import copy
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.controller.schema.execution_origin import (
    ExecutionOrigin, current_execution_origin, execution_origin_scope,
)
from openjiuwen.harness.subagent_runtime.models import UserInputOp
from openjiuwen.harness.subagent_runtime.native_execution import NativeSubagentExecution
from openjiuwen.harness.subagent_runtime.ports import SubagentTurnRequest


def test_operation_live_carrier_is_explicit_and_not_serialized():
    original = ExecutionOrigin(object())
    with execution_origin_scope(original):
        plain = UserInputOp('query', 'task')
        assert plain.execution_origin is None
        owned = plain.with_execution_origin(original)
        assert dataclasses.asdict(owned) == {'query': 'query', 'task_id': 'task'}
        restored = UserInputOp(**json.loads(json.dumps(dataclasses.asdict(owned))))
    assert restored.execution_origin is None
    assert plain.execution_origin is None
    for value in (copy.copy(owned), copy.deepcopy(owned), dataclasses.replace(owned)):
        assert value.execution_origin is original
    assert owned.with_execution_origin(None).execution_origin is None
    assert original.host_value not in dataclasses.asdict(owned).values()


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_repeated_caller_cancel_cannot_drop_actual_native_finalizer(monkeypatch, fails):
    from openjiuwen.harness.subagent_runtime import native_execution as module
    entered, release = asyncio.Event(), asyncio.Event()
    finalized = []
    class Child:
        async def stream(self, inputs, session):
            assert current_execution_origin() is None
            yield {'type': 'llm_output', 'payload': {'content': 'synthetic'}}
    async def finish(session, succeeded):
        assert current_execution_origin() is None
        entered.set()
        await release.wait()
        finalized.append(succeeded)
        if fails:
            raise RuntimeError('synthetic finalizer failure')
    monkeypatch.setattr(module, 'prepare_subagent_task_resources', AsyncMock())
    monkeypatch.setattr(module, 'cleanup_subagent_task_resources', AsyncMock())
    session = SimpleNamespace(pre_run=AsyncMock(), close_stream=AsyncMock())
    execution = NativeSubagentExecution(agent=Child(), session_factory=lambda: session,
        subagent_id='child', parent_session_id='parent', on_turn_finished=finish)
    result = AsyncMock()
    source = ExecutionOrigin(object())
    with execution_origin_scope(source):
        caller = asyncio.create_task(execution.run_turn(SubagentTurnRequest('task', 'query'), on_result=result))
        try:
            await entered.wait()
            result.assert_awaited_once()
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done() and finalized == []
            release.set()
            expected = RuntimeError if fails else asyncio.CancelledError
            with pytest.raises(expected):
                await caller
            assert finalized == [True]
            assert current_execution_origin() is source
            session.close_stream.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['queued', 'claimed', 'running'])
async def test_exact_original_operation_cancel_preserves_foreign_successor(stage):
    from openjiuwen.harness.subagent_runtime.instance import SubagentInstance
    from openjiuwen.harness.subagent_runtime.ports import SubagentTurnResult
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    class Execution:
        async def run_turn(self, request, *, on_chunk, on_result):
            calls.append(request.task_id)
            if request.task_id == 'A':
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                    await release.wait()
            await on_result(SubagentTurnResult(output='ok'))
        async def close(self, reason):
            pass
    semaphore = asyncio.Semaphore(0 if stage == 'claimed' else 1)
    instance = SubagentInstance(subagent_id='child', subagent_type='test', display_name='child', role='test',
        parent_session_id='parent', execution=Execution(), running_semaphore=semaphore)
    root_a, root_b = ExecutionOrigin(object()), ExecutionOrigin(object())
    a = UserInputOp('a', 'A').with_execution_origin(root_a)
    b = UserInputOp('b', 'B').with_execution_origin(root_b)
    await instance.enqueue(a)
    await instance.enqueue(b)
    if stage != 'queued':
        await instance.start_worker()
        while instance._claimed_op is not a:
            await asyncio.sleep(0)
    if stage == 'running':
        await entered.wait()
    original = instance._capture_origin_ops(root_a)
    assert original == (a,)
    stopping = asyncio.create_task(instance._finish_original_ops(original, cancel=True))
    try:
        if stage == 'running':
            await cancelled.wait()
            assert not stopping.done() and instance._claimed_op is a
            assert calls == ['A'] and not b._lifetime.cancel_requested
            release.set()
        await asyncio.wait_for(stopping, 1)
        assert a._lifetime.done.is_set() and not b._lifetime.cancel_requested
        assert not instance._closed
        if stage == 'queued':
            await instance.start_worker()
        if stage == 'claimed':
            semaphore.release()
        await asyncio.wait_for(instance._ops.join(), 1)
        assert calls == (['A', 'B'] if stage == 'running' else ['B'])
        assert b._lifetime.done.is_set() and not instance._worker_task.done()
    finally:
        release.set()
        if stage == 'claimed' and semaphore.locked():
            semaphore.release()
        await instance.start_worker()
        await instance.shutdown('test')


def test_nested_lifecycle_survives_parent_logical_terminal_without_parent_authority():
    from openjiuwen.harness.subagent_runtime.operation_origin import _capture_operation_lifetime, _operation_scope
    live = [True]
    def check_parent():
        if not live[0]:
            raise RuntimeError('logical parent ended')
    source = ExecutionOrigin(object(), _checker=check_parent)
    with execution_origin_scope(source):
        parent = _capture_operation_lifetime()
    live[0] = False
    with _operation_scope(parent), execution_origin_scope(None):
        child = _capture_operation_lifetime()
        assert child.origin is source and child.parent is parent
        assert current_execution_origin() is None
        parent.cancel_requested = True
        with pytest.raises(RuntimeError, match='no longer accepts'):
            _capture_operation_lifetime()


@pytest.mark.asyncio
async def test_inherited_expired_operation_scope_never_downgrades_to_legacy():
    from openjiuwen.harness.subagent_runtime.operation_origin import _capture_operation_lifetime, _operation_scope
    release = asyncio.Event()
    async def later():
        await release.wait()
        return _capture_operation_lifetime()
    source = ExecutionOrigin(object())
    with execution_origin_scope(source):
        original = _capture_operation_lifetime()
    with _operation_scope(original):
        task = asyncio.create_task(later())
    release.set()
    with pytest.raises(RuntimeError, match='scope expired'):
        await task


@pytest.mark.asyncio
async def test_expired_parent_execution_scope_does_not_create_legacy_operation():
    from openjiuwen.harness.subagent_runtime.operation_origin import _capture_operation_lifetime
    release = asyncio.Event()
    async def later():
        await release.wait()
        return _capture_operation_lifetime()
    with execution_origin_scope(ExecutionOrigin(object())):
        task = asyncio.create_task(later())
    release.set()
    with pytest.raises(RuntimeError, match='origin scope expired'):
        await task
    with execution_origin_scope(None):
        assert _capture_operation_lifetime() is None


@pytest.mark.asyncio
async def test_activity_actual_write_tail_keeps_shared_emitter_and_foreign_items():
    from openjiuwen.harness.subagent_runtime.activity_events import ActivityEmitter
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from openjiuwen.harness.subagent_runtime.models import SubagentActivity
    a = UserInputOp('a', 'a').with_execution_origin(ExecutionOrigin(object()))
    b = UserInputOp('b', 'b').with_execution_origin(ExecutionOrigin(object()))
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []
    async def write(chunk):
        value = chunk.payload['subagent_activity']
        writes.append(value['task_id'])
        if value['task_id'] == 'a-current':
            entered.set()
            await release.wait()
    emitter = ActivityEmitter(SimpleNamespace(write_stream=write), config=SubagentRuntimeConfig())
    def activity(task, op):
        return SubagentActivity('child', task, 1, 'thinking', 'synthetic')._with_operation(op._lifetime)
    emitter.start()
    emitter.offer(activity('a-current', a))
    await entered.wait()
    emitter.offer(activity('a-queued', a))
    emitter.offer(activity('b-queued', b))
    original = emitter._capture_origin_items(a.execution_origin)
    drain = emitter._drain_task
    waiter = asyncio.create_task(emitter._finish_original_items(original, cancel=True))
    try:
        await asyncio.sleep(0)
        assert not waiter.done() and writes == ['a-current']
        assert not drain.done() and not drain.cancelling()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not drain.done() and not original[-1].done.is_set()
        release.set()
        await asyncio.wait_for(emitter._finish_original_items(original, cancel=True), 1)
        await asyncio.wait_for(emitter._queue.join(), 1)
        assert writes == ['a-current', 'b-queued']
        assert emitter._capture_origin_items(a.execution_origin) == ()
        assert emitter._drain_task is drain and not drain.done()
        assert dataclasses.asdict(activity('wire', a)) == dataclasses.asdict(SubagentActivity('child', 'wire', 1, 'thinking', 'synthetic'))
        assert '_operation' not in activity('wire', a).to_dict()
    finally:
        release.set()
        await emitter.close()


@pytest.mark.asyncio
async def test_control_background_operation_survives_parent_logical_end_and_exact_exit():
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Execution:
        async def run_turn(self, request, *, on_chunk, on_result):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await release.wait()
        async def close(self, reason):
            pass
    class Factory:
        async def create(self, request, context):
            return Execution()
    parent = SimpleNamespace(deep_config=None)
    control = SubagentControl(parent, 'parent', config=SubagentRuntimeConfig(enable_activity_stream=False), execution_factory=Factory())
    logical_live = [True]
    def parent_check():
        if not logical_live[0]:
            raise RuntimeError('logical turn ended')
    source = ExecutionOrigin(object(), _checker=parent_check)
    with execution_origin_scope(source):
        result = await control.spawn('test', 'background')
    await entered.wait()
    logical_live[0] = False
    handle = control._capture_origin_exit(source)
    operation = handle.operations[0][2][0]
    assert not operation._lifetime.done.is_set()
    stopping = asyncio.create_task(control._finish_origin_exit(handle, cancel=True))
    try:
        await cancelled.wait()
        assert not stopping.done()
        run = operation._lifetime.run_task
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        stopping = asyncio.create_task(control._finish_origin_exit(handle, cancel=True))
        await asyncio.sleep(0)
        assert run.cancelling() == 1 and not run.done()
        release.set()
        await asyncio.wait_for(stopping, 1)
        assert operation._lifetime.done.is_set()
        assert control._manager.find(result.subagent_id) is handle.operations[0][1]
        assert not handle.operations[0][1]._worker_task.done()
    finally:
        release.set()
        await control.cancel_all('test cleanup')


@pytest.mark.asyncio
async def test_original_cancel_during_running_status_await_cannot_launch_execution():
    from openjiuwen.harness.subagent_runtime.instance import SubagentInstance
    from openjiuwen.harness.subagent_runtime.models import SubagentStatusKind
    from openjiuwen.harness.subagent_runtime.ports import SubagentTurnResult
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def status(value):
        if value.kind is SubagentStatusKind.RUNNING and not entered.is_set():
            entered.set()
            await release.wait()
    class Execution:
        async def run_turn(self, request, *, on_chunk, on_result):
            calls.append(request.task_id)
            await on_result(SubagentTurnResult(output='ok'))
        async def close(self, reason):
            pass
    instance = SubagentInstance(subagent_id='child', subagent_type='test', display_name='child', role='test',
        parent_session_id='parent', execution=Execution(), running_semaphore=asyncio.Semaphore(1),
        on_status_changed=status)
    a = UserInputOp('a', 'a').with_execution_origin(ExecutionOrigin(object()))
    b = UserInputOp('b', 'b').with_execution_origin(ExecutionOrigin(object()))
    await instance.enqueue(a)
    await instance.enqueue(b)
    await instance.start_worker()
    await entered.wait()
    original = instance._capture_origin_ops(a.execution_origin)
    stopping = asyncio.create_task(instance._finish_original_ops(original, cancel=True))
    try:
        await asyncio.sleep(0)
        assert not stopping.done() and a._lifetime.run_task is None
        release.set()
        await asyncio.wait_for(stopping, 1)
        await asyncio.wait_for(instance._ops.join(), 1)
        assert calls == ['b']
    finally:
        release.set()
        await instance.shutdown('test')


@pytest.mark.asyncio
async def test_spawn_source_invalidated_during_factory_await_cleans_original_admission(tmp_path, monkeypatch):
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    monkeypatch.chdir(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    execution = SimpleNamespace(run_turn=AsyncMock(), close=AsyncMock())
    class Factory:
        async def create(self, request, context):
            entered.set()
            await release.wait()
            return execution
    control = SubagentControl(SimpleNamespace(deep_config=None), 'parent',
        config=SubagentRuntimeConfig(enable_activity_stream=False), execution_factory=Factory())
    live = [True]
    def check():
        if not live[0]:
            raise RuntimeError('source revoked')
    with execution_origin_scope(ExecutionOrigin(object(), _checker=check)):
        task = asyncio.create_task(control.spawn('test', 'query', subagent_id='child'))
        await entered.wait()
        live[0] = False
        release.set()
        with pytest.raises(RuntimeError, match='source revoked'):
            await task
    assert control._registry.count == 0
    assert control._manager.find('child') is None
    execution.run_turn.assert_not_awaited()
    execution.close.assert_awaited_once_with('spawn_failed')


@pytest.mark.asyncio
async def test_new_input_gate_preserves_original_managed_activity_and_drops_live_carrier_from_history(tmp_path, monkeypatch):
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from openjiuwen.harness.subagent_runtime.models import SubagentActivity
    from openjiuwen.harness.subagent_runtime.operation_origin import _operation_scope
    monkeypatch.chdir(tmp_path)
    control = SubagentControl(SimpleNamespace(deep_config=None), 'parent',
        config=SubagentRuntimeConfig(enable_activity_stream=False))
    original = UserInputOp('query', 'old').with_execution_origin(ExecutionOrigin(object()))
    original._lifetime.operation = original
    with _operation_scope(original._lifetime):
        control._handle_activity(SubagentActivity('child', 'old', 1, 'tool_result', 'done'))
    assert ('child', 'old') in control._pending_activities
    control._prepare_turn_activity_gate('child', 'new')
    assert ('child', 'old') in control._pending_activities
    control._mark_activity_ready('child', 'old')
    assert control._activities['child'][0]._operation is None
    assert not control._pending_activities


@pytest.mark.asyncio
async def test_completed_operation_late_callback_cannot_mutate_foreign_successor():
    from openjiuwen.harness.subagent_runtime.instance import SubagentInstance
    from openjiuwen.harness.subagent_runtime.ports import SubagentTurnResult
    callbacks = {}
    entered, release = asyncio.Event(), asyncio.Event()
    chunks = []
    async def chunk(value):
        chunks.append(value)
    class Execution:
        async def run_turn(self, request, *, on_chunk, on_result):
            callbacks[request.task_id] = (on_chunk, on_result)
            if request.task_id == 'b':
                entered.set()
                await release.wait()
            await on_result(SubagentTurnResult(output=request.task_id))
        async def close(self, reason):
            pass
    instance = SubagentInstance(subagent_id='child', subagent_type='test', display_name='child', role='test',
        parent_session_id='parent', execution=Execution(), running_semaphore=asyncio.Semaphore(1), on_chunk=chunk)
    a = UserInputOp('a', 'a').with_execution_origin(ExecutionOrigin(object()))
    b = UserInputOp('b', 'b').with_execution_origin(ExecutionOrigin(object()))
    await instance.enqueue(a)
    await instance.enqueue(b)
    await instance.start_worker()
    try:
        await entered.wait()
        for callback, value in zip(callbacks['a'], ('stale', SubagentTurnResult(output='stale'))):
            with pytest.raises(RuntimeError, match='original operation'):
                await callback(value)
        assert chunks == [] and instance.last_output == 'a'
        assert instance._claimed_op is b
        release.set()
        await asyncio.wait_for(instance._ops.join(), 1)
        assert instance.last_output == 'b'
    finally:
        release.set()
        await instance.shutdown('test')


@pytest.mark.asyncio
async def test_background_operation_finishes_normally_after_parent_logical_terminal(tmp_path, monkeypatch):
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from openjiuwen.harness.subagent_runtime.ports import SubagentTurnResult
    monkeypatch.chdir(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    class Execution:
        async def run_turn(self, request, *, on_chunk, on_result):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            await on_result(SubagentTurnResult(output='background result'))
        async def close(self, reason):
            pass
    class Factory:
        async def create(self, request, context):
            return Execution()
    control = SubagentControl(SimpleNamespace(deep_config=None), 'parent',
        config=SubagentRuntimeConfig(enable_activity_stream=False), execution_factory=Factory())
    live = [True]
    def check():
        if not live[0]:
            raise RuntimeError('parent terminal')
    source = ExecutionOrigin(object(), _checker=check)
    with execution_origin_scope(source):
        result = await control.spawn('test', 'background')
    await entered.wait()
    live[0] = False
    handle = control._capture_origin_exit(source)
    waiter = asyncio.create_task(control._finish_origin_exit(handle, cancel=False))
    try:
        await asyncio.sleep(0)
        assert not waiter.done() and cancelled == []
        release.set()
        await asyncio.wait_for(waiter, 1)
        instance = control._manager.find(result.subagent_id)
        assert instance.last_output == 'background result' and cancelled == []
        assert not instance._worker_task.done()
    finally:
        release.set()
        await control.cancel_all('test')


@pytest.mark.asyncio
async def test_actual_native_no_child_terminal_allows_idle_shared_activity_drain(monkeypatch):
    from openjiuwen.harness.subagent_runtime.activity_events import ActivityEmitter
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from openjiuwen.harness_protocol import HarnessInput, TurnEventKind
    from tests.unit_tests.harness_providers.test_native_exact_exit import harness_case, terminals
    case = await harness_case(monkeypatch)
    control = SubagentControl(case.agent, case.case.session.get_session_id())
    emitter = ActivityEmitter(case.case.session, config=SubagentRuntimeConfig())
    control._activity_emitter = emitter
    case.agent._subagent_controls = {case.case.session.get_session_id(): control}
    emitter.start()
    drain = emitter._drain_task
    try:
        receipt = await case.harness.send(HarnessInput(content='A'))
        result = await asyncio.wait_for(terminals(case.harness, receipt.turn_id), 2)
        assert result == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
        assert emitter._current_item is None and emitter._queue.empty()
        assert emitter._drain_task is drain and not drain.done()
    finally:
        await emitter.close()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_real_activity_write_blocks_quiescence_until_done_without_stopping_drain(monkeypatch):
    from openjiuwen.harness.subagent_runtime.activity_events import ActivityEmitter
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from openjiuwen.harness.subagent_runtime.models import SubagentActivity
    from openjiuwen.harness.deep_agent import _OwnedRoundExitUnconfirmed
    from tests.unit_tests.harness_providers.test_native_exact_exit import harness_case
    case = await harness_case(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def write(_chunk):
        entered.set()
        await release.wait()
    case.case.session.write_stream = write
    control = SubagentControl(case.agent, case.case.session.get_session_id())
    emitter = ActivityEmitter(case.case.session, config=SubagentRuntimeConfig())
    control._activity_emitter = emitter
    case.agent._subagent_controls = {case.case.session.get_session_id(): control}
    emitter.start()
    emitter.offer(SubagentActivity('child', 'old', 1, 'thinking', 'text'))
    try:
        await entered.wait()
        assert emitter._queue.empty() and emitter._current_item is not None
        with pytest.raises(_OwnedRoundExitUnconfirmed, match='activity source/exit'):
            case.agent._check_unattributed_subagent_exit(case.case.session)
        release.set()
        await asyncio.wait_for(emitter._queue.join(), 1)
        case.agent._check_unattributed_subagent_exit(case.case.session)
        assert not emitter._drain_task.done()
    finally:
        release.set()
        await emitter.close()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_unproven_legacy_spawn_completion_is_not_a_child_exit_receipt(monkeypatch):
    from openjiuwen.harness.deep_agent import _OwnedRoundExitUnconfirmed
    from openjiuwen.harness.tools.subagent.session_tools import SessionToolkit
    from tests.unit_tests.harness_providers.test_native_exact_exit import harness_case
    case = await harness_case(monkeypatch)
    toolkit = SessionToolkit()
    case.agent._session_toolkit = toolkit
    toolkit.upsert_running('legacy', 'child', 'background')
    try:
        for state in ('running', 'completed', 'canceled'):
            if state == 'completed':
                toolkit.mark_completed('legacy', 'output')
            if state == 'canceled':
                toolkit.mark_canceled('legacy')
            with pytest.raises(_OwnedRoundExitUnconfirmed, match='legacy session_spawn source'):
                case.agent._check_unattributed_subagent_exit(case.case.session)
    finally:
        await case.harness.stop()


@pytest.mark.asyncio
async def test_unproven_live_spawn_wrapper_rejected_without_allocating_control(monkeypatch):
    from openjiuwen.core.controller.schema.task import Task, TaskStatus
    from openjiuwen.harness.deep_agent import _OwnedRoundExitUnconfirmed
    from openjiuwen.harness.tools import SESSION_SPAWN_TASK_TYPE
    from tests.unit_tests.harness_providers.test_native_exact_exit import harness_case
    case = await harness_case(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def tail():
        entered.set()
        await release.wait()
    task = Task(task_id='spawn-tail', session_id=case.case.session.get_session_id(),
        task_type=SESSION_SPAWN_TASK_TYPE, status=TaskStatus.SUBMITTED)
    await case.case.scheduler.stop()
    await case.case.manager.add_task(task)
    stored = case.case.manager.tasks[task.task_id]
    capture = case.case.manager._capture_task_execution(stored)
    await case.case.manager.update_task_status(task.task_id, TaskStatus.COMPLETED)
    wrapper = asyncio.create_task(tail())
    wrapper._jiuwen_execution_capture = (capture, case.case.session)
    case.case.scheduler._owned_execution_tasks[task.task_id] = wrapper
    try:
        await entered.wait()
        with pytest.raises(_OwnedRoundExitUnconfirmed, match='wrapper exit'):
            case.agent._check_unattributed_subagent_exit(case.case.session)
        assert not wrapper.cancelling()
        release.set()
        await wrapper
        case.agent._check_unattributed_subagent_exit(case.case.session)
    finally:
        release.set()
        await wrapper
        await case.harness.stop()
