"""Actual DeepAgent queue/Round/controller provenance, without model IO."""

import asyncio
import copy
import json
from dataclasses import FrozenInstanceError, asdict, fields, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.controller.schema.execution_origin import (
    ExecutionOrigin,
    current_execution_origin,
    execution_origin_scope,
)
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import ActiveInteractionRound, RoundOutcome, RoundWorkItem, SendInputRequest
from openjiuwen.harness.task_loop.loop_queues import LoopQueues
from openjiuwen.harness.task_loop.task_loop_controller import TaskLoopController


def test_frozen_work_origin_is_live_private_and_copies_preserve_identity():
    source = ExecutionOrigin(object())
    with execution_origin_scope(source):
        raw = RoundWorkItem.user(request_id="request", inputs={"query": "hello"})
    assert raw.execution_origin is None
    work = raw.with_execution_origin(source)
    assert work is not raw and work.execution_origin is source
    for cloned in (copy.copy(work), copy.deepcopy(work), replace(work), replace(work, request_id="another")):
        assert cloned.execution_origin is source
    assert work.with_execution_origin(None).execution_origin is None
    assert raw.execution_origin is None
    public = asdict(work)
    assert "_origin" not in public and "execution_origin" not in public
    assert all(field.name != "_origin" for field in fields(work))
    with execution_origin_scope(source):
        restored = RoundWorkItem(**json.loads(json.dumps(public)))
    assert restored.execution_origin is None
    with pytest.raises(FrozenInstanceError):
        work._origin = None


def controller(queues):
    value = TaskLoopController()
    value._event_handler = SimpleNamespace(queues=queues)
    # The real wrapper resolves its original queues via the existing handler.
    value._get_interaction_queues = lambda: queues
    return value


@pytest.fixture
def agent(monkeypatch):
    value = DeepAgent(AgentCard(name="origin", description="synthetic"))
    value._interaction_started = True
    value._interaction_session = MagicMock()
    value._interaction_session.get_session_id.return_value = "session"
    value._react_agent = MagicMock()
    monkeypatch.setattr(value, "_notify_work", lambda: None)
    monkeypatch.setattr(value._interaction_output, "has_consumer", lambda: True)
    monkeypatch.setattr(value, "_emit_round_boundary", AsyncMock(return_value=True))
    return value


@pytest.mark.asyncio
async def test_send_queue_execute_actual_round_submits_original_input_event(agent, monkeypatch):
    source, unrelated = ExecutionOrigin(object()), ExecutionOrigin(object())
    events, seen = [], []
    queues = LoopQueues()
    loop = controller(queues)
    loop._card = agent.card
    loop._event_handler = SimpleNamespace(
        prepare_round=lambda: "round", wait_completion=AsyncMock(return_value={"result_type": "answer"})
    )

    async def publish(_agent_id, _session, event):
        events.append(event)
        seen.append(current_execution_origin())

    loop._event_queue = SimpleNamespace(publish_event=publish)
    coordinator = MagicMock()
    coordinator.is_aborted = False
    coordinator.should_continue.return_value = False
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    monkeypatch.setattr(agent, "prepare_interaction_task_loop", AsyncMock(return_value=(coordinator, loop)))
    monkeypatch.setattr(agent, "load_state", lambda *_: state)
    monkeypatch.setattr(agent, "save_state", lambda *_: seen.append(current_execution_origin()))
    monkeypatch.setattr(agent, "clear_state", lambda *_: None)
    monkeypatch.setattr(agent, "_write_round_result_to_stream", AsyncMock())
    with execution_origin_scope(source):
        await agent.send_input(SendInputRequest("request", {"query": "synthetic"}))
    work = agent._event_manager.next_work()
    assert work.execution_origin is source
    with execution_origin_scope(unrelated):
        await agent._execute_round(work)
        assert current_execution_origin() is unrelated
    assert len(events) == 1 and events[0].execution_origin is source
    assert seen and all(item is source for item in seen)
    assert state.pending_follow_ups == []
    assert current_execution_origin() is None


