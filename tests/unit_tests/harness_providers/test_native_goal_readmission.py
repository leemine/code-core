"""Actual Native admission/exit barrier and Manager; no external Provider IO."""

import asyncio

import pytest

from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.readmission import _NativeGoalReadmissionPlan
from openjiuwen.harness.goal.store import SessionGoalStore
from openjiuwen.harness_protocol import HarnessInput, TurnEventKind
from tests.unit_tests.harness_providers.test_native_exact_exit import harness_case, terminals


async def setup(monkeypatch):
    c = await harness_case(monkeypatch)
    agent = c.agent
    monkeypatch.setattr(agent, "_notify_work", lambda: None)
    emitted = []

    def manager():
        result = GoalManager(
            store=SessionGoalStore(c.case.session),
            event_manager=agent.event_manager,
            control_lock=agent._interaction_control_lock,
            has_output_stream=agent.has_output_stream,
            cancel_active_round=agent._cancel_active_round,
            emit_event=emitted.append,
            notify_work=agent._notify_work,
        )
        result._execution._owner = agent
        return result

    initial = manager()
    record = await initial.set("saved goal")
    agent.goal_manager = manager()  # new manager: persisted state has no live slot
    plans, previous, dispatched = [], [None], []

    def prepare(instance, content, origin):
        assert instance is agent and origin is c.harness.active_turn._origin
        selector = instance.goal_manager._capture_idle_readmission(expected_record=instance.goal_manager.peek())
        plan = _NativeGoalReadmissionPlan(selector, "attach", lambda: None, previous[0])
        plans.append(plan)
        return plan

    async def dispatch(instance, request, content, resuming):
        # Read the result already published by the pre-attach transaction. No
        # second resume/set, no additional stream reader, no model invocation.
        result = plans[-1].result()
        dispatched.append(result)
        assert instance.goal_manager._execution_origin[3] is c.harness.active_turn._origin
        assert instance.event_manager._goal_queue[0].execution_origin is c.harness.active_turn._origin
        return False

    object.__setattr__(c.harness._host_hooks, "prepare_goal_readmission", prepare)
    object.__setattr__(c.harness._host_hooks, "dispatch_input", dispatch)
    c.plans, c.previous, c.dispatched, c.record = plans, previous, dispatched, record
    return c


async def submit(c, text):
    receipt = await c.harness.send(HarnessInput(content=text))
    pending = c.harness._capture_owned_turn(receipt.turn_id)
    result = await asyncio.wait_for(terminals(c.harness, receipt.turn_id), 2)
    return pending, result


@pytest.mark.asyncio
async def test_fresh_cold_then_exact_hot_readmission_use_new_pending_sources(monkeypatch):
    c = await setup(monkeypatch)
    try:
        first, lifecycle = await submit(c, "A")
        assert lifecycle == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
        assert first._exit.confirmed.done() and first._exit.cleanup.done()
        c.previous[0] = first
        second, lifecycle = await submit(c, "B")
        assert lifecycle == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
        assert second._origin is not first._origin
        assert c.agent.goal_manager._execution_origin[3] is second._origin
        assert [r.revision for r in c.dispatched] == [c.record.revision] * 2
        assert not c.agent.has_output_stream() and not c.agent.event_manager.has_pending_work()
        assert c.agent.react_agent.invoke_calls == []
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["missing", "wrong_source", "confirmed", "cleanup", "round_handle", "execution_done"]
)
async def test_hot_readmission_requires_exact_old_exit_receipt(monkeypatch, change):
    c = await setup(monkeypatch)

    def restore():
        pass

    try:
        first, lifecycle = await submit(c, "A")
        assert lifecycle[-1] is TurnEventKind.FINISHED
        c.previous[0] = first
        if change == "missing":
            c.previous[0] = None
        elif change == "wrong_source":
            old = first._origin
            first._origin = c.origins["B"]

            def restore():
                first._origin = old
        elif change == "confirmed":
            old = first._exit.confirmed
            first._exit.confirmed = asyncio.get_running_loop().create_future()

            def restore():
                first._exit.confirmed = old
        elif change == "cleanup":
            old = first._exit.cleanup
            first._exit.cleanup = asyncio.create_task(asyncio.Event().wait())

            def restore():
                first._exit.cleanup.cancel()
                first._exit.cleanup = old
        elif change == "round_handle":
            old = first._exit.round_handle
            first._exit.round_handle = None

            def restore():
                first._exit.round_handle = old
        else:
            first._execution_done.clear()

            def restore():
                first._execution_done.set()

        _, lifecycle = await submit(c, "B")
        assert lifecycle[-1] is TurnEventKind.FAILED
        assert len(c.dispatched) == 1
        assert not c.agent.has_output_stream() and not c.agent.event_manager.has_pending_work()
    finally:
        restore()
        await c.harness.stop()


