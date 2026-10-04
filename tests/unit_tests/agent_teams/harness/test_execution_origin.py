"""Live provenance across existing Native queues and the real task kernel."""
import asyncio
import copy
import pickle

import pytest
import pytest_asyncio

from openjiuwen.agent_teams.harness import HarnessState, NativeHarness
from openjiuwen.agent_teams.schema.events import EventMessage, MemberShutdownEvent
from openjiuwen.core.controller.schema import (
    ExecutionOrigin,
    InputEvent,
    Task,
    current_execution_origin,
    execution_origin_scope,
)
from openjiuwen.core.controller.schema.execution_origin import shared_execution_origin
from openjiuwen.core.runner import Runner
from openjiuwen.harness.task_loop.loop_queues import LoopQueues
from tests.unit_tests.agent_teams.harness.fixtures import (
    drain_outputs,
    make_spec,
    start_harness,
    wait_for_state,
    wait_invoke_running,
)


def test_origin_identity_is_live_only():
    origin = ExecutionOrigin({'synthetic-secret': 'never-serialize'})
    assert copy.copy(origin) is origin and copy.deepcopy(origin) is origin
    assert 'secret' not in repr(origin)
    with pytest.raises(TypeError, match='cannot be serialized'):
        pickle.dumps(origin)
    event = InputEvent.from_user_input('hello').with_execution_origin(origin)
    task = Task(session_id='session', task_id='task', task_type='test', inputs=[event])
    assert task.model_copy(deep=True).inputs[0].execution_origin is origin
    assert 'never-serialize' not in task.model_dump_json()
    with execution_origin_scope(ExecutionOrigin(object())):
        restored = Task.model_validate_json(task.model_dump_json())
    assert restored.inputs[0].execution_origin is None
    forged = InputEvent.model_validate({'input_data': [], '_execution_origin': origin})
    assert forged.execution_origin is None


@pytest.mark.asyncio
async def test_scope_expiry_masks_inherited_tasks_but_not_queued_origin():
    origin = ExecutionOrigin(object())
    ready = asyncio.Event()
    async def inherited():
        await ready.wait()
        return current_execution_origin()
    with execution_origin_scope(origin):
        queued = InputEvent.from_user_input('queued').with_execution_origin(origin)
        child = asyncio.create_task(inherited())
        with execution_origin_scope(None):
            assert current_execution_origin() is None
        assert current_execution_origin() is origin
    ready.set()
    assert await child is None
    assert queued.execution_origin is origin
    with execution_origin_scope(queued.execution_origin):
        assert current_execution_origin() is origin
    assert current_execution_origin() is None


@pytest.mark.parametrize('kind', ['steer', 'follow_up'])
def test_queue_validates_identity_before_returning_text(kind):
    queue = LoopQueues()
    origin = ExecutionOrigin(object())
    push = getattr(queue, 'push_' + kind)
    drain = queue.drain_steering if kind == 'steer' else queue.drain_follow_up
    with execution_origin_scope(origin):
        push('one')
        push('two')
    assert drain(expected_origin=origin) == ['one', 'two']
    with execution_origin_scope(origin):
        push('old')
    with execution_origin_scope(ExecutionOrigin(object())):
        with pytest.raises(ValueError, match='mixed'):
            drain()
    push('legacy', origin=None)
    assert drain(expected_origin=None) == ['legacy']


def test_mixed_task_input_sources_do_not_choose_first():
    origin = ExecutionOrigin(object())
    owned = InputEvent.from_user_input('first').with_execution_origin(origin)
    missing = InputEvent.from_user_input('unowned')
    with pytest.raises(ValueError, match='mixed'):
        shared_execution_origin([owned, missing])


@pytest.mark.asyncio
async def test_inprocess_late_delivery_and_json_restore_never_use_publisher_scope():
    from openjiuwen.agent_teams.messager.inprocess import _Bus
    seen = []
    async def receive(message):
        seen.append(current_execution_origin())
    bus = _Bus()
    bus.subscribe('receiver', 'topic', receive)
    bus.register_p2p('receiver', receive)
    origin = ExecutionOrigin(object())
    with execution_origin_scope(origin):
        message = EventMessage.from_event(MemberShutdownEvent(team_name='team', member_name='worker', force=False))
    restored = EventMessage.model_validate_json(message.model_dump_json())
    with execution_origin_scope(ExecutionOrigin(object())):
        await bus.publish('topic', message)
        await bus.send('receiver', restored)
    assert seen == [origin, None]