@pytest.mark.asyncio
async def test_send_captures_source_before_readiness_wait(agent):
    source, later = ExecutionOrigin(object()), ExecutionOrigin(object())
    entered, release = asyncio.Event(), asyncio.Event()

    class Ready:
        async def __aenter__(self):
            entered.set()
            await release.wait()

        async def __aexit__(self, *_):
            return None

    agent.set_fresh_input_context_factory(Ready)
    with execution_origin_scope(source):
        sending = asyncio.create_task(agent.send_input(SendInputRequest("request", {"query": "hello"})))
        await entered.wait()
    try:
        with execution_origin_scope(later):
            release.set()
            await sending
        assert agent._event_manager.next_work().execution_origin is source
    finally:
        release.set()
        await sending


@pytest.mark.asyncio
async def test_round_scope_stays_live_across_await_and_terminal_cleanup(agent, monkeypatch):
    source, supervisor = ExecutionOrigin(object()), ExecutionOrigin(object())
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def run(work, *_):
        seen.append(current_execution_origin())
        entered.set()
        await release.wait()
        seen.append(current_execution_origin())
        return RoundOutcome()

    async def boundary(*_):
        seen.append(current_execution_origin())
        return True

    monkeypatch.setattr(agent, "run_one_round", run)
    monkeypatch.setattr(agent, "_emit_round_boundary", boundary)
    work = RoundWorkItem.user(request_id="request", inputs={"query": "hello"}).with_execution_origin(source)
    with execution_origin_scope(supervisor):
        running = asyncio.create_task(agent._execute_round(work))
        await entered.wait()
    release.set()
    await running
    assert seen == [source, source, source]
    assert current_execution_origin() is None


@pytest.mark.asyncio
async def test_goal_resume_keeps_suspended_source_not_answer_source(agent, monkeypatch):
    source, answer = ExecutionOrigin(object()), ExecutionOrigin(object())
    suspended = RoundWorkItem.goal(
        inputs={"query": "goal"}, goal_id="g", revision=1, session_id="s"
    ).with_execution_origin(source)
    agent._active_interaction_round = ActiveInteractionRound(suspended, "original", waiting_for_input=True)
    resume = InteractiveInput()
    resume.update("question", "answer")
    work = RoundWorkItem.user(request_id="answer", inputs={"query": resume}).with_execution_origin(answer)
    seen = []

    async def run(actual, *_):
        seen.append((actual.execution_origin, current_execution_origin(), actual.context["goal_id"]))
        return RoundOutcome()

    monkeypatch.setattr(agent, "run_one_round", run)
    await agent._execute_round(work)
    assert seen == [(source, source, "g")]


@pytest.mark.asyncio
async def test_promote_uses_actual_followup_batch_not_supervisor(agent):
    source, unrelated = ExecutionOrigin(object()), ExecutionOrigin(object())
    queues = LoopQueues()
    agent._loop_controller = controller(queues)
    queues.push_follow_up("first", origin=source)
    queues.push_follow_up("second", origin=source)
    with execution_origin_scope(unrelated):
        await agent._promote_loop_follow_ups()
    first, second = agent._event_manager.next_work(), agent._event_manager.next_work()
    assert [first.query, second.query] == ["first", "second"]
    assert first.execution_origin is second.execution_origin is source


@pytest.mark.asyncio
async def test_mixed_promote_is_visible_error_and_finishes_output_without_dispatch(agent, monkeypatch):
    queues = LoopQueues()
    agent._loop_controller = controller(queues)
    queues.push_follow_up("first", origin=ExecutionOrigin(object()))
    queues.push_follow_up("other", origin=None)
    emitted, finished = AsyncMock(), AsyncMock()
    monkeypatch.setattr(agent._interaction_output, "emit", emitted)
    monkeypatch.setattr(agent._interaction_output, "finish_current", finished)
    with pytest.raises(ValueError, match="mixed"):
        await agent._promote_loop_follow_ups()
    assert agent._event_manager.next_work() is None and not queues.has_follow_up()
    assert emitted.await_args.args[0].type == "execution.error"
    finished.assert_awaited_once()


