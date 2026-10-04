"""Exact Native lifecycle over actual Core Round/scheduler with synthetic model IO."""
import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import OutputLeaseManager
from openjiuwen.harness_protocol import (
    AbortMode, HarnessContext, HarnessInput, HarnessStateError, TurnEventKind, TurnLifecycleEvent,
)
from openjiuwen.harness_providers.native.harness import DeepAgentHarness
from openjiuwen.harness_providers.native.host import NativeHostHooks
from tests.unit_tests.harness.test_owned_round_pipeline import pipeline


async def harness_case(monkeypatch, capture=None):
    case = await pipeline(monkeypatch)
    agent = case.agent
    agent.event_manager._discard_captured_work((case.work,))
    monkeypatch.setattr(agent, "_notify_work", DeepAgent._notify_work.__get__(agent))
    monkeypatch.setattr(agent._interaction_output, "has_consumer", OutputLeaseManager.has_consumer.__get__(agent._interaction_output))
    origins = {key: ExecutionOrigin(object(), _checker=lambda: None) for key in ("A", "B")}
    harness = DeepAgentHarness(lambda _: agent, observe_tools=False,
        host_hooks=NativeHostHooks(capture_execution_origin=capture or (lambda content: origins[content.content])))
    async def opened(_):
        harness._agent = agent
        harness._agent_session = case.session
        return case.session.get_session_id()
    async def closed():
        supervisor = agent._interaction_supervisor_task
        if supervisor is not None:
            supervisor.cancel()
            await supervisor
        forwarder = agent._interaction_forwarder_task
        if forwarder is not None:
            forwarder.cancel()
            await asyncio.gather(forwarder, return_exceptions=True)
        await case.scheduler.stop()
        harness._agent = None
        harness._agent_session = None
    monkeypatch.setattr(harness, "_open_session", opened)
    monkeypatch.setattr(harness, "_close_session", closed)
    await harness.start(HarnessContext(agent_name="native", agent_id="a", host_session_id="owned-pipeline", system_prompt=""))
    return SimpleNamespace(**locals())


async def terminals(harness, turn_id):
    return [item.event.kind async for item in harness.turn_events(turn_id) if isinstance(item.event, TurnLifecycleEvent)]


@pytest.mark.asyncio
async def test_actual_native_normal_turn_keeps_original_source(monkeypatch):
    case = await harness_case(monkeypatch)
    receipt = await case.harness.send(HarnessInput(content="A"))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    try:
        result = await asyncio.wait_for(terminals(case.harness, receipt.turn_id), 2)
        assert result == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
        assert original._origin.host_value is case.origins["A"].host_value
        assert original._exit.confirmed.done() and original._execution_done.is_set()
        assert all(event.execution_origin is original._origin for event in case.case.events)
    finally:
        await case.harness.stop()


