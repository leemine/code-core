"""Idle controls use actual Native/GoalManager/exit graphs, synthetic model IO only."""
import pytest

from openjiuwen.harness.goal.schema import GoalStatus
from tests.unit_tests.harness_providers.test_native_goal_readmission import setup, submit


@pytest.mark.asyncio
@pytest.mark.parametrize('hot', [False, True])
@pytest.mark.parametrize('action', ['pause', 'clear'])
async def test_idle_control_without_new_turn_output_or_model(monkeypatch, hot, action):
    c = await setup(monkeypatch)
    try:
        previous = (await submit(c, 'A'))[0] if hot else None
        manager = c.agent.goal_manager
        record = manager.peek()
        old_slot, first, output = manager._execution_origin, c.harness._first_managed_turn, c.agent._interaction_output
        old_events, old_calls = tuple(c.case.events), list(c.agent.react_agent.invoke_calls)
        cap = c.harness._capture_idle_goal_control(
            expected_record=record, previous_turn=previous, check_current=lambda: None)
        result = await cap.apply(action=action)
        cap.check_result()
        assert result.goal_id == record.goal_id and result.revision == record.revision
        if action == 'clear':
            assert manager.peek() is None and manager._execution_origin is None
        else:
            assert manager.peek().status is GoalStatus.PAUSED
            assert manager._execution_origin is old_slot
        assert c.harness._first_managed_turn is first
        assert not c.harness._pending and c.harness.active_turn is None
        assert c.agent._interaction_output is output and not c.agent.has_output_stream()
        assert tuple(c.case.events) == old_events and c.agent.react_agent.invoke_calls == old_calls
        assert not c.agent._interaction_emit_tasks
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['store', 'backing', 'manager', 'source', 'output', 'command_lock'])
async def test_capture_reentrant_controller_cannot_retarget(monkeypatch, change):
    import asyncio

    from openjiuwen.harness.goal.store import SessionGoalStore
    from tests.unit_tests.harness.test_deep_agent_event_executor import FakeSession

    c = await setup(monkeypatch)
    manager = c.agent.goal_manager
    original = manager.peek().to_dict()
    touched = []
    patches = pytest.MonkeyPatch()

    def checker():
        if touched:
            return
        touched.append(True)
        if change == 'store':
            patches.setattr(manager, '_store', SessionGoalStore(c.case.session))
        elif change == 'backing':
            replacement = FakeSession(c.case.session.get_session_id())
            SessionGoalStore(replacement).save(manager.peek())
            patches.setattr(manager._store, '_session', replacement)
        elif change == 'manager':
            patches.setattr(c.agent, 'goal_manager', object())
        elif change == 'source':
            patches.setattr(manager, '_execution_origin', ('sid', 'goal', 1, c.origins['A']))
        elif change == 'output':
            patches.setattr(c.agent, '_interaction_output', object())
        else:
            patches.setattr(c.harness, '_command_lock', asyncio.Lock())

    try:
        with pytest.raises((PermissionError, RuntimeError)):
            c.harness._capture_idle_goal_control(
                expected_record=manager.peek(), previous_turn=None, check_current=checker)
        assert SessionGoalStore(c.case.session).load().to_dict() == original
    finally:
        patches.undo()
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing', 'source', 'confirmed', 'cleanup', 'round_handle', 'execution_done'])
async def test_hot_idle_rejects_unknown_or_replaced_original_exit(monkeypatch, change):
    import asyncio

    c = await setup(monkeypatch)
    def restore():
        pass
    try:
        previous, _ = await submit(c, 'A')
        manager = c.agent.goal_manager
        record = manager.peek()
        if change == 'missing':
            previous = None
        elif change == 'source':
            old = previous._origin
            previous._origin = c.origins['A']  # same host value is insufficient
            def restore():
                previous._origin = old
        elif change == 'confirmed':
            old = previous._exit.confirmed
            previous._exit.confirmed = asyncio.get_running_loop().create_future()
            def restore():
                previous._exit.confirmed = old
        elif change == 'cleanup':
            old = previous._exit.cleanup
            pending = asyncio.create_task(asyncio.Event().wait())
            previous._exit.cleanup = pending
            def restore():
                pending.cancel()
                previous._exit.cleanup = old
        elif change == 'round_handle':
            old = previous._exit.round_handle
            previous._exit.round_handle = None
            def restore():
                previous._exit.round_handle = old
        else:
            previous._execution_done.clear()
            def restore():
                previous._execution_done.set()
        with pytest.raises((PermissionError, RuntimeError)):
            c.harness._capture_idle_goal_control(
                expected_record=record, previous_turn=previous, check_current=lambda: None)
        assert manager.peek().to_dict() == record.to_dict()
    finally:
        restore()
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['pause', 'clear'])
async def test_hot_idle_never_consults_expired_original_execution_checker(monkeypatch, action):
    c = await setup(monkeypatch)
    try:
        previous, _ = await submit(c, 'A')
        def revoked():
            raise PermissionError('old execution ended')
        object.__setattr__(c.origins['A'], '_checker', revoked)
        cap = c.harness._capture_idle_goal_control(
            expected_record=c.agent.goal_manager.peek(), previous_turn=previous, check_current=lambda: None)
        await cap.apply(action=action)
        cap.check_result()
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['controller', 'record', 'stopping', 'backing'])
async def test_idle_commit_wait_revalidates_before_ack(monkeypatch, change):
    import asyncio

    from openjiuwen.harness.goal.store import SessionGoalStore
    from tests.unit_tests.harness.test_deep_agent_event_executor import FakeSession

    c = await setup(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    live = [True]
    running = None
    manager = c.agent.goal_manager
    store = manager._store
    original_commit = store.commit
    def checker():
        if not live[0]:
            raise PermissionError('controller revoked')
    async def delayed():
        await original_commit()
        entered.set()
        await release.wait()
    monkeypatch.setattr(store, 'commit', delayed)
    try:
        cap = c.harness._capture_idle_goal_control(
            expected_record=manager.peek(), previous_turn=None, check_current=checker)
        running = asyncio.create_task(cap.apply(action='pause'))
        await asyncio.wait_for(entered.wait(), 1)
        if change == 'controller':
            live[0] = False
        elif change == 'record':
            record = store.load()
            record.touch(bump_revision=True)
            store.save(record)
        elif change == 'stopping':
            c.harness._stopping = True
        else:
            other = FakeSession(c.case.session.get_session_id())
            SessionGoalStore(other).save(store.load())
            store._session = other
        release.set()
        with pytest.raises((PermissionError, RuntimeError)):
            await running
        assert c.agent.react_agent.invoke_calls == [] and not c.agent._interaction_emit_tasks
    finally:
        release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        c.harness._stopping = False
        store._session = c.case.session
        await c.harness.stop()


@pytest.mark.asyncio
async def test_caller_cancel_keeps_same_commit_task_and_retry(monkeypatch):
    import asyncio

    c = await setup(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def delayed():
        calls.append(True)
        entered.set()
        await release.wait()
    monkeypatch.setattr(c.agent.goal_manager._store, 'commit', delayed)
    try:
        cap = c.harness._capture_idle_goal_control(
            expected_record=c.agent.goal_manager.peek(), previous_turn=None, check_current=lambda: None)
        caller = asyncio.create_task(cap.apply(action='clear'))
        await asyncio.wait_for(entered.wait(), 1)
        operation = cap._run.task
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not operation.done() and not operation.cancelling()
        release.set()
        result = await cap.apply(action='clear')
        assert cap._run.task is operation and len(calls) == 1 and result.goal_id == c.record.goal_id
        with pytest.raises(PermissionError):
            await cap.apply(action='pause')
    finally:
        release.set()
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['record', 'result', 'task', 'controller'])
async def test_idle_final_reply_cannot_be_rebound(monkeypatch, change):
    c = await setup(monkeypatch)
    change_now = [False]
    cap = None
    def checker():
        if not change_now[0]:
            return
        if change == 'controller':
            raise PermissionError('controller revoked')
        if change == 'record':
            record = c.agent.goal_manager.peek()
            record.touch(bump_revision=True)
            c.agent.goal_manager._store.save(record)
        elif change == 'result':
            cap._run.result.objective = 'changed reply'
        else:
            cap._run.task = object()
    try:
        cap = c.harness._capture_idle_goal_control(
            expected_record=c.agent.goal_manager.peek(), previous_turn=None, check_current=checker)
        await cap.apply(action='pause')
        change_now[0] = True
        with pytest.raises(PermissionError):
            cap.check_result()
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('inventory', ['queue', 'dequeued', 'emit', 'scheduler', 'followup'])
async def test_idle_cold_requires_all_original_inventories_empty(monkeypatch, inventory):
    import asyncio

    c = await setup(monkeypatch)
    task = None
    try:
        if inventory == 'queue':
            c.agent.event_manager._user_queue.append(c.case.work)
        elif inventory == 'dequeued':
            c.agent.event_manager._dequeued = c.case.work
        elif inventory in {'emit', 'scheduler'}:
            task = asyncio.create_task(asyncio.Event().wait())
            if inventory == 'emit':
                c.agent._interaction_emit_tasks.add(task)
            else:
                c.case.scheduler._owned_execution_tasks['unknown'] = task
        else:
            state = c.agent.load_state(c.case.session)
            state.pending_follow_ups = ['unknown work']
            c.agent.save_state(c.case.session, state)
        with pytest.raises(PermissionError):
            c.harness._capture_idle_goal_control(
                expected_record=c.agent.goal_manager.peek(), previous_turn=None, check_current=lambda: None)
    finally:
        c.agent.event_manager._user_queue.clear()
        c.agent.event_manager._dequeued = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await c.harness.stop()


@pytest.mark.asyncio
async def test_first_admission_invalidates_captured_cold_control(monkeypatch):
    c = await setup(monkeypatch)
    try:
        cap = c.harness._capture_idle_goal_control(
            expected_record=c.agent.goal_manager.peek(), previous_turn=None, check_current=lambda: None)
        await submit(c, 'A')
        with pytest.raises((PermissionError, RuntimeError)):
            await cap.apply(action='clear')
        assert c.agent.goal_manager.peek() is not None
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
async def test_missing_goal_and_legacy_native_cannot_obtain_idle_mutation_cap(monkeypatch):
    c = await setup(monkeypatch)
    try:
        with pytest.raises(TypeError):
            c.harness._capture_idle_goal_control(
                expected_record=None, previous_turn=None, check_current=lambda: None)
        object.__setattr__(c.harness._host_hooks, 'capture_execution_origin', None)
        with pytest.raises(RuntimeError):
            c.harness._capture_idle_goal_control(
                expected_record=c.agent.goal_manager.peek(), previous_turn=None, check_current=lambda: None)
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [GoalStatus.PAUSED, GoalStatus.BLOCKED, GoalStatus.COMPLETED])
async def test_idle_pause_noop_preserves_nonactive_record(monkeypatch, status):
    c = await setup(monkeypatch)
    try:
        record = c.agent.goal_manager.peek()
        record.status = status
        c.agent.goal_manager._store.save(record)
        cap = c.harness._capture_idle_goal_control(
            expected_record=record, previous_turn=None, check_current=lambda: None)
        assert (await cap.apply(action='pause')).to_dict() == record.to_dict()
        assert c.agent.goal_manager.peek().to_dict() == record.to_dict()
        assert c.agent.react_agent.invoke_calls == []
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('point', ['before_apply', 'after_apply'])
async def test_exact_harness_cycle_is_part_of_controller_target(monkeypatch, point):
    c = await setup(monkeypatch)
    original = c.harness._event_buffer
    try:
        cap = c.harness._capture_idle_goal_control(
            expected_record=c.agent.goal_manager.peek(), previous_turn=None, check_current=lambda: None)
        if point == 'after_apply':
            await cap.apply(action='pause')
        c.harness._event_buffer = object()
        with pytest.raises(RuntimeError):
            if point == 'before_apply':
                await cap.apply(action='pause')
            else:
                cap.check_result()
    finally:
        c.harness._event_buffer = original
        await c.harness.stop()