@pytest.mark.parametrize("stop", ["stop", "interrupt", "goal", "abort"])
def test_stopped_sourced_followup_never_persists_or_dispatches(agent, monkeypatch, stop):
    source = ExecutionOrigin(object())
    queues = LoopQueues()
    loop = controller(queues)
    queues.push_follow_up("not-auto-resumed", origin=source)
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    monkeypatch.setattr(agent, "load_state", lambda *_: state)
    monkeypatch.setattr(agent, "save_state", lambda *_: None)
    coordinator = MagicMock()
    coordinator.is_aborted = stop == "abort"
    coordinator.should_continue.return_value = stop != "stop"
    work = RoundWorkItem(
        kind="goal" if stop == "goal" else "user", request_id="r", inputs={"query": "hello"}
    ).with_execution_origin(source)
    with pytest.raises(BaseError, match="recovery is not supported"):
        agent._build_interaction_next_work(
            work=work,
            result={"result_type": "interrupt" if stop == "interrupt" else "answer"},
            session=agent._interaction_session,
            coordinator=coordinator,
            controller=loop,
        )
    assert state.pending_follow_ups == [] and not queues.has_follow_up()
    assert agent._event_manager.next_work() is None


def test_followup_and_remaining_task_keep_source_without_persistent_strings(agent, monkeypatch):
    source = ExecutionOrigin(object())
    queues = LoopQueues()
    loop = controller(queues)
    for value in ("first", "second"):
        queues.push_follow_up(value, origin=source)
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    monkeypatch.setattr(agent, "load_state", lambda *_: state)
    monkeypatch.setattr(agent, "save_state", lambda *_: None)
    monkeypatch.setattr(agent, "_has_remaining_tasks", lambda *_: True)
    coordinator = MagicMock()
    coordinator.is_aborted = False
    coordinator.should_continue.return_value = True
    work = RoundWorkItem.user(request_id="r", inputs={"query": "hello"}).with_execution_origin(source)
    results = []
    for _ in range(3):
        result = agent._build_interaction_next_work(
            work=work,
            result={"result_type": "answer"},
            session=agent._interaction_session,
            coordinator=coordinator,
            controller=loop,
        )
        results.append(result)
    assert [result.query for result in results] == ["first", "second", "hello"]
    assert all(result.execution_origin is source for result in results)
    assert state.pending_follow_ups == []


