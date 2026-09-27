# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Goal interruption must retain the original attempt owner."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.goal import GoalManager
from tests.unit_tests.harness.goal.test_goal_driver import MemoryStore


@pytest.mark.asyncio
async def test_interrupt_does_not_schedule_another_goal_attempt(monkeypatch):
    agent = DeepAgent(AgentCard(name="goal-wait", description="test"))
    agent._interaction_started = True
    session = MagicMock()
    session.get_session_id.return_value = "session-1"
    agent._interaction_session = session
    monkeypatch.setattr(agent, "_notify_work", MagicMock())
    monkeypatch.setattr(agent, "_emit_round_boundary", AsyncMock(return_value=False))
    monkeypatch.setattr(agent, "_write_round_result_to_stream", AsyncMock())
    monkeypatch.setattr(agent, "load_state", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(agent, "save_state", MagicMock())
    monkeypatch.setattr(agent, "clear_state", MagicMock())
    monkeypatch.setattr(agent, "_build_interaction_next_work", MagicMock(return_value=None))
    controller = MagicMock()
    controller.submit_round = AsyncMock()
    controller.wait_round_completion = AsyncMock(return_value={"result_type": "interrupt"})
    monkeypatch.setattr(agent, "prepare_interaction_task_loop", AsyncMock(return_value=(MagicMock(), controller)))
    agent.goal_manager = GoalManager(
        store=MemoryStore(), control_lock=agent._interaction_control_lock,
        event_manager=agent._event_manager, has_output_stream=agent.has_output_stream,
        cancel_active_round=agent._cancel_active_round, emit_event=lambda _: None,
        notify_work=agent._notify_work,
    )
    lease = await agent._interaction_output.attach()
    assert lease is not None
    try:
        goal = await agent.goal_manager.set("wait for actual approval", max_attempts=1)
        work = agent._event_manager.next_work()
        assert work is not None
        await agent._execute_round(work)
        assert agent.goal_manager.peek().attempt_count == 1
        # This is the actual supervisor idle/attach path that previously
        # recreated Goal work even though the permission question was unanswered.
        for _ in range(3):
            await agent._close_idle_output_if_finished()
            assert agent._event_manager.next_work() is None
            assert agent.goal_manager.peek().attempt_count == 1
        assert agent._event_manager.has_running_goal(goal_id=goal.goal_id)
    finally:
        await agent._interaction_output.detach(lease.token)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_again", [False, True, "cancel"])
@pytest.mark.parametrize("pause", [False, True])
@pytest.mark.parametrize("queued_user", [False, True])
async def test_answer_resumes_original_goal_attempt_and_report(interrupt_again, pause, queued_user):
    """Real supervisor/kernel/rail/Manager; only the inner model is scripted."""
    import asyncio

    from openjiuwen.core.runner import Runner
    from openjiuwen.core.session import InteractiveInput
    from openjiuwen.harness.goal import (
        GoalAssessment,
        GoalAssessmentStatus,
        GoalEvaluator,
        GoalStatus,
        GoalStopConfig,
        GoalStopStrategy,
    )
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from openjiuwen.harness.schema.interaction import SendInputRequest
    from tests.unit_tests.agent_teams.harness.fixtures import FakeReactAgent

    agent = DeepAgent(AgentCard(name="goal-answer", description="test"))
    agent.configure(DeepAgentConfig(enable_task_loop=True))
    entered = asyncio.Queue()

    class React(FakeReactAgent):
        async def invoke(self, inputs, session, **kwargs):
            self.invocations.append(inputs)
            entered.put_nowait(len(self.invocations))
            if len(self.invocations) == 1:
                agent._task_completion_rail._goal_report_sink.submit(
                    GoalAssessment(status=GoalAssessmentStatus.COMPLETE, evidence="same attempt evidence")
                )
                return {"result_type": "interrupt"}
            if inputs["query"] == "ordinary user after answer":
                assert agent.goal_manager.peek().status is GoalStatus.COMPLETED
                return {"result_type": "answer", "output": "ordinary user complete"}
            assert isinstance(inputs["query"], InteractiveInput)
            if interrupt_again == "cancel":
                await asyncio.Event().wait()
            if interrupt_again is True and len(self.invocations) == 2:
                return {"result_type": "interrupt"}
            return {"result_type": "answer", "output": "done after actual answer"}

    react = React(agent.card)
    agent.set_react_agent(react, initialized=True)
    reader = None
    await Runner.start()
    try:
        await agent.start()
        rail = agent._task_completion_rail
        rail._goal_evaluator = GoalEvaluator(GoalStopConfig(strategy=GoalStopStrategy.AGENT_REPORT))
        stream = await agent.attach_output()
        assert stream is not None

        async def drain():
            return [chunk async for chunk in stream]

        reader = asyncio.create_task(drain())
        record = await agent.goal_manager.set("wait then finish", max_attempts=1)
        assert await asyncio.wait_for(entered.get(), 2) == 1
        for _ in range(30):
            await asyncio.sleep(0)
        assert len(react.invocations) == 1
        assert agent._event_manager.has_running_goal(goal_id=record.goal_id)
        if queued_user:
            await agent.send_input(SendInputRequest(
                request_id="ordinary", inputs={"query": "ordinary user after answer"},
            ))
            for _ in range(30):
                await asyncio.sleep(0)
            assert len(react.invocations) == 1
        if pause:
            paused = await agent.goal_manager.pause()
            resumed = await agent.goal_manager.resume()
            assert paused.revision == resumed.revision == record.revision
        assert await agent.attach_output() is None
        assert agent.goal_manager.peek().attempt_count == 1

        for index in range(2 if interrupt_again is True else 1):
            answer = InteractiveInput()
            answer.update(f"permission-{index}", {"action": "allow_once"})
            await agent.send_input(SendInputRequest(request_id=f"answer-{index}", inputs={"query": answer}))
            assert await asyncio.wait_for(entered.get(), 2) == index + 2
            if interrupt_again == "cancel":
                active_task = agent._interaction_round_task
                await stream.aclose()
                await asyncio.wait_for(asyncio.gather(active_task, return_exceptions=True), 3)
                cancelled = agent.goal_manager.peek()
                assert cancelled.status is GoalStatus.ACTIVE
                assert cancelled.attempt_count == 1
                assert cancelled.last_assessment is None
                assert rail._goal_report_sink.report is not None
                assert not agent._event_manager.has_running_goal(goal_id=record.goal_id)
                return
            for _ in range(30):
                await asyncio.sleep(0)
            assert agent.goal_manager.peek().attempt_count == 1
            if interrupt_again is True and index == 0:
                assert agent.goal_manager.peek().status is GoalStatus.ACTIVE
                assert rail._goal_report_sink.report is not None
        await asyncio.wait_for(reader, 3)
        completed = agent.goal_manager.peek()
        assert completed.status is GoalStatus.COMPLETED
        assert completed.attempt_count == completed.last_assessed_attempt == 1
        assert completed.revision == record.revision
        assert completed.last_assessment.evidence == "same attempt evidence"
        assert not agent._event_manager.has_running_goal(goal_id=record.goal_id)
    finally:
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await agent.stop()
        await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["clear", "overwrite", "stop"])