@pytest.mark.asyncio
async def test_later_native_turn_cannot_claim_restored_slot_as_cold(monkeypatch):
    c = await setup(monkeypatch)
    try:
        first, _ = await submit(c, "A")
        # Reconstructed manager does not make an already-used Native Session
        # a fresh cold admission. Exact original Pending is still required.
        current = c.agent.goal_manager
        c.agent.goal_manager = GoalManager(
            store=current._store, execution=current._execution, control_lock=current._control_lock
        )
        _, lifecycle = await submit(c, "B")
        assert lifecycle[-1] is TurnEventKind.FAILED
        assert len(c.dispatched) == 1 and first._exit.confirmed.done()
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
async def test_ordinary_native_input_cannot_ensure_cold_active_without_plan(monkeypatch):
    c = await setup(monkeypatch)
    object.__setattr__(c.harness._host_hooks, "prepare_goal_readmission", None)
    try:
        _, lifecycle = await submit(c, "A")
        assert lifecycle[-1] is TurnEventKind.FAILED
        assert not c.plans and not c.dispatched
        assert not c.agent.has_output_stream() and not c.agent.event_manager.has_pending_work()
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
async def test_hot_exit_proof_rechecked_after_output_attach_wait(monkeypatch):
    c = await setup(monkeypatch)
    release = asyncio.Event()
    running = None
    old = None
    try:
        first, _ = await submit(c, "A")
        c.previous[0] = first
        original_attach = c.agent._attach_output_locked
        entered = asyncio.Event()

        async def delayed():
            stream = await original_attach()
            entered.set()
            await release.wait()
            return stream

        monkeypatch.setattr(c.agent, "_attach_output_locked", delayed)
        running = asyncio.create_task(submit(c, "B"))
        await asyncio.wait_for(entered.wait(), 1)
        old = first._exit.confirmed
        first._exit.confirmed = asyncio.get_running_loop().create_future()
        release.set()
        _, lifecycle = await running
        assert lifecycle[-1] is TurnEventKind.FAILED
        assert len(c.dispatched) == 1 and not c.agent.has_output_stream()
    finally:
        release.set()
        if old is not None:
            first._exit.confirmed = old
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await c.harness.stop()


@pytest.mark.asyncio
async def test_actual_cold_goal_attempts_keep_new_pending_source_to_completion():
    from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, current_execution_origin
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.goal import (
        GoalAssessment,
        GoalAssessmentStatus,
        GoalEvaluator,
        GoalRecord,
        GoalStopConfig,
        GoalStopStrategy,
    )
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from openjiuwen.harness_protocol import HarnessContext
    from openjiuwen.harness_providers.native.harness import DeepAgentHarness
    from openjiuwen.harness_providers.native.host import NativeHostHooks
    from tests.unit_tests.agent_teams.harness.fixtures import FakeReactAgent

    agent = DeepAgent(AgentCard(name="readmitted-goal"))
    agent.configure(DeepAgentConfig(enable_task_loop=True))
    sources, plans = [], []

    class React(FakeReactAgent):
        async def invoke(self, inputs, session, **kwargs):
            self.invocations.append(inputs)
            sources.append(current_execution_origin())
            status = GoalAssessmentStatus.COMPLETE if len(sources) == 2 else GoalAssessmentStatus.CONTINUE
            agent._task_completion_rail._goal_report_sink.submit(
                GoalAssessment(status=status, evidence="synthetic component evidence")
            )
            return {"result_type": "answer", "output": "synthetic Goal result"}

    agent.set_react_agent(React(agent.card), initialized=True)
    source = ExecutionOrigin(object(), _checker=lambda: None)

    async def before_start(instance, session):
        store = SessionGoalStore(session)
        store.save(
            GoalRecord(
                goal_id="persisted", session_id=session.get_session_id(), objective="retained goal", max_attempts=2
            )
        )

    def prepare(instance, _content, _origin):
        plan = _NativeGoalReadmissionPlan(
            instance.goal_manager._capture_idle_readmission(expected_record=instance.goal_manager.peek()),
            "attach",
            lambda: None,
        )
        plans.append(plan)
        return plan

    async def dispatch(*_):
        assert plans[-1].result().goal_id == "persisted"
        return True

    harness = DeepAgentHarness(
        lambda _: agent,
        observe_tools=False,
        host_hooks=NativeHostHooks(
            before_start=before_start,
            capture_execution_origin=lambda _: source,
            prepare_goal_readmission=prepare,
            dispatch_input=dispatch,
        ),
    )
    await Runner.start()
    try:
        await harness.start(
            HarnessContext(agent_name="native", agent_id="goal", host_session_id="readmitted", system_prompt="")
        )
        agent._task_completion_rail._goal_evaluator = GoalEvaluator(
            GoalStopConfig(strategy=GoalStopStrategy.AGENT_REPORT)
        )
        receipt = await harness.send(HarnessInput(content="attach"))
        pending = harness._capture_owned_turn(receipt.turn_id)
        lifecycle = await asyncio.wait_for(terminals(harness, receipt.turn_id), 4)
        assert lifecycle == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
        assert len(sources) == 2 and all(value is pending._origin for value in sources)
        assert agent.goal_manager.peek().status.value == "completed"
        assert pending._exit.confirmed.done() and pending._exit.cleanup.done()
    finally:
        await harness.stop()
        await Runner.stop()
