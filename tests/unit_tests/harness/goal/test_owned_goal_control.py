"""Original Goal selector over actual manager/store/EventManager/DeepAgent."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.goal.schema import GoalStatus
from openjiuwen.harness.schema.interaction import InteractionPhase

from .test_goal_manager import ManagerHarness


@pytest_asyncio.fixture
async def case():
    h = ManagerHarness()
    agent = DeepAgent(AgentCard(name="owned-goal-test"))
    agent._interaction_session = h.session
    agent._event_manager = h.events
    agent._interaction_started = True
    agent._try_transition_interaction_phase(InteractionPhase.IDLE)
    agent.goal_manager = h.manager
    # Real Native adapter's optional ownership binding; legacy callbacks remain
    # on the existing constructor, but private control never invokes them.
    h.manager._execution._owner = agent
    live = [True]

    def check():
        if not live[0]:
            raise PermissionError("original root ended")

    source = ExecutionOrigin(object(), _checker=check)
    with execution_origin_scope(source):
        record = await h.manager.set("original")
    work = h.events.next_work()
    owned = agent._prepare_owned_round(work)
    parked = asyncio.Event()
    task = asyncio.create_task(parked.wait())
    owned._facade_task = task
    try:
        yield SimpleNamespace(**locals())
    finally:
        parked.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_original_pause_preserves_root_and_revision(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    result = await c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None)
    assert result.status is GoalStatus.PAUSED and result.revision == c.record.revision
    assert c.h.manager._execution_origin[3] is c.source
    assert not c.task.done() and not c.h.cancel_calls


@pytest.mark.asyncio
async def test_checker_cannot_replace_source_before_pause(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)

    def replace():
        c.h.manager._execution_origin = (*c.h.manager._execution_origin[:3], ExecutionOrigin(object()))

    with pytest.raises(PermissionError):
        await c.h.manager._apply_owned_control(target, action="pause", check_current=replace)
    assert c.h.store.load().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
async def test_clear_joins_original_tail_without_holding_goal_lock(case):
    c = case
    c.task.cancel()
    await asyncio.gather(c.task, return_exceptions=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def facade():
        try:
            await asyncio.Event().wait()
        finally:
            async with c.h.manager._control_lock:
                entered.set()
            await release.wait()

    c.task = asyncio.create_task(facade())
    c.owned._facade_task = c.task
    await asyncio.sleep(0)
    target = c.h.manager._capture_owned_control(expected_origin=c.source)

    def checker():
        return None

    operation = asyncio.create_task(c.h.manager._apply_owned_control(target, action="clear", check_current=checker))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not operation.done() and c.h.store.load() is None
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not c.task.done()
        release.set()
        removed = await asyncio.wait_for(
            c.h.manager._apply_owned_control(target, action="clear", check_current=checker), 1
        )
        assert removed.goal_id == c.record.goal_id and c.task.done()
    finally:
        release.set()
        await asyncio.gather(c.task, operation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["manager_store", "lock", "source", "revision", "session", "root_work", "facade", "inputs"]
)
async def test_callback_changes_are_rechecked_before_side_effect(case, change):
    c = case
    manager = c.h.manager
    target = manager._capture_owned_control(expected_origin=c.source)
    before = c.h.store.load().to_dict()
    original_store = manager._store
    original_session = c.h.session._session_id

    def checker():
        if change == "manager_store":
            manager._store = object()
        elif change == "lock":
            manager._control_lock = asyncio.Lock()
        elif change == "source":
            manager._execution_origin = (*manager._execution_origin[:3], ExecutionOrigin(object()))
        elif change == "revision":
            record = c.h.store.load()
            record.revision += 1
            c.h.store.save(record)
        elif change == "session":
            c.h.session._session_id = "other"
        elif change == "root_work":
            from openjiuwen.harness.schema.interaction import RoundWorkItem

            c.owned.work = RoundWorkItem.user(request_id="other", inputs={"query": "other"})
        elif change == "facade":
            c.owned._facade_task = asyncio.current_task()
        else:
            c.work.inputs["query"] = "different"

    try:
        with pytest.raises(PermissionError):
            await manager._apply_owned_control(target, action="pause", check_current=checker)
        # No pause/write or queue cancellation, apart from the explicit attacker mutation.
        assert c.h.store.load().status is GoalStatus.ACTIVE
        if change not in {"revision", "session"}:
            assert c.h.store.load().to_dict() == before
    finally:
        manager._store = original_store
        c.h.session._session_id = original_session


@pytest.mark.asyncio
async def test_usage_progress_before_control_does_not_change_goal_generation(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    record = c.h.store.load()
    record.token_usage.accumulate(3, 5, 0)
    record.attempt_count += 1
    record.touch()
    c.h.store.save(record)
    result = await c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None)
    assert result.token_usage.input_tokens == 3 and result.attempt_count == 1


@pytest.mark.asyncio
async def test_waiting_control_revalidates_original_root(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    lock = c.h.manager._control_lock
    await lock.acquire()
    task = asyncio.create_task(c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None))
    await asyncio.sleep(0)
    c.live[0] = False
    lock.release()
    with pytest.raises(PermissionError):
        await task
    assert c.h.store.load().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
async def test_pause_commit_wait_checks_again_before_output(case):
    c = case
    entered, release = asyncio.Event(), asyncio.Event()

    async def commit():
        entered.set()
        await release.wait()

    c.h.session.commit = commit
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    before = len(c.h.emitted)
    task = asyncio.create_task(c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None))
    await entered.wait()
    c.live[0] = False
    release.set()
    with pytest.raises(PermissionError):
        await task
    assert c.h.store.load().status is GoalStatus.PAUSED  # committed, not silently rolled back
    assert len(c.h.emitted) == before


@pytest.mark.asyncio
async def test_same_live_attempt_resume_keeps_revision_and_original_source(case):
    c = case
    await c.h.manager.pause()
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    with execution_origin_scope(ExecutionOrigin(object())):
        result = await c.h.manager._apply_owned_control(target, action="resume", check_current=lambda: None)
    assert result.status is GoalStatus.ACTIVE and result.revision == c.record.revision
    assert c.h.manager._execution_origin[3] is c.source
    assert not c.h.events.has_pending_work() and not c.task.done()


@pytest.mark.asyncio
async def test_idle_resume_cannot_borrow_queued_goal(case):
    c = case
    await c.h.manager.pause()
    c.agent._active_interaction_round = None
    c.h.events.mark_finished(c.work)
    c.h.events.push_goal(c.work)
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    with pytest.raises(PermissionError, match="idle"):
        await c.h.manager._apply_owned_control(target, action="resume", check_current=lambda: None)
    assert c.h.store.load().status is GoalStatus.PAUSED


@pytest.mark.asyncio
async def test_active_overwrite_joins_only_old_goal_and_preserves_new_queue(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    result = await c.h.manager._apply_owned_control(
        target, action="set", check_current=lambda: None, objective="replacement", overwrite_confirmed=True
    )
    assert result.goal_id != c.record.goal_id
    assert c.task.done() and not c.h.cancel_calls
    next_work = c.h.events.next_work()
    assert next_work.context["goal_id"] == result.goal_id
    assert next_work.execution_origin is c.source


@pytest.mark.asyncio
async def test_dequeued_old_goal_cannot_start_after_clear(case):
    c = case
    c.agent._active_interaction_round = None
    c.h.events.mark_finished(c.work)
    c.h.events.push_goal(c.work)
    dequeued = c.h.events.next_work()  # actual supervisor-local original item
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    await c.h.manager._apply_owned_control(target, action="clear", check_current=lambda: None)
    with pytest.raises(PermissionError, match="cleared or replaced"):
        c.agent._prepare_owned_round(dequeued)
    assert c.agent._active_interaction_round is None


@pytest.mark.asyncio
async def test_old_round_after_prepare_await_cannot_publish_input(case, monkeypatch):
    c = case
    from unittest.mock import AsyncMock

    from openjiuwen.harness.schema.config import DeepAgentConfig

    c.agent.configure(DeepAgentConfig(enable_task_loop=True))
    entered, release = asyncio.Event(), asyncio.Event()
    loop = SimpleNamespace(submit_round=AsyncMock())
    # A real run_one_round waits in its existing preparation seam before input.
    c.owned._controller = loop
    c.agent._loop_controller = loop

    async def prepare(_):
        entered.set()
        await release.wait()
        return c.agent.loop_coordinator, loop

    monkeypatch.setattr(c.agent, "prepare_interaction_task_loop", prepare)
    running = asyncio.create_task(c.agent.run_one_round(c.work, c.owned.task_id, c.h.session))
    await entered.wait()
    c.h.store.clear()
    release.set()
    outcome = await running
    assert outcome.error_code == "round_execution_error"
    loop.submit_round.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [False, True, "yes"])
async def test_checker_must_return_none(case, returned):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    with pytest.raises(TypeError):
        await c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: returned)
    assert c.h.store.load().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
async def test_capture_and_apply_different_tasks_with_original_checker(case):
    c = case

    async def capture():
        return c.h.manager._capture_owned_control(expected_origin=c.source)

    selector = await asyncio.create_task(capture())
    result = await c.h.manager._apply_owned_control(selector, action="pause", check_current=lambda: None)
    assert result.status is GoalStatus.PAUSED


@pytest.mark.asyncio
async def test_clear_exit_failure_retries_only_original_tail(case, monkeypatch):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)

    def checker():
        return None

    original = c.agent._drain_owned_round
    attempts = []

    async def drain(owned, **kwargs):
        attempts.append(owned)
        if len(attempts) == 1:
            raise RuntimeError("synthetic temporary exit failure")
        return await original(owned, **kwargs)

    monkeypatch.setattr(c.agent, "_drain_owned_round", drain)
    with pytest.raises(RuntimeError, match="temporary exit failure"):
        await c.h.manager._apply_owned_control(target, action="clear", check_current=checker)
    assert c.h.store.load() is None and c.task.done()
    removed = await c.h.manager._apply_owned_control(target, action="clear", check_current=checker)
    assert removed.goal_id == c.record.goal_id
    assert attempts == [c.owned, c.owned] and not c.h.cancel_calls


@pytest.mark.asyncio
async def test_cached_success_revalidates_current_control_authority(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    allowed = [True]

    def checker():
        if not allowed[0]:
            raise PermissionError("control revoked")

    await c.h.manager._apply_owned_control(target, action="pause", check_current=checker)
    allowed[0] = False
    with pytest.raises(PermissionError, match="control revoked"):
        await c.h.manager._apply_owned_control(target, action="pause", check_current=checker)


@pytest.mark.asyncio
async def test_retry_cannot_change_original_parameters_or_checker(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)

    def checker():
        return None

    await c.h.manager._apply_owned_control(target, action="pause", check_current=checker)
    with pytest.raises(PermissionError, match="retry differs"):
        await c.h.manager._apply_owned_control(target, action="clear", check_current=checker)
    with pytest.raises(PermissionError, match="retry differs"):
        await c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None)


@pytest.mark.asyncio
async def test_async_checker_rejected_before_mutation(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)

    async def checker():
        pass

    with pytest.raises(TypeError, match="synchronously"):
        await c.h.manager._apply_owned_control(target, action="pause", check_current=checker)
    assert c.h.store.load().status is GoalStatus.ACTIVE


async def goal_pipeline(monkeypatch):
    from openjiuwen.harness.goal.manager import GoalManager
    from openjiuwen.harness.goal.store import SessionGoalStore
    from tests.unit_tests.harness.test_owned_round_pipeline import pipeline

    c = await pipeline(monkeypatch)
    c.agent.event_manager._discard_captured_work((c.work,))
    c.goal = GoalManager(
        store=SessionGoalStore(c.session),
        event_manager=c.agent.event_manager,
        control_lock=asyncio.Lock(),
        has_output_stream=lambda: True,
        cancel_active_round=c.agent._cancel_active_round,
        emit_event=lambda _: None,
        notify_work=lambda: None,
    )
    c.agent.goal_manager = c.goal
    c.goal._execution._owner = c.agent
    with execution_origin_scope(c.source):
        c.record = await c.goal.set("actual Goal pipeline")
    c.work = c.agent.event_manager.next_work()
    assert c.work.kind == "goal"
    return c


@pytest.mark.asyncio
async def test_actual_wrapper_after_rail_wait_rejects_cleared_goal(monkeypatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent

    c = await goal_pipeline(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = AgentCallbackContext.fire

    async def fire(ctx, event, *args, **kwargs):
        if event is AgentCallbackEvent.BEFORE_TASK_ITERATION:
            entered.set()
            await release.wait()
        return await original(ctx, event, *args, **kwargs)

    monkeypatch.setattr(AgentCallbackContext, "fire", fire)
    executing = asyncio.create_task(c.agent._execute_round(c.work))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        c.goal._store.clear()
        release.set()
        await asyncio.wait_for(executing, 2)
        assert c.agent.react_agent.invoke_calls == []
    finally:
        release.set()
        await asyncio.gather(executing, return_exceptions=True)
        await c.scheduler.stop()


@pytest.mark.asyncio
async def test_actual_clear_waits_original_scheduler_wrapper_tail(monkeypatch):
    c = await goal_pipeline(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def tail(*_):
        entered.set()
        # Real wrapper cleanup holds until IO acknowledges completion, including
        # cancellation; the synthetic transport controls only this completion.
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    c.scheduler._ensure_session_completion_signal = tail
    executing = asyncio.create_task(c.agent._execute_round(c.work))
    operation = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        owned = c.agent._capture_owned_round(c.source)
        owned._facade_task = executing
        target = c.goal._capture_owned_control(expected_origin=c.source)
        operation = asyncio.create_task(c.goal._apply_owned_control(target, action="clear", check_current=lambda: None))
        for _ in range(15):
            await asyncio.sleep(0)
        assert c.goal.peek() is None and not operation.done()
        assert not owned._scheduler_wrapper.done() and not executing.done()
        release.set()
        removed = await asyncio.wait_for(operation, 2)
        assert removed.goal_id == c.record.goal_id
        assert owned._scheduler_wrapper.done() and executing.done()
    finally:
        release.set()
        await asyncio.gather(*(t for t in (executing, operation) if t is not None), return_exceptions=True)
        await c.scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["store", "slot", "work", "facade"])
async def test_capture_freezes_original_before_source_callback(case, changed):
    c = case
    original = c.source._checker

    def checker():
        original()
        if changed == "store":
            from openjiuwen.harness.goal.store import SessionGoalStore

            c.h.manager._store = SessionGoalStore(c.h.session)
        elif changed == "slot":
            c.h.manager._execution_origin = tuple(list(c.h.manager._execution_origin))
        elif changed == "work":
            from dataclasses import replace

            c.owned.work = replace(c.work).with_execution_origin(c.source)
        else:
            c.owned._facade_task = asyncio.current_task()

    object.__setattr__(c.source, "_checker", checker)
    with pytest.raises(PermissionError):
        c.h.manager._capture_owned_control(expected_origin=c.source)


@pytest.mark.asyncio
async def test_initial_set_requires_actual_original_live_root(case):
    from openjiuwen.harness.schema.interaction import RoundWorkItem

    c = case
    c.h.store.clear()
    c.h.manager._execution_origin = None
    c.owned.work = RoundWorkItem.user(request_id="native-root", inputs={"query": "start"}).with_execution_origin(
        c.source
    )
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    result = await c.h.manager._apply_owned_control(
        target, action="set", objective="new goal", check_current=lambda: None
    )
    assert result.objective == "new goal"
    assert c.h.manager._execution_origin[3] is c.source
    assert not c.task.cancelling()


@pytest.mark.asyncio
async def test_initial_set_without_live_root_cannot_capture(case):
    c = case
    c.h.store.clear()
    c.h.manager._execution_origin = None
    c.agent._active_interaction_round = None
    with pytest.raises(PermissionError):
        c.h.manager._capture_owned_control(expected_origin=c.source)


@pytest.mark.asyncio
async def test_adapter_event_manager_replacement_is_rejected(case):
    from openjiuwen.harness.task_loop.event_manager import EventManager

    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    c.h.manager._execution._event_manager = EventManager()
    with pytest.raises(PermissionError):
        await c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None)
    assert c.h.store.load().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
async def test_late_clear_ack_never_clears_replacement_goal(case, monkeypatch):
    c = case
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.agent._drain_owned_round

    async def drain(owned, **kwargs):
        entered.set()
        await release.wait()
        return await original(owned, **kwargs)

    monkeypatch.setattr(c.agent, "_drain_owned_round", drain)
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    operation = asyncio.create_task(
        c.h.manager._apply_owned_control(target, action="clear", check_current=lambda: None)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with execution_origin_scope(c.source):
            replacement = await c.h.manager.set("replacement")
        release.set()
        with pytest.raises(PermissionError):
            await operation
        assert c.h.store.load().goal_id == replacement.goal_id
        assert c.h.events.has_pending_work()
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["clear", "set"])
async def test_waiting_goal_control_clears_only_original_interruption_and_wakes(case, monkeypatch, action):
    from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

    c = case
    c.parked.set()
    await c.task
    c.owned.waiting_for_input = True
    c.h.session.update_state({INTERRUPTION_KEY: {"original": True}})
    wakes = []
    monkeypatch.setattr(c.agent, "_notify_work", lambda: wakes.append(c.agent._active_interaction_round))
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    params = {"objective": "replacement", "overwrite_confirmed": True} if action == "set" else {}
    await c.h.manager._apply_owned_control(target, action=action, check_current=lambda: None, **params)
    assert c.h.session.get_state(INTERRUPTION_KEY) is None
    assert wakes == [None] and c.agent._active_interaction_round is None
    if action == "set":
        assert c.h.events.next_work().context["goal_id"] == c.h.manager.peek().goal_id


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["capture", "apply"])
async def test_original_stop_phase_is_not_live_goal_control(case, at):
    c = case
    if at == "apply":
        target = c.h.manager._capture_owned_control(expected_origin=c.source)
    assert c.agent._try_transition_interaction_phase(InteractionPhase.TERMINATED)
    with pytest.raises(PermissionError):
        if at == "capture":
            c.h.manager._capture_owned_control(expected_origin=c.source)
        else:
            await c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "pause", "clear"])
async def test_stop_during_commit_prevents_late_control_emission(case, action):
    import asyncio

    c = case
    entered, release = asyncio.Event(), asyncio.Event()

    async def commit():
        entered.set()
        await release.wait()

    c.h.session.commit = commit
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    before = len(c.h.emitted)
    params = {"objective": "replacement", "overwrite_confirmed": True} if action == "set" else {}
    applying = asyncio.create_task(
        c.h.manager._apply_owned_control(target, action=action, check_current=lambda: None, **params)
    )
    await entered.wait()
    c.agent._try_transition_interaction_phase(InteractionPhase.TERMINATED)
    release.set()
    with pytest.raises(PermissionError):
        await applying
    assert len(c.h.emitted) == before


@pytest.mark.asyncio
async def test_clear_ack_after_original_source_ends_requires_real_drain(case, monkeypatch):
    c = case
    original = c.agent._drain_owned_round

    async def draining(owned, **kwargs):
        await original(owned, **kwargs)
        c.live[0] = False  # Original real drain finished before its ACK delivery.

    monkeypatch.setattr(c.agent, "_drain_owned_round", draining)
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    checks = []

    def ack():
        checks.append(True)

    result = await c.h.manager._apply_owned_control(target, action="clear", check_current=lambda: None, check_ack=ack)
    assert result.goal_id == c.record.goal_id and checks
    assert target._run.cancellation.done() and c.task.done()
    target.check_result()
    with pytest.raises(PermissionError):
        target.check(live=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "set"])
async def test_nonclear_controls_never_use_completion_ack(case, monkeypatch, action):
    c = case
    method = "_" + action + "_locked"
    original = getattr(c.h.manager, method)

    async def applied(*args, **kwargs):
        result = await original(*args, **kwargs)
        c.live[0] = False
        return result

    monkeypatch.setattr(c.h.manager, method, applied)
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    checks = []
    params = {"objective": "replacement", "overwrite_confirmed": True} if action == "set" else {}
    with pytest.raises(PermissionError):
        await c.h.manager._apply_owned_control(
            target, action=action, check_current=lambda: None, check_ack=lambda: checks.append(True), **params
        )
    assert not checks


@pytest.mark.asyncio
async def test_queued_clear_has_no_exit_receipt_ack(case, monkeypatch):
    from openjiuwen.harness.schema.interaction import RoundWorkItem

    c = case
    c.owned.work = RoundWorkItem.user(request_id="original", inputs={"query": "ordinary"}).with_execution_origin(
        c.source
    )
    original = c.h.manager._clear_locked

    async def clearing(*args, **kwargs):
        result = await original(*args, **kwargs)
        c.live[0] = False
        return result

    monkeypatch.setattr(c.h.manager, "_clear_locked", clearing)
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    checks = []
    with pytest.raises(PermissionError):
        await c.h.manager._apply_owned_control(
            target, action="clear", check_current=lambda: None, check_ack=lambda: checks.append(True)
        )
    assert target._run.cancellation is None and not checks and not c.task.done()


@pytest.mark.asyncio
async def test_unconfirmed_drain_never_exposes_clear_ack(case, monkeypatch):
    c = case
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.agent._drain_owned_round

    async def draining(owned, **kwargs):
        entered.set()
        await release.wait()
        return await original(owned, **kwargs)

    monkeypatch.setattr(c.agent, "_drain_owned_round", draining)
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    checks = []
    task = asyncio.create_task(
        c.h.manager._apply_owned_control(
            target, action="clear", check_current=lambda: None, check_ack=lambda: checks.append(True)
        )
    )
    try:
        await entered.wait()
        with pytest.raises(PermissionError, match="unconfirmed"):
            target.check_result()
        assert checks == []
        release.set()
        await task
        assert checks
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["record", "slot", "work", "facade"])
async def test_ack_callback_cannot_replace_original_clear_proof(case, change):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)

    def ack():
        if change == "record":
            c.h.store.save(c.record)
        elif change == "slot":
            c.h.manager._execution_origin = tuple(list(c.h.manager._execution_origin))
        elif change == "work":
            from dataclasses import replace

            c.owned.work = replace(c.work).with_execution_origin(c.source)
        else:
            c.owned._facade_task = asyncio.current_task()

    with pytest.raises(PermissionError):
        await c.h.manager._apply_owned_control(target, action="clear", check_current=lambda: None, check_ack=ack)


@pytest.mark.asyncio
async def test_clear_cached_reply_rechecks_same_ack_callback(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    allowed = [True]

    def current():
        return None

    def ack():
        if not allowed[0]:
            raise PermissionError("ACK owner expired")

    await c.h.manager._apply_owned_control(target, action="clear", check_current=current, check_ack=ack)
    with pytest.raises(PermissionError, match="retry differs"):
        await c.h.manager._apply_owned_control(target, action="clear", check_current=current, check_ack=lambda: None)
    allowed[0] = False
    with pytest.raises(PermissionError, match="ACK owner expired"):
        await c.h.manager._apply_owned_control(target, action="clear", check_current=current, check_ack=ack)


@pytest.mark.asyncio
async def test_result_check_before_apply_is_not_authority(case):
    target = case.h.manager._capture_owned_control(expected_origin=case.source)
    with pytest.raises(PermissionError, match="completed mutation"):
        target.check_result()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["task", "applied", "result_object", "result_value", "result_facts"])
async def test_ack_callback_cannot_change_original_operation_or_result(case, change):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)

    def ack():
        p = target._run
        if change == "task":
            p.task = c.task
        elif change == "applied":
            p.applied = False
        elif change == "result_object":
            p.result = p.result.copy_for_response()
        elif change == "result_value":
            p.result.objective = "replacement"
        else:
            p.result_facts["objective"] = "replacement"

    with pytest.raises(PermissionError, match="result changed"):
        await c.h.manager._apply_owned_control(target, action="clear", check_current=lambda: None, check_ack=ack)


@pytest.mark.asyncio
async def test_owned_control_callback_cannot_retarget_store_same_sid(case):
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.harness.goal.store import SessionGoalStore

    c = case
    original_store = c.h.store
    original_session = original_store._session
    replacement = create_agent_session(session_id=original_session.get_session_id(), card=c.agent.card)
    await replacement._inner.checkpointer().pre_agent_execute(replacement._inner, None)
    replacement_store = SessionGoalStore(replacement)
    replacement_store.save(original_store.load())
    selector = c.h.manager._capture_owned_control(expected_origin=c.source)

    def checker():
        original_store._session = replacement

    try:
        with pytest.raises(PermissionError):
            await c.h.manager._apply_owned_control(selector, action="pause", check_current=checker)
        assert replacement_store.load().status is GoalStatus.ACTIVE
    finally:
        original_store._session = original_session


@pytest.mark.asyncio
async def test_owned_control_capture_source_checker_cannot_replace_backing(case):
    c = case
    from openjiuwen.harness.goal.store import SessionGoalStore

    original = c.h.store._session
    replacement = type(original)()
    SessionGoalStore(replacement).save(c.h.store.load())
    checker = c.source._checker

    def replace_backing():
        c.h.store._session = replacement

    object.__setattr__(c.source, "_checker", replace_backing)
    try:
        with pytest.raises(PermissionError):
            c.h.manager._capture_owned_control(expected_origin=c.source)
    finally:
        object.__setattr__(c.source, "_checker", checker)
        c.h.store._session = original


@pytest.mark.asyncio
async def test_owned_control_commit_await_rechecks_backing(case):
    c = case
    target = c.h.manager._capture_owned_control(expected_origin=c.source)
    original = c.h.store._session
    replacement = type(original)()

    async def commit():
        c.h.store._session = replacement

    c.h.session.commit = commit
    try:
        with pytest.raises(PermissionError):
            await c.h.manager._apply_owned_control(target, action="pause", check_current=lambda: None)
    finally:
        c.h.store._session = original