async def test_parked_goal_cleanup_retires_owner_and_old_interruption(monkeypatch, operation):
    from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
    from openjiuwen.harness.schema.interaction import ActiveInteractionRound, RoundWorkItem

    agent = DeepAgent(AgentCard(name="goal-cleanup", description="test"))
    agent._interaction_started = True
    session = MagicMock()
    session.close_stream = AsyncMock()
    agent._interaction_session = session
    agent.abort = AsyncMock()
    monkeypatch.setattr(agent, "_notify_work", MagicMock())
    agent.goal_manager = GoalManager(
        store=MemoryStore(), control_lock=agent._interaction_control_lock,
        event_manager=agent._event_manager, has_output_stream=agent.has_output_stream,
        cancel_active_round=agent._cancel_active_round, emit_event=lambda _: None,
        notify_work=agent._notify_work,
    )
    lease = await agent._interaction_output.attach()
    try:
        goal = await agent.goal_manager.set("old question")
        work = agent._event_manager.next_work()
        agent._event_manager.mark_started(work)
        agent._active_interaction_round = ActiveInteractionRound(work, waiting_for_input=True)
        await agent.goal_manager.begin_attempt(goal_id=goal.goal_id, revision=goal.revision)
        if operation == "clear":
            await agent.goal_manager.clear()
            assert agent.goal_manager.peek() is None
        elif operation == "overwrite":
            replacement = await agent.goal_manager.set("new goal", overwrite_confirmed=True)
            assert replacement.goal_id != goal.goal_id
        else:
            await agent.stop()
        assert agent._active_interaction_round is None
        assert not agent._event_manager.has_running_goal(goal_id=goal.goal_id)
        session.update_state.assert_any_call({INTERRUPTION_KEY: None})
        agent.abort.assert_awaited()
        if operation == "overwrite":
            new_work = agent._event_manager.next_work()
            assert isinstance(new_work, RoundWorkItem)
            assert new_work.context["goal_id"] == replacement.goal_id
    finally:
        await agent._interaction_output.detach(lease.token)


def test_resume_selection_keeps_ordinary_fifo_and_queued_goal():
    from openjiuwen.core.session import InteractiveInput
    from openjiuwen.harness.schema.interaction import RoundWorkItem
    from openjiuwen.harness.task_loop.event_manager import EventManager

    events = EventManager()
    first = RoundWorkItem.user(request_id="first", inputs={"query": "first"})
    second = RoundWorkItem.user(request_id="second", inputs={"query": "second"})
    answer = RoundWorkItem.user(request_id="answer", inputs={"query": InteractiveInput()})
    goal = RoundWorkItem.goal(inputs={"query": "goal"}, goal_id="goal", revision=0, session_id="session")
    for work in (first, second, answer):
        events.push_user(work)
    events.push_goal(goal)
    assert events.next_work(resume_only=True) == answer
    assert events.next_work(resume_only=True) is None
    assert [events.next_work(), events.next_work(), events.next_work()] == [first, second, goal]