@pytest.mark.asyncio
async def test_coordination_queue_masks_supervisor_scope_and_keeps_event_source():
    from openjiuwen.agent_teams.agent.coordination.event_bus import EventBus
    from openjiuwen.agent_teams.schema.team import TeamRole
    received = []
    done = asyncio.Event()
    async def receive(event):
        received.append(current_execution_origin())
        if len(received) == 2:
            done.set()
    bus = EventBus(role=TeamRole.HUMAN_AGENT)
    original = ExecutionOrigin(object())
    event = EventMessage(event_type='test', payload={}).with_execution_origin(original)
    with execution_origin_scope(ExecutionOrigin(object())):
        await bus.start(wake_callback=receive)
    try:
        await bus.enqueue(event)
        await bus.enqueue(EventMessage.model_validate_json(event.model_dump_json()))
        await asyncio.wait_for(done.wait(), 2)
    finally:
        await bus.stop()
    assert received == [original, None]


@pytest_asyncio.fixture
async def native():
    await Runner.start()
    harness = NativeHarness(make_spec())
    fake = await start_harness(harness, sleep_seconds=0.02)
    seen = []
    invoke = fake.invoke
    async def observe(inputs, session, **kwargs):
        origin = current_execution_origin()
        seen.append((origin, session, harness.owns_execution(harness, session, origin=origin),
                     harness.owns_execution(fake, session, origin=origin)))
        return await invoke(inputs, session, **kwargs)
    fake.invoke = observe
    output = []
    consumer = asyncio.create_task(drain_outputs(harness, output))
    try:
        yield harness, fake, seen
    finally:
        await harness.stop()
        await consumer
        await Runner.stop()


