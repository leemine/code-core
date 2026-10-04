"""Actual original Goal store, locks, queue and sole lease with synthetic authority."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.goal.readmission import _NativeGoalReadmissionPlan
from openjiuwen.harness.goal.schema import GoalStatus
from openjiuwen.harness.schema.interaction import InteractionPhase, RoundWorkItem

from .test_goal_manager import ManagerHarness


@pytest_asyncio.fixture
async def idle():
    h = ManagerHarness(output_attached=False)
    record = await h.manager.set("retained objective", max_attempts=7, token_budget=900)
    # Real newly constructed Manager restores persisted data, not live authority.
    from openjiuwen.harness.goal.manager import GoalManager

    h.manager = GoalManager(store=h.store, execution=h.manager._execution, control_lock=h.manager._control_lock)
    agent = DeepAgent(AgentCard(name="readmission-test"))
    agent._interaction_session = h.session
    agent._interaction_started = True
    agent._try_transition_interaction_phase(InteractionPhase.IDLE)
    agent._event_manager = h.events
    agent._interaction_control_lock = h.manager._control_lock
    agent.goal_manager = h.manager
    h.manager._execution._owner = agent
    h.manager._execution._has_output_stream = agent._interaction_output.has_consumer
    live = [True]

    def check():
        if not live[0]:
            raise PermissionError("new producer ended")

    origin = ExecutionOrigin(object(), _checker=check)
    proof_calls = []

    def proof():
        proof_calls.append(True)

    def plan(action="attach", checker=check):
        selector = h.manager._capture_idle_readmission(expected_record=h.store.load())
        return _NativeGoalReadmissionPlan(selector, action, checker)

    async def apply(value):
        return await agent._attach_output_for_goal_readmission(value, new_origin=origin, check_previous=proof)

    yield SimpleNamespace(**locals())
    await agent._interaction_output.shutdown()


@pytest.mark.asyncio
async def test_active_attach_keeps_record_and_uses_fresh_source(idle):
    c = idle
    before = c.h.store.load().to_dict()
    p = c.plan()
    stream = await c.apply(p)
    assert stream is not None and c.h.store.load().to_dict() == before
    assert c.h.manager._execution_origin[3] is c.origin
    work = c.h.events.next_work()
    assert work.execution_origin is c.origin and work.context["revision"] == c.record.revision
    assert p.result().goal_id == c.record.goal_id
    assert c.proof_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [GoalStatus.PAUSED, GoalStatus.BLOCKED])
async def test_idle_resume_bumps_once_and_preserves_goal(idle, status):
    c = idle
    old = c.h.store.load()
    old.status = status
    old.attempt_count = 3
    c.h.store.save(old)
    p = c.plan("resume")
    stream = await c.apply(p)
    result = p.result()
    assert result.goal_id == old.goal_id and result.revision == old.revision + 1
    assert result.attempt_count == 3 and result.max_attempts == 7 and result.token_budget == 900
    assert await c.apply(p) is stream
    assert c.h.store.load().revision == old.revision + 1


@pytest.mark.asyncio
async def test_occupied_lease_does_not_rebind(idle):
    c = idle
    existing = await c.agent._attach_output_locked()
    before = c.h.manager._execution_origin
    with pytest.raises(PermissionError, match="consumer"):
        await c.apply(c.plan())
    assert c.agent._interaction_output.current_token() == existing._lease.token
    assert c.h.manager._execution_origin is before
    assert c.h.events.next_work() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["record", "store", "slot", "session", "events", "phase"])
async def test_reentrant_checker_cannot_retarget(idle, change):
    c = idle

    def checker():
        if change == "record":
            rec = c.h.store.load()
            rec.objective = "other"
            c.h.store.save(rec)
        elif change == "store":
            c.h.manager._store = ManagerHarness().store
        elif change == "slot":
            c.h.manager._execution_origin = ("other", "goal", 2, c.origin)
        elif change == "session":
            c.agent._interaction_session = object()
        elif change == "events":
            c.agent._event_manager = ManagerHarness().events
        else:
            c.agent._try_transition_interaction_phase(InteractionPhase.TERMINATED)

    with pytest.raises(PermissionError):
        await c.apply(c.plan(checker=checker))
    assert not c.agent._interaction_output.has_consumer()
    assert c.h.events.next_work() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["queued", "dequeued", "active", "round", "emitter"])
async def test_existing_execution_inventory_blocks(idle, pending):
    c = idle
    parked = asyncio.create_task(asyncio.Event().wait())
    work = RoundWorkItem.user(request_id="old-request", inputs={"query": "old"})
    if pending == "queued":
        c.h.events.push_user(work)
    elif pending == "dequeued":
        c.h.events.push_user(work)
        c.h.events.next_work()
    elif pending == "active":
        c.h.events.mark_started(work)
    elif pending == "round":
        c.agent._interaction_round_task = parked
    else:
        c.agent._interaction_emit_tasks.add(parked)
    try:
        with pytest.raises(PermissionError, match="quiescent"):
            await c.apply(c.plan())
        assert not c.agent._interaction_output.has_consumer()
        assert not parked.cancelled()
    finally:
        parked.cancel()
        await asyncio.gather(parked, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "shutdown", "record"])
async def test_commit_wait_rechecks_before_queue_or_emit(idle, change):
    c = idle
    await c.h.manager.pause()
    p = c.plan("resume")
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.h.store.commit

    async def commit():
        entered.set()
        await release.wait()
        await original()

    c.h.store.commit = commit
    running = asyncio.create_task(c.apply(p))
    try:
        await entered.wait()
        if change == "revoke":
            c.live[0] = False
        elif change == "shutdown":
            c.agent._try_transition_interaction_phase(InteractionPhase.TERMINATED)
        else:
            record = c.h.store.load()
            record.objective = "replacement"
            c.h.store.save(record)
        release.set()
        with pytest.raises(PermissionError):
            await running
        assert not c.agent._interaction_output.has_consumer()
        assert c.h.events.next_work() is None and c.h.emitted == []
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_on_lock_cannot_use_stale_selector(idle):
    c = idle
    p = c.plan()
    await c.h.manager._control_lock.acquire()
    running = asyncio.create_task(c.apply(p))
    await asyncio.sleep(0)
    changed = c.h.store.load()
    changed.revision += 1
    c.h.store.save(changed)
    c.h.manager._control_lock.release()
    with pytest.raises(PermissionError):
        await running
    assert not c.agent._interaction_output.has_consumer()


@pytest.mark.asyncio
async def test_callback_failure_after_queue_discards_only_own_work(idle):
    c = idle

    def emit(_):
        raise PermissionError("sink revoked")

    c.h.manager._execution._emit_event = emit
    with pytest.raises(PermissionError, match="sink revoked"):
        await c.apply(c.plan())
    assert not c.agent._interaction_output.has_consumer()
    assert c.h.events.next_work() is None


@pytest.mark.asyncio
async def test_cancelled_waiter_rejoins_same_operation_without_second_resume(idle):
    c = idle
    await c.h.manager.pause()
    p = c.plan("resume")
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.h.store.commit

    async def commit():
        entered.set()
        await release.wait()
        await original()

    c.h.store.commit = commit
    running = asyncio.create_task(c.apply(p))
    await entered.wait()
    operation = p._run.task
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not operation.done()
    release.set()
    stream = await c.apply(p)
    assert p._run.task is operation and p._run.stream is stream
    assert p.result().revision == c.record.revision + 1


@pytest.mark.asyncio
async def test_legacy_persisted_followups_are_not_cold_idle_proof(idle):
    c = idle
    state = c.agent.load_state(c.h.session)
    state.pending_follow_ups.append("unknown old text")
    c.agent.save_state(c.h.session, state)
    with pytest.raises(PermissionError, match="ownership is unknown"):
        await c.apply(c.plan())
    assert not c.agent.has_output_stream()


@pytest.mark.asyncio
async def test_result_checker_cannot_replace_original_reply(idle):
    c = idle
    armed = [False]
    p = None

    def checker():
        if armed[0]:
            p._run.result.objective = "wrong result"

    p = c.plan(checker=checker)
    await c.apply(p)
    armed[0] = True
    with pytest.raises(PermissionError, match="result changed"):
        p.result()


@pytest.mark.asyncio
async def test_commit_failure_is_not_replayed_as_another_mutation(idle):
    c = idle
    await c.h.manager.pause()
    p = c.plan("resume")
    calls = []

    async def commit():
        calls.append(True)
        raise OSError("synthetic unknown disk result")

    c.h.store.commit = commit
    for _ in range(2):
        with pytest.raises(OSError, match="unknown disk"):
            await c.apply(p)
    assert len(calls) == 1
    assert c.h.store.load().revision == c.record.revision + 1
    assert not c.agent.has_output_stream() and not c.h.events.has_pending_work()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cold", "other", "same_host_value", "expired"])
async def test_ordinary_managed_attach_cannot_revive_old_active_goal(idle, source):
    c = idle
    old = c.h.store.load()
    origin = c.origin
    if source == "other":
        c.h.manager._execution_origin = (old.session_id, old.goal_id, old.revision, ExecutionOrigin(object()))
    elif source == "same_host_value":
        c.h.manager._execution_origin = (old.session_id, old.goal_id, old.revision, ExecutionOrigin(origin.host_value))
    elif source == "expired":
        c.h.manager._execution_origin = (old.session_id, old.goal_id, old.revision, origin)
        c.live[0] = False
    with pytest.raises(PermissionError):
        await c.agent._attach_output_for_origin(origin)
    assert not c.agent.has_output_stream() and not c.h.events.has_pending_work()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["absent", "paused", "legacy_active"])
async def test_existing_non_active_and_legacy_attach_remain_available(idle, state):
    c = idle
    if state == "absent":
        c.h.store.clear()
    elif state == "paused":
        record = c.h.store.load()
        record.status = GoalStatus.PAUSED
        c.h.store.save(record)
    if state == "legacy_active":
        stream = await c.agent.attach_output()
        assert c.h.events.next_work().execution_origin is None
    else:
        stream = await c.agent._attach_output_for_origin(c.origin)
        assert not c.h.events.has_pending_work()
    assert stream is not None