@pytest.mark.asyncio
async def test_legacy_generator_keeps_live_followups_out_of_state_and_does_not_leak_scope(agent, monkeypatch):
    source, consumer = ExecutionOrigin(object()), ExecutionOrigin(object())
    queues = LoopQueues()
    for text in ("first", "second"):
        queues.push_follow_up(text, origin=source)
    loop = controller(queues)
    loop._card = agent.card
    loop._event_handler = SimpleNamespace(
        prepare_round=lambda: "r", wait_completion=AsyncMock(return_value={"result_type": "answer"})
    )
    events = []

    async def publish(_id, _session, event):
        events.append(event)

    loop._event_queue = SimpleNamespace(publish_event=publish)
    loop.unbind_session, loop.stop = AsyncMock(), AsyncMock()
    coordinator = MagicMock()
    coordinator.is_aborted = False
    coordinator.should_continue.return_value = True
    coordinator.stop_reason = None
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    monkeypatch.setattr(agent, "_setup_task_loop", AsyncMock(return_value=(coordinator, loop)))
    agent._bound_session_id = "session"
    monkeypatch.setattr(agent, "load_state", lambda *_: state)
    monkeypatch.setattr(agent, "save_state", lambda *_: None)
    monkeypatch.setattr(agent, "_has_pending_session_spawn", lambda: False)
    monkeypatch.setattr(agent, "_has_remaining_tasks", lambda *_: False)
    context = SimpleNamespace(inputs=agent._normalize_inputs({"query": "initial"}))
    stream = agent._run_task_loop(context, agent._interaction_session)
    try:
        with execution_origin_scope(source):
            await anext(stream)
        assert current_execution_origin() is None and state.pending_follow_ups == []
        with execution_origin_scope(consumer):
            await anext(stream)
            assert current_execution_origin() is consumer
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        assert len(events) == 2 and all(event.execution_origin is source for event in events)
        assert state.pending_follow_ups == [] and not queues.has_follow_up()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_managed_stop_error_finishes_original_output_and_keeps_other_user_queue(agent, monkeypatch):
    source = ExecutionOrigin(object())
    queues = LoopQueues()
    queues.push_follow_up("must-not-auto-resume", origin=source)
    loop = controller(queues)
    loop._card = agent.card
    loop._event_handler = SimpleNamespace(
        prepare_round=lambda: "r", wait_completion=AsyncMock(return_value={"result_type": "answer"})
    )
    loop._event_queue = SimpleNamespace(publish_event=AsyncMock())
    coordinator = MagicMock()
    coordinator.is_aborted = False
    coordinator.should_continue.return_value = False
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    monkeypatch.setattr(agent, "prepare_interaction_task_loop", AsyncMock(return_value=(coordinator, loop)))
    monkeypatch.setattr(agent, "load_state", lambda *_: state)
    monkeypatch.setattr(agent, "save_state", lambda *_: None)
    monkeypatch.setattr(agent, "clear_state", lambda *_: None)
    monkeypatch.setattr(agent, "_write_round_result_to_stream", AsyncMock())
    emit, finish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(agent._interaction_output, "emit", emit)
    monkeypatch.setattr(agent._interaction_output, "finish_current", finish)
    other = RoundWorkItem.user(request_id="unrelated", inputs={"query": "other"})
    agent._event_manager.push_user(other)
    await agent._execute_round(
        RoundWorkItem.user(request_id="r", inputs={"query": "initial"}).with_execution_origin(source)
    )
    assert loop._event_queue.publish_event.await_count == 1
    assert emit.await_args.args[0].type == "execution.error"
    finish.assert_awaited_once()
    assert not queues.has_follow_up() and state.pending_follow_ups == []
    assert agent._event_manager.next_work() is other


def test_legacy_none_still_persists_followups_and_managed_cannot_adopt_them(agent, monkeypatch):
    queues = LoopQueues()
    loop = controller(queues)
    queues.push_follow_up("legacy", origin=None)
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    monkeypatch.setattr(agent, "load_state", lambda *_: state)
    monkeypatch.setattr(agent, "save_state", lambda *_: None)
    coordinator = MagicMock()
    coordinator.is_aborted = False
    coordinator.should_continue.return_value = False
    work = RoundWorkItem.user(request_id="r", inputs={"query": "initial"})
    assert (
        agent._build_interaction_next_work(
            work=work,
            result={"result_type": "answer"},
            session=agent._interaction_session,
            coordinator=coordinator,
            controller=loop,
        )
        is None
    )
    assert state.pending_follow_ups == ["legacy"]
    with pytest.raises(BaseError, match="unavailable or mixed"):
        agent._build_interaction_next_work(
            work=work.with_execution_origin(ExecutionOrigin(object())),
            result={"result_type": "answer"},
            session=agent._interaction_session,
            coordinator=coordinator,
            controller=loop,
        )
    assert state.pending_follow_ups == ["legacy"]


@pytest.mark.asyncio
async def test_steer_requires_same_actual_round_origin(agent):
    from openjiuwen.harness.schema.interaction import InputDispatchMode

    source, other = ExecutionOrigin(object()), ExecutionOrigin(object())
    queues = LoopQueues()
    agent._loop_controller = controller(queues)
    work = RoundWorkItem.user(request_id="r", inputs={"query": "initial"}).with_execution_origin(source)
    agent._active_interaction_round = ActiveInteractionRound(work, "task")
    with execution_origin_scope(other), pytest.raises(BaseError, match="does not match"):
        await agent.send_input(SendInputRequest("other", {"query": "steer"}, mode=InputDispatchMode.STEER))
    assert queues.drain_steering(expected_origin=source) == []
    with execution_origin_scope(source):
        await agent.send_input(SendInputRequest("same", {"query": "steer"}, mode=InputDispatchMode.STEER))
    assert queues.drain_steering(expected_origin=source) == ["steer"]