@pytest.mark.asyncio
async def test_sequential_requests_cross_supervisor_and_task_scheduler(native):
    harness, fake, seen = native
    first, second = ExecutionOrigin(object()), ExecutionOrigin(object())
    for origin in (first, second):
        with execution_origin_scope(origin):
            await harness.send('hello')
        assert await wait_for_state(harness, HarnessState.IDLE)
    assert [item[0] for item in seen] == [first, second]
    assert all(item[2:] == (True, True) for item in seen)
    assert current_execution_origin() is None
    assert not harness.owns_execution(harness, seen[0][1], origin=first)
    assert not harness.owns_execution(harness, seen[0][1], origin=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('immediate', [False, True])
async def test_mixed_origin_send_rejected_before_queue_or_steer(native, immediate):
    harness, fake, seen = native
    fake.sleep_seconds = 0.2
    original = ExecutionOrigin(object())
    await harness.send('first', origin=original)
    await wait_invoke_running(fake)
    before = harness._st.seq_counter
    for different in (None, ExecutionOrigin(object())):
        with pytest.raises(ValueError, match='mixed'):
            await harness.send('must-not-run', immediate=immediate, origin=different)
    assert harness._st.seq_counter == before
    assert await wait_for_state(harness, HarnessState.IDLE)
    assert [item[0] for item in seen] == [original]
    assert [item['query'] for item in fake.invocations] == ['first']


@pytest.mark.asyncio
async def test_same_origin_followups_batch_without_persisting_origin(native):
    harness, fake, seen = native
    fake.sleep_seconds = 0.1
    origin = ExecutionOrigin(object())
    await harness.send('first', origin=origin)
    await wait_invoke_running(fake)
    await harness.send('second', origin=origin)
    await harness.send('third', origin=origin)
    assert await wait_for_state(harness, HarnessState.IDLE)
    assert [item[0] for item in seen] == [origin, origin]
    assert harness.load_state(seen[0][1]).pending_follow_ups == []


@pytest.mark.asyncio
async def test_pause_resume_keeps_original_source_and_rejects_replacement(native):
    harness, fake, seen = native
    fake.sleep_seconds = 5
    original = ExecutionOrigin(object())
    await harness.send('first', origin=original)
    await wait_invoke_running(fake)
    await harness.pause()
    assert harness.state is HarnessState.PAUSED
    with pytest.raises(ValueError, match='mixed'):
        await harness.send('different', origin=ExecutionOrigin(object()))
    fake.sleep_seconds = 0
    with execution_origin_scope(ExecutionOrigin(object())):
        await harness.resume()
    assert await wait_for_state(harness, HarnessState.IDLE)
    assert [item[0] for item in seen] == [original, original]


@pytest.mark.asyncio
async def test_cold_resume_has_no_source_even_inside_new_scope(native):
    harness, fake, seen = native
    with execution_origin_scope(ExecutionOrigin(object())):
        await harness.resume(query='restored')
    assert await wait_for_state(harness, HarnessState.IDLE)
    assert [item[0] for item in seen] == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize('protected', [False, True])
async def test_child_entry_does_not_borrow_member_origin(protected):
    from openjiuwen.harness.tools.subagent.task_tool import _run_subagent_with_observable_stream
    class Child:
        async def invoke(self, inputs):
            assert current_execution_origin() is None
            if protected:
                raise PermissionError('independent child source required')
            return {'output': 'legacy'}
    with execution_origin_scope(ExecutionOrigin(object())):
        if protected:
            with pytest.raises(PermissionError, match='independent child'):
                await _run_subagent_with_observable_stream(Child(), {})
        else:
            assert await _run_subagent_with_observable_stream(Child(), {}) == {'output': 'legacy'}


@pytest.mark.asyncio
@pytest.mark.parametrize('protected', [False, True])
async def test_product_child_execution_masks_parent_and_finalizes(monkeypatch, protected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openjiuwen.harness.subagent_runtime import native_execution as module
    from openjiuwen.harness.subagent_runtime.ports import SubagentTurnRequest
    finalized = []
    class Child:
        async def stream(self, inputs, session):
            assert current_execution_origin() is None
            if protected:
                raise PermissionError('independent child source required')
            yield {'type': 'llm_output', 'payload': {'content': 'legacy'}}
    async def finish(session, succeeded):
        finalized.append((current_execution_origin(), succeeded))
    monkeypatch.setattr(module, 'prepare_subagent_task_resources', AsyncMock())
    monkeypatch.setattr(module, 'cleanup_subagent_task_resources', AsyncMock())
    session = SimpleNamespace(pre_run=AsyncMock(), close_stream=AsyncMock())
    execution = module.NativeSubagentExecution(agent=Child(), session_factory=lambda: session,
        subagent_id='child', parent_session_id='parent', on_turn_finished=finish)
    result = AsyncMock()
    parent = ExecutionOrigin(object())
    with execution_origin_scope(parent):
        if protected:
            with pytest.raises(PermissionError, match='independent child'):
                await execution.run_turn(SubagentTurnRequest('task', 'query'), on_result=result)
            result.assert_not_awaited()
        else:
            await execution.run_turn(SubagentTurnRequest('task', 'query'), on_result=result)
            result.assert_awaited_once()
        assert current_execution_origin() is parent
    assert finalized == [(None, not protected)]
    session.close_stream.assert_awaited_once()


def test_callback_steering_uses_same_existing_queue_and_checks_origin():
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
    queue = LoopQueues()
    ctx = AgentCallbackContext(agent=object(), inputs=None, session=None)
    ctx.bind_steering_queue(queue.steering)
    origin = ExecutionOrigin(object())
    with execution_origin_scope(origin):
        queue.push_steer('first')
        ctx.push_steering('second')
        assert ctx.drain_steering() == ['first', 'second']
        ctx.push_steering('must-not-borrow')
    with pytest.raises(ValueError, match='mixed'):
        ctx.drain_steering()


@pytest.mark.asyncio
async def test_restored_followup_never_adopts_current_source(native):
    harness, fake, seen = native
    state = harness.load_state(harness._session)
    state.pending_follow_ups = ['restored']
    harness.save_state(harness._session, state)
    with pytest.raises(ValueError, match='restored follow-up'):
        harness._drain_pending_follow_ups(harness._session, expected_origin=ExecutionOrigin(object()))
    assert harness._drain_pending_follow_ups(harness._session, expected_origin=None) == ['restored']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['task_completion', 'task_failed', 'task_interaction'])
async def test_scheduler_terminal_event_keeps_original_task_source(kind):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
    from openjiuwen.core.controller.schema import ControllerOutputChunk, ControllerOutputPayload, TextDataFrame

    origin = ExecutionOrigin(object())
    task = Task(session_id='session', task_id='task', task_type='test',
                inputs=[InputEvent.from_user_input('original').with_execution_origin(origin)])
    scheduler = object.__new__(TaskScheduler)
    scheduler._task_manager = SimpleNamespace(get_task=AsyncMock(return_value=[task.model_copy(deep=True)]))
    scheduler._event_queue = SimpleNamespace(publish_event=AsyncMock())
    scheduler._card = SimpleNamespace(id='agent')
    chunk = ControllerOutputChunk(index=0, type='controller_output', last_chunk=True,
        payload=ControllerOutputPayload(type=kind, data=[TextDataFrame(text='done')]))
    with execution_origin_scope(ExecutionOrigin(object())):
        await scheduler._publish_task_event('task', object(), chunk)
    event = scheduler._event_queue.publish_event.await_args.args[2]
    assert event.execution_origin is origin
    assert event.task.inputs[0].execution_origin is origin


@pytest.mark.asyncio
async def test_cancelled_scope_expires_child_context_and_restores_caller():
    parent, source = ExecutionOrigin(object()), ExecutionOrigin(object())
    ready, release = asyncio.Event(), asyncio.Event()
    children = []
    async def inherited():
        await release.wait()
        return current_execution_origin()
    async def run():
        with execution_origin_scope(source):
            children.append(asyncio.create_task(inherited()))
            ready.set()
            await asyncio.Event().wait()
    with execution_origin_scope(parent):
        running = asyncio.create_task(run())
        await ready.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert current_execution_origin() is parent
        release.set()
        assert await children[0] is None