@pytest.mark.asyncio
async def test_original_cancel_waits_real_wrapper_tail_before_successor(monkeypatch):
    from openjiuwen.core.controller.modules import task_scheduler
    monkeypatch.setattr(task_scheduler, "_STOP_TIMEOUT_SECONDS", 0.03)
    case = await harness_case(monkeypatch)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def invoke(inputs, *_args, **_kwargs):
        calls.append(inputs["query"])
        if inputs["query"] == "A":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await release.wait()
        return {"output": "synthetic"}
    monkeypatch.setattr(case.agent.react_agent, "invoke", invoke)
    a = await case.harness.send(HarnessInput(content="A"))
    original = case.harness._capture_owned_turn(a.turn_id)
    a_events = asyncio.create_task(terminals(case.harness, a.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    b = await case.harness.send(HarnessInput(content="B"))
    successor = case.harness._capture_owned_turn(b.turn_id)
    try:
        with pytest.raises(HarnessStateError, match="unconfirmed"):
            await case.harness._abort_owned_turn(original)
        assert cancelled.is_set() and calls == ["A"]
        assert case.harness.active_turn is original and not successor.abort_requested
        assert not a_events.done() and not original._exit.confirmed.done()
        cleanup = original._exit.cleanup
        release.set()
        await asyncio.wait_for(case.harness._abort_owned_turn(original), 2)
        assert original._exit.cleanup is cleanup
        assert await asyncio.wait_for(a_events, 2) == [TurnEventKind.STARTED, TurnEventKind.ABORTED]
        assert await asyncio.wait_for(terminals(case.harness, b.turn_id), 2) == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
        assert calls == ["A", "B"]
    finally:
        release.set()
        await asyncio.gather(a_events, return_exceptions=True)
        await case.harness.stop()


@pytest.mark.asyncio
async def test_configured_source_hook_cannot_downgrade_to_legacy(monkeypatch):
    case = await harness_case(monkeypatch, capture=lambda _: None)
    try:
        with pytest.raises(HarnessStateError, match="original synchronous source"):
            await case.harness.send(HarnessInput(content="A"))
        assert not case.harness._pending and case.harness.active_turn is None
        assert case.agent.react_agent.invoke_calls == []
    finally:
        await case.harness.stop()


@pytest.mark.asyncio
async def test_late_host_dispatch_is_joined_then_denied_before_send(monkeypatch):
    from openjiuwen.core.controller.modules import task_scheduler
    monkeypatch.setattr(task_scheduler, "_STOP_TIMEOUT_SECONDS", 0.03)
    entered, release = asyncio.Event(), asyncio.Event()
    case = await harness_case(monkeypatch)
    async def dispatch(agent, request, _content, _resume):
        entered.set()
        await release.wait()
        await agent.send_input(request)
        return True
    object.__setattr__(case.harness._host_hooks, "dispatch_input", dispatch)
    receipt = await case.harness.send(HarnessInput(content="A"))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    events = asyncio.create_task(terminals(case.harness, receipt.turn_id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(HarnessStateError, match="unconfirmed"):
            await case.harness._abort_owned_turn(original)
        assert not original._exit.confirmed.done() and not events.done()
        assert original._exit.admissions and all(task is not case.harness._supervisor_task for task in original._exit.admissions)
        release.set()
        await asyncio.wait_for(case.harness._abort_owned_turn(original), 2)
        assert await asyncio.wait_for(events, 2) == [TurnEventKind.STARTED, TurnEventKind.ABORTED]
        assert case.agent.react_agent.invoke_calls == []
    finally:
        release.set()
        await asyncio.gather(events, return_exceptions=True)
        await case.harness.stop()


@pytest.mark.asyncio
async def test_cancel_ack_is_not_original_interaction_handle_exit(monkeypatch):
    from openjiuwen.core.controller.modules import task_scheduler
    from openjiuwen.harness_protocol import UserInputRequest, UserInputResponse, InteractionResponseStatus
    monkeypatch.setattr(task_scheduler, "_STOP_TIMEOUT_SECONDS", 0.03)
    case = await harness_case(monkeypatch)
    entered, release, ack = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Interaction:
        async def handle(self, request):
            entered.set()
            await release.wait()
            return UserInputResponse(request_id=request.request_id, status=InteractionResponseStatus.COMPLETED, content="late")
        async def cancel(self, *_args, **_kwargs):
            ack.set()
    # The host's actual immutable Context is installed before admission.
    object.__setattr__(case.harness._context, "interactions", Interaction())
    original_execute = case.harness._execute_turn
    async def execute(turn):
        # Same shared Native supervisor executes this real ledger request.
        await case.harness._request_interaction(UserInputRequest(request_id="ask-original", prompt="synthetic", turn_id=turn.turn_id))
        return await original_execute(turn)
    monkeypatch.setattr(case.harness, "_execute_turn", execute)
    receipt = await case.harness.send(HarnessInput(content="A"))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    events = asyncio.create_task(terminals(case.harness, receipt.turn_id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(HarnessStateError, match="unconfirmed"):
            await case.harness._abort_owned_turn(original)
        entry = original._exit.interactions[0]
        assert ack.is_set() and entry.cancel_task.done() and entry.handling
        assert not entry.handle_done.is_set() and not events.done()
        release.set()
        await asyncio.wait_for(case.harness._abort_owned_turn(original), 2)
        assert entry.handle_done.is_set()
        assert await asyncio.wait_for(events, 2) == [TurnEventKind.STARTED, TurnEventKind.ABORTED]
    finally:
        release.set()
        await asyncio.gather(events, return_exceptions=True)
        await case.harness.stop()


@pytest.mark.asyncio
async def test_queued_exact_cancel_never_interrupts_active_or_new_turn(monkeypatch):
    case = await harness_case(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def invoke(inputs, *_args, **_kwargs):
        calls.append(inputs['query'])
        entered.set()
        await release.wait()
        return {'output': 'ok'}
    monkeypatch.setattr(case.agent.react_agent, 'invoke', invoke)
    a = await case.harness.send(HarnessInput(content='A'))
    await entered.wait()
    b = await case.harness.send(HarnessInput(content='B'))
    queued = case.harness._capture_owned_turn(b.turn_id)
    try:
        await case.harness._abort_owned_turn(queued)
        assert not case.harness.active_turn.abort_requested and calls == ['A']
        release.set()
        assert (await terminals(case.harness, a.turn_id))[-1] is TurnEventKind.FINISHED
        assert (await terminals(case.harness, b.turn_id))[-1] is TurnEventKind.ABORTED
        assert calls == ['A']
        with pytest.raises(HarnessStateError, match='replaced'):
            await case.harness._abort_owned_turn(queued)
    finally:
        release.set()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_cancelled_abort_caller_keeps_original_cleanup_and_tail(monkeypatch):
    case = await harness_case(monkeypatch)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def invoke(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()
    monkeypatch.setattr(case.agent.react_agent, 'invoke', invoke)
    receipt = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    await entered.wait()
    stopping = asyncio.create_task(case.harness.abort(mode=AbortMode.FORCE))
    try:
        await cancelled.wait()
        cleanup = original._exit.cleanup
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert not cleanup.done() and not original._exit.confirmed.done()
        release.set()
        await asyncio.wait_for(case.harness._abort_owned_turn(original), 2)
        assert original._exit.cleanup is cleanup
        assert (await terminals(case.harness, receipt.turn_id))[-1] is TurnEventKind.ABORTED
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)
        await case.harness.stop()


@pytest.mark.asyncio
async def test_normal_eof_waits_original_steer_admission_not_shared_supervisor(monkeypatch):
    case = await harness_case(monkeypatch)
    entered, model_release, steer_entered, steer_release = [asyncio.Event() for _ in range(4)]
    calls = []
    async def invoke(inputs, *_args, **_kwargs):
        calls.append(inputs['query'])
        entered.set()
        await model_release.wait()
        return {'output': 'ok'}
    async def dispatch(agent, request, content, _resume):
        if content.content == 'steer':
            steer_entered.set()
            await steer_release.wait()
        await agent.send_input(request)
        return True
    object.__setattr__(case.harness._host_hooks, 'dispatch_input', dispatch)
    monkeypatch.setattr(case.agent.react_agent, 'invoke', invoke)
    a = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(a.turn_id)
    await entered.wait()
    steering = asyncio.create_task(case.harness._steer(original, HarnessInput(content='steer')))
    await steer_entered.wait()
    b = await case.harness.send(HarnessInput(content='B'))
    a_events = asyncio.create_task(terminals(case.harness, a.turn_id))
    try:
        model_release.set()
        await asyncio.wait_for(original._execution_done.wait(), 2)
        assert not a_events.done() and calls == ['A']
        assert original._exit.admissions and all(t is not case.harness._supervisor_task for t in original._exit.admissions)
        steer_release.set()
        with pytest.raises(HarnessStateError, match='no longer accepts'):
            await steering
        assert (await asyncio.wait_for(a_events, 2))[-1] is TurnEventKind.FINISHED
        assert (await asyncio.wait_for(terminals(case.harness, b.turn_id), 2))[-1] is TurnEventKind.FINISHED
        assert calls == ['A', 'B']
    finally:
        model_release.set()
        steer_release.set()
        await asyncio.gather(steering, a_events, return_exceptions=True)
        await case.harness.stop()


@pytest.mark.asyncio
async def test_session_replacement_cannot_turn_unknown_exit_into_terminal(monkeypatch):
    case = await harness_case(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def invoke(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {'output': 'ok'}
    monkeypatch.setattr(case.agent.react_agent, 'invoke', invoke)
    a = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(a.turn_id)
    events = asyncio.create_task(terminals(case.harness, a.turn_id))
    await entered.wait()
    try:
        case.harness._agent_session = object()
        release.set()
        await asyncio.wait_for(original._execution_done.wait(), 2)
        with pytest.raises(HarnessStateError, match='session changed'):
            await original._exit.cleanup
        assert not original._exit.confirmed.done() and not events.done()
        assert case.harness.active_turn is original
        case.harness._agent_session = case.case.session
        await asyncio.wait_for(case.harness._abort_owned_turn(original), 2)
        assert (await asyncio.wait_for(events, 2))[-1] is TurnEventKind.ABORTED
    finally:
        case.harness._agent_session = case.case.session
        release.set()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_source_fence_checks_host_again_and_self_abort_never_fences(monkeypatch):
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError('revoked root')
    source = ExecutionOrigin(object(), _checker=check)
    case = await harness_case(monkeypatch, capture=lambda _: source)
    entered, release = asyncio.Event(), asyncio.Event()
    async def dispatch(agent, request, _content, _resume):
        original = case.harness.active_turn
        with pytest.raises(HarnessStateError, match='producer cannot'):
            await case.harness._abort_owned_turn(original)
        assert original._exit is None and not original.abort_requested
        entered.set()
        await release.wait()
        await agent.send_input(request)
        return True
    object.__setattr__(case.harness._host_hooks, 'dispatch_input', dispatch)
    a = await case.harness.send(HarnessInput(content='A'))
    await entered.wait()
    try:
        live[0] = False
        release.set()
        assert (await asyncio.wait_for(terminals(case.harness, a.turn_id), 2))[-1] is TurnEventKind.FAILED
        assert case.agent.react_agent.invoke_calls == []
    finally:
        release.set()
        await case.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['queued', 'claimed', 'current'])
async def test_unknown_real_child_owned_work_blocks_exit_without_cancelling_child(monkeypatch, stage):
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.instance import SubagentInstance
    from openjiuwen.harness.subagent_runtime.models import UserInputOp
    from openjiuwen.harness.subagent_runtime.ports import SubagentTurnResult
    from openjiuwen.harness.deep_agent import _OwnedRoundExitUnconfirmed
    case = await harness_case(monkeypatch)
    child_entered, child_release = asyncio.Event(), asyncio.Event()
    class Execution:
        cancelled = False
        async def run_turn(self, request, *, on_chunk, on_result):
            child_entered.set()
            try:
                await child_release.wait()
                await on_result(SubagentTurnResult(output='child'))
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        async def close(self, reason):
            pass
    execution = Execution()
    semaphore = asyncio.Semaphore(0 if stage == 'claimed' else 1)
    child = SubagentInstance(subagent_id='foreign-child', subagent_type='test', display_name='test', role='test',
        parent_session_id=case.case.session.get_session_id(), execution=execution, running_semaphore=semaphore)
    control = SubagentControl(case.agent, case.case.session.get_session_id())
    control._manager._instances[child.subagent_id] = child
    case.agent._subagent_controls = {case.case.session.get_session_id(): control}
    await child.enqueue(UserInputOp(query='foreign', task_id='foreign-task'))
    if stage != 'queued':
        await child.start_worker()
        while not child._turn_claimed:
            await asyncio.sleep(0)
    if stage == 'current':
        await child_entered.wait()
    receipt = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    events = asyncio.create_task(terminals(case.harness, receipt.turn_id))
    try:
        await asyncio.wait_for(original._execution_done.wait(), 2)
        with pytest.raises(_OwnedRoundExitUnconfirmed, match='subagent'):
            await original._exit.cleanup
        assert not events.done() and not original._exit.confirmed.done() and not execution.cancelled
        assert control._manager.find(child.subagent_id) is child
        if stage == 'queued':
            await child.start_worker()
        if stage == 'claimed':
            semaphore.release()
        child_release.set()
        await asyncio.wait_for(child._ops.join(), 2)
        await asyncio.wait_for(case.harness._abort_owned_turn(original), 2)
        assert (await asyncio.wait_for(events, 2))[-1] is TurnEventKind.ABORTED
        assert not execution.cancelled
    finally:
        child_release.set()
        if stage == 'claimed' and semaphore.locked():
            semaphore.release()
        await child.start_worker()
        await child.shutdown('test cleanup')
        await case.harness.stop()


@pytest.mark.asyncio
async def test_exact_queued_source_discard_preserves_foreign_inputs_and_join(monkeypatch):
    from openjiuwen.harness.task_loop.loop_queues import LoopQueues
    case = await harness_case(monkeypatch)
    a, b = case.origins['A'], case.origins['B']
    queues = LoopQueues()
    for source, text in ((a, 'a1'), (b, 'b1'), (a, 'a2'), (b, 'b2')):
        queues.push_follow_up(text, origin=source)
    joining = asyncio.create_task(queues.follow_up.join())
    await asyncio.sleep(0)
    queues._discard_origin(a)
    await asyncio.sleep(0)
    assert not joining.done()
    assert queues.drain_follow_up(expected_origin=b) == ['b1', 'b2']
    queues.follow_up.task_done()
    queues.follow_up.task_done()
    await joining
    await case.harness.stop()


@pytest.mark.asyncio
async def test_original_interaction_cancel_failure_retries_same_ledger_entry(monkeypatch):
    from openjiuwen.harness_protocol import UserInputRequest, UserInputResponse, InteractionResponseStatus
    case = await harness_case(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Interaction:
        async def handle(self, request):
            entered.set()
            await release.wait()
            return UserInputResponse(request_id=request.request_id, status=InteractionResponseStatus.COMPLETED, content='late')
        async def cancel(self, request_id, **_kwargs):
            calls.append(request_id)
            if len(calls) == 1:
                raise RuntimeError('synthetic cancellation failure')
            release.set()
    object.__setattr__(case.harness._context, 'interactions', Interaction())
    original_execute = case.harness._execute_turn
    async def execute(turn):
        await case.harness._request_interaction(UserInputRequest(request_id='retry-original', prompt='synthetic', turn_id=turn.turn_id))
        return await original_execute(turn)
    monkeypatch.setattr(case.harness, '_execute_turn', execute)
    receipt = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    events = asyncio.create_task(terminals(case.harness, receipt.turn_id))
    try:
        await entered.wait()
        with pytest.raises(RuntimeError, match='synthetic cancellation'):
            await case.harness._abort_owned_turn(original)
        handle = original._exit
        entry = handle.interactions[0]
        assert not handle.confirmed.done() and not events.done()
        assert case.harness._pending_interactions['retry-original'] is entry
        await asyncio.wait_for(case.harness._abort_owned_turn(original), 2)
        assert original._exit is handle and calls == ['retry-original', 'retry-original']
        assert entry.handle_done.is_set() and entry.cancel_task.done()
        assert (await events)[-1] is TurnEventKind.ABORTED
    finally:
        release.set()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_exact_work_capture_removes_original_dequeued_and_followups_only(monkeypatch):
    from openjiuwen.harness.schema.interaction import RoundWorkItem
    case = await harness_case(monkeypatch)
    a, b = case.origins['A'], case.origins['B']
    manager = case.agent.event_manager
    first = RoundWorkItem.user(request_id='a1', inputs={'query': 'a1'}).with_execution_origin(a)
    followup = RoundWorkItem.user(request_id='a2', inputs={'query': 'a2'}, is_follow_up=True).with_execution_origin(a)
    foreign = RoundWorkItem.user(request_id='b', inputs={'query': 'b'}).with_execution_origin(b)
    manager.push_user(first)
    manager.push_user(followup)
    manager.push_user(foreign)
    assert manager.next_work() is first
    handle = case.agent._capture_origin_exit(a, expected_session=case.case.session)
    assert handle.work == (followup, first)
    await case.agent._cancel_owned_origin(handle)
    assert manager._dequeued is None and manager.next_work() is foreign
    assert manager.next_work() is None
    # A stale output token cannot detach a new foreign lease.
    first_stream = await case.agent.attach_output()
    await case.agent._detach_owned_output(first_stream._lease.token, expected_origin=a, expected_session=case.case.session)
    foreign_stream = await case.agent.attach_output()
    await case.agent._detach_owned_output(first_stream._lease.token, expected_origin=a, expected_session=case.case.session)
    assert case.agent._interaction_output.current_lease() is foreign_stream._lease
    await foreign_stream.close()
    await case.harness.stop()


@pytest.mark.asyncio
async def test_actual_forwarder_marker_tail_precedes_normal_turn_terminal(monkeypatch):
    from openjiuwen.harness.deep_agent import _ROUND_BOUNDARY
    case = await harness_case(monkeypatch)
    queue = asyncio.Queue()
    entered, release = asyncio.Event(), asyncio.Event()
    async def iterator():
        while True:
            yield await queue.get()
    async def write(chunk):
        await queue.put(chunk)
    async def marker(_session):
        await queue.put(_ROUND_BOUNDARY)
        return True
    emit = case.agent._interaction_output.emit
    async def delayed_emit(chunk, **kwargs):
        if chunk == 'original-tail':
            entered.set()
            await release.wait()
        await emit(chunk, **kwargs)
    async def invoke(*_args, **_kwargs):
        await case.case.session.write_stream('original-tail')
        return {'output': 'ok'}
    case.case.session.stream_iterator = iterator
    case.case.session.write_stream = write
    monkeypatch.setattr(case.agent, '_emit_round_boundary', marker)
    monkeypatch.setattr(case.agent._interaction_output, 'emit', delayed_emit)
    monkeypatch.setattr(case.agent.react_agent, 'invoke', invoke)
    case.agent._interaction_forwarder_task = asyncio.create_task(case.agent._forward_session_stream())
    receipt = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    events = asyncio.create_task(terminals(case.harness, receipt.turn_id))
    try:
        await entered.wait()
        owned = case.agent._capture_owned_round(original._origin)
        # Existing supervisor's former 2s timeout must not turn this green.
        await asyncio.sleep(2.05)
        assert not owned._facade_task.done() and not owned._forwarded.is_set()
        assert not events.done() and not original._execution_done.is_set()
        release.set()
        assert (await asyncio.wait_for(events, 2))[-1] is TurnEventKind.FINISHED
        assert owned._forwarded.is_set() and owned._facade_task.done()
    finally:
        release.set()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_fire_and_forget_original_emit_tail_blocks_terminal(monkeypatch):
    from openjiuwen.harness.schema.interaction import InteractionEvent
    case = await harness_case(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    emit = case.agent._interaction_output.emit
    async def delayed_emit(chunk, **kwargs):
        if getattr(chunk, 'type', None) == 'execution.error':
            entered.set()
            await release.wait()
        await emit(chunk, **kwargs)
    async def invoke(*_args, **_kwargs):
        case.agent._emit_interaction_event(InteractionEvent.execution_error(code='synthetic', message='synthetic'))
        return {'output': 'ok'}
    monkeypatch.setattr(case.agent._interaction_output, 'emit', delayed_emit)
    monkeypatch.setattr(case.agent.react_agent, 'invoke', invoke)
    receipt = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(receipt.turn_id)
    events = asyncio.create_task(terminals(case.harness, receipt.turn_id))
    try:
        await entered.wait()
        await original._execution_done.wait()
        assert not original._exit.confirmed.done() and not events.done()
        owned_emits = tuple(case.agent._interaction_emit_tasks)
        assert len(owned_emits) == 1 and owned_emits[0]._jiuwen_execution_origin is original._origin
        release.set()
        assert (await asyncio.wait_for(events, 2))[-1] is TurnEventKind.FINISHED
        assert all(task.done() for task in owned_emits)
        from openjiuwen.core.controller.schema.execution_origin import execution_origin_scope
        with execution_origin_scope(original._origin):
            with pytest.raises(HarnessStateError, match='no longer accepts'):
                case.agent._emit_interaction_event(InteractionEvent.execution_error(code='late', message='late'))
    finally:
        release.set()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_actual_child_activity_tail_is_not_hidden_by_idle_instance(monkeypatch):
    from openjiuwen.harness.subagent_runtime.activity_events import ActivityEmitter
    from openjiuwen.harness.subagent_runtime.control import SubagentControl
    from openjiuwen.harness.subagent_runtime.config import SubagentRuntimeConfig
    from openjiuwen.harness.subagent_runtime.models import SubagentActivity
    from openjiuwen.harness.deep_agent import _OwnedRoundExitUnconfirmed
    case = await harness_case(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def write(_chunk):
        entered.set()
        await release.wait()
    case.case.session.write_stream = write
    emitter = ActivityEmitter(case.case.session, config=SubagentRuntimeConfig())
    control = SubagentControl(case.agent, case.case.session.get_session_id())
    control._activity_emitter = emitter
    case.agent._subagent_controls = {case.case.session.get_session_id(): control}
    emitter.start()
    emitter.offer(SubagentActivity(subagent_id='original', task_id='task', seq=1, kind='thinking', summary='synthetic'))
    try:
        await entered.wait()
        assert not control._manager.list_ids() and emitter._queue.empty()
        with pytest.raises(_OwnedRoundExitUnconfirmed, match='activity source/exit'):
            case.agent._check_unattributed_subagent_exit(case.case.session)
        assert not emitter._drain_task.done()
        release.set()
        # F32 does not infer idle from an empty queue; the original drain has
        # no source/current-item proof. F33 must preserve that product ability.
        await emitter.close()
        case.agent._check_unattributed_subagent_exit(case.case.session)
    finally:
        release.set()
        await emitter.close()
        await case.harness.stop()


@pytest.mark.asyncio
async def test_stopped_actual_forwarder_keeps_original_round_unconfirmed(monkeypatch):
    from openjiuwen.harness.deep_agent import _OwnedRoundExitUnconfirmed
    case = await pipeline(monkeypatch)
    queue = asyncio.Queue()
    waiting = asyncio.Event()
    async def iterator():
        waiting.set()
        while True:
            yield await queue.get()
    async def marker(_session):
        # Simulate durable stream accepted marker but consumer exits before it.
        return True
    case.session.stream_iterator = iterator
    forwarder = asyncio.create_task(case.agent._forward_session_stream())
    case.agent._interaction_forwarder_task = forwarder
    monkeypatch.setattr(case.agent, '_emit_round_boundary', marker)
    await waiting.wait()
    forwarder.cancel()
    await asyncio.gather(forwarder, return_exceptions=True)
    try:
        with pytest.raises(_OwnedRoundExitUnconfirmed, match='marker was not confirmed'):
            await case.agent._execute_round(case.work)
        owned = case.agent._capture_owned_round(case.source)
        assert owned is not None and not owned._forwarded.is_set()
        assert owned._scheduler_wrapper.done()
    finally:
        await case.scheduler.stop()


@pytest.mark.asyncio
async def test_native_stop_retains_binding_until_exact_tail_then_retries_release(monkeypatch):
    from openjiuwen.core.controller.modules import task_scheduler
    monkeypatch.setattr(task_scheduler, '_STOP_TIMEOUT_SECONDS', 0.03)
    case = await harness_case(monkeypatch)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []
    async def invoke(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()
            order.append('wrapper-exited')
    async def agent_stop():
        assert original._exit.confirmed.done()
        order.append('agent-stop')
    async def post_run():
        order.append('session-post-run')
    monkeypatch.setattr(case.agent.react_agent, 'invoke', invoke)
    monkeypatch.setattr(case.agent, 'stop', agent_stop)
    case.case.session.post_run = post_run
    monkeypatch.setattr(case.harness, '_close_session', DeepAgentHarness._close_session.__get__(case.harness))
    a = await case.harness.send(HarnessInput(content='A'))
    original = case.harness._capture_owned_turn(a.turn_id)
    await entered.wait()
    b = await case.harness.send(HarnessInput(content='B'))
    try:
        with pytest.raises(HarnessStateError, match='unconfirmed'):
            await case.harness.stop()
        assert cancelled.is_set() and order == []
        assert case.harness._agent is case.agent and case.harness._agent_session is case.case.session
        cleanup = original._exit.cleanup
        release.set()
        await asyncio.wait_for(case.harness.stop(), 2)
        assert original._exit.cleanup is cleanup
        assert order == ['wrapper-exited', 'agent-stop', 'session-post-run']
        assert case.harness._agent is None and case.harness._agent_session is None
        assert (await terminals(case.harness, a.turn_id))[-1] is TurnEventKind.ABORTED
        assert (await terminals(case.harness, b.turn_id))[-1] is TurnEventKind.ABORTED
    finally:
        release.set()
        await case.harness.stop()
        supervisor = case.agent._interaction_supervisor_task
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await case.case.scheduler.stop()