@pytest.mark.asyncio
async def test_controller_must_be_synchronous_and_cannot_grant_new_source(monkeypatch):
    c = await setup(monkeypatch)
    async def async_checker():
        return None
    try:
        with pytest.raises(TypeError, match='synchronously'):
            c.harness._capture_idle_goal_control(
                expected_record=c.agent.goal_manager.peek(), previous_turn=None, check_current=async_checker)
        assert c.agent.goal_manager._execution_origin is None
    finally:
        await c.harness.stop()


@pytest.mark.asyncio
async def test_waiting_for_original_control_lock_rechecks_controller_before_write(monkeypatch):
    import asyncio

    c = await setup(monkeypatch)
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError('controller revoked')
    lock = c.agent.goal_manager._control_lock
    running = None
    try:
        original = c.agent.goal_manager.peek()
        cap = c.harness._capture_idle_goal_control(
            expected_record=original, previous_turn=None, check_current=check)
        await lock.acquire()
        running = asyncio.create_task(cap.apply(action='clear'))
        # The operation owns command/send but cannot mutate until the existing
        # control lock becomes available.
        for _ in range(20):
            if c.harness._command_lock.locked():
                break
            await asyncio.sleep(0)
        assert c.harness._command_lock.locked()
        live[0] = False
        lock.release()
        with pytest.raises(PermissionError):
            await running
        assert c.agent.goal_manager.peek().to_dict() == original.to_dict()
    finally:
        if lock.locked():
            lock.release()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await c.harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['pause', 'clear'])
async def test_idle_never_emits_even_if_original_output_consumer_still_exists(monkeypatch, action):
    c = await setup(monkeypatch)
    emitted = []
    try:
        manager = c.agent.goal_manager
        monkeypatch.setattr(manager._execution, '_has_output_stream', lambda: True)
        monkeypatch.setattr(manager._execution, '_emit_event', emitted.append)
        output = c.agent._interaction_output
        cap = c.harness._capture_idle_goal_control(
            expected_record=manager.peek(), previous_turn=None, check_current=lambda: None)
        await cap.apply(action=action)
        assert not emitted and not c.agent._interaction_emit_tasks
        assert c.agent._interaction_output is output
    finally:
        await c.harness.stop()