@pytest.mark.asyncio
async def test_legacy_generator_stopped_sourced_batch_cleans_original_controller(agent, monkeypatch):
    source = ExecutionOrigin(object())
    queues = LoopQueues()
    loop = controller(queues)
    loop.submit_round = AsyncMock()

    async def completed(**_):
        queues.push_follow_up("held-after-interrupt", origin=source)
        return {"result_type": "interrupt"}

    loop.wait_round_completion = completed
    loop.unbind_session, loop.stop = AsyncMock(), AsyncMock()
    coordinator = MagicMock()
    coordinator.should_continue.return_value = True
    coordinator.stop_reason = None
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    monkeypatch.setattr(agent, "_setup_task_loop", AsyncMock(return_value=(coordinator, loop)))
    agent._bound_session_id = "session"
    monkeypatch.setattr(agent, "load_state", lambda *_: state)
    monkeypatch.setattr(agent, "save_state", lambda *_: None)
    monkeypatch.setattr(agent, "_has_pending_session_spawn", lambda: False)
    context = SimpleNamespace(inputs=agent._normalize_inputs({"query": "initial"}))
    stream = agent._run_task_loop(context, agent._interaction_session)
    with execution_origin_scope(source):
        assert (await anext(stream))["result_type"] == "interrupt"
        with pytest.raises(BaseError, match="recovery is not supported"):
            await anext(stream)
    loop.submit_round.assert_awaited_once()
    assert loop.submit_round.await_args.kwargs["origin"] is source
    loop.unbind_session.assert_awaited_once()
    loop.stop.assert_awaited_once()
    assert not queues.has_follow_up() and state.pending_follow_ups == []


@pytest.mark.asyncio
async def test_source_less_round_masks_inherited_supervisor_origin(agent, monkeypatch):
    seen = []

    async def run(*_):
        seen.append(current_execution_origin())
        return RoundOutcome()

    monkeypatch.setattr(agent, "run_one_round", run)
    unrelated = ExecutionOrigin(object())
    with execution_origin_scope(unrelated):
        await agent._execute_round(RoundWorkItem.user(request_id="legacy", inputs={"query": "hello"}))
        assert current_execution_origin() is unrelated
    assert seen == [None]


@pytest.mark.parametrize("managed", [False, True])
def test_stop_evaluator_observes_saved_state_after_queue_drain(agent, monkeypatch, managed):
    from openjiuwen.harness.schema.stop_condition import StopConditionEvaluator
    from openjiuwen.harness.task_loop.loop_coordinator import LoopCoordinator

    source = ExecutionOrigin(object()) if managed else None
    queues = LoopQueues()
    queues.push_follow_up("queued", origin=source)
    state = SimpleNamespace(stop_condition_state=None, pending_follow_ups=[])
    current, observations, saves = [state], [], []
    monkeypatch.setattr(agent, "load_state", lambda *_: current[0])

    def save(_session, value):
        saves.append(list(value.pending_follow_ups))
        current[0] = value

    monkeypatch.setattr(agent, "save_state", save)

    class Evaluator(StopConditionEvaluator):
        def should_stop(self, ctx):
            observations.append((len(saves), queues.has_follow_up(), ctx.iteration))
            if not managed:
                current[0] = SimpleNamespace(stop_condition_state=None, pending_follow_ups=["reloaded"])
            return False

    result = agent._build_interaction_next_work(
        work=RoundWorkItem.user(request_id="r", inputs={"query": "initial"}).with_execution_origin(source),
        result={"result_type": "answer"},
        session=agent._interaction_session,
        coordinator=LoopCoordinator([Evaluator()]),
        controller=controller(queues),
    )
    assert observations == [(1, False, 1)]
    assert result.query == ("queued" if managed else "reloaded")
    assert result.execution_origin is source
