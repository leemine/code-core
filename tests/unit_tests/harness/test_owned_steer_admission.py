"""Actual Round/controller queues with synthetic authority and facade tasks."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope
from openjiuwen.harness.schema.interaction import (
    ActiveInteractionRound,
    InputDispatchMode,
    RoundWorkItem,
    SendInputRequest,
)
from openjiuwen.harness.task_loop.loop_queues import LoopQueues
from tests.unit_tests.harness import test_deep_agent_round_origin as fixtures

agent = fixtures.agent


@pytest_asyncio.fixture
async def owned(agent):
    queues = LoopQueues()
    agent._loop_controller = fixtures.controller(queues)
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    live = [True]
    def check():
        if not live[0]:
            raise PermissionError('original parent ended')
    source = ExecutionOrigin(object(), _checker=check)
    work = RoundWorkItem.user(request_id='parent', inputs={'query': 'parent'}).with_execution_origin(source)
    original = ActiveInteractionRound(work, 'task', _session=agent._interaction_session,
                                     _controller=agent.loop_controller, _facade_task=task)
    agent._active_interaction_round = original
    value = SimpleNamespace(agent=agent, queues=queues, original=original, source=source,
                            task=task, release=release, live=live)
    yield value
    release.set()
    await task


def request():
    return SendInputRequest('input', {'query': 'supplemental'}, mode=InputDispatchMode.STEER)


def assert_empty(case):
    assert case.queues.drain_steering() == []
    assert case.agent._event_manager.next_work() is None


@pytest.mark.asyncio
async def test_owned_steer_uses_original_controller_and_source(owned):
    with execution_origin_scope(owned.source):
        await owned.agent._send_owned_steer(owned.original, request(), check_current=lambda: None)
    with execution_origin_scope(owned.source):
        assert owned.queues.drain_steering() == ['supplemental']
    assert owned.agent._active_interaction_round is owned.original
    assert owned.agent._event_manager.next_work() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('lock_name', ['_interaction_send_lock', '_interaction_control_lock'])
@pytest.mark.parametrize('change', ['credential', 'parent', 'round', 'waiting', 'query', 'source_scope'])
async def test_lock_wait_rechecks_input_and_exact_parent_without_fallback(owned, lock_name, change):
    lock = getattr(owned.agent, lock_name)
    await lock.acquire()
    allowed = [True]
    def check():
        if not allowed[0]:
            raise PermissionError('temporary credential ended')
    submitted = request()
    with execution_origin_scope(owned.source):
        pending = asyncio.create_task(owned.agent._send_owned_steer(owned.original, submitted, check_current=check))
        await asyncio.sleep(0)
        if change == 'credential':
            allowed[0] = False
        elif change == 'parent':
            owned.live[0] = False
        elif change == 'round':
            owned.agent._active_interaction_round = replace(owned.original)
        elif change == 'waiting':
            owned.original.waiting_for_input = True
        elif change == 'query':
            submitted.inputs['query'] = 'different'
        if change != 'source_scope':
            lock.release()
            with pytest.raises(Exception, match='ended|ownership changed|no longer accepts|input changed'):
                await pending
    if change == 'source_scope':
        lock.release()
        with pytest.raises(RuntimeError, match='scope expired'):
            await pending
    assert_empty(owned)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['idle', 'done', 'waiting', 'controller', 'foreign', 'none'])
async def test_owned_steer_never_creates_a_replacement_round(owned, change):
    source = owned.source
    if change == 'idle':
        owned.agent._active_interaction_round = None
    elif change == 'done':
        owned.release.set()
        await owned.task
    elif change == 'waiting':
        owned.original.waiting_for_input = True
    elif change == 'controller':
        owned.agent._loop_controller = fixtures.controller(LoopQueues())
    elif change == 'foreign':
        source = ExecutionOrigin(object())
    else:
        source = None
    with execution_origin_scope(source):
        with pytest.raises(Exception, match='ownership changed|no longer accepts|requires its original'):
            await owned.agent._send_owned_steer(owned.original, request(), check_current=lambda: None)
    assert_empty(owned)


@pytest.mark.asyncio
@pytest.mark.parametrize('callback', ['non_none', 'async'])
async def test_owned_steer_rejects_nonsynchronous_authority(owned, callback):
    async def asynchronous():
        return None
    checker = asynchronous if callback == 'async' else lambda: True
    with execution_origin_scope(owned.source):
        with pytest.raises(TypeError, match='synchronously'):
            await owned.agent._send_owned_steer(owned.original, request(), check_current=checker)
    assert_empty(owned)


@pytest.mark.asyncio
async def test_existing_plain_send_keeps_idle_followup_behavior(owned):
    owned.agent._active_interaction_round = None
    with execution_origin_scope(owned.source):
        await owned.agent.send_input(request())
    assert owned.agent._event_manager.next_work().execution_origin is owned.source


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['work', 'facade', 'controller', 'session'])
async def test_original_round_fields_are_pinned_before_lock_wait(owned, field):
    lock = owned.agent._interaction_send_lock
    await lock.acquire()
    other_task = asyncio.create_task(owned.release.wait())
    try:
        with execution_origin_scope(owned.source):
            pending = asyncio.create_task(owned.agent._send_owned_steer(
                owned.original, request(), check_current=lambda: None))
            await asyncio.sleep(0)
            if field == 'work':
                owned.original.work = replace(owned.original.work, request_id='replacement')
            elif field == 'facade':
                owned.original._facade_task = other_task
            elif field == 'controller':
                other = fixtures.controller(LoopQueues())
                owned.original._controller = owned.agent._loop_controller = other
            else:
                owned.original._session = owned.agent._interaction_session = SimpleNamespace()
            lock.release()
            with pytest.raises(Exception, match='changed|no longer accepts'):
                await pending
        assert_empty(owned)
    finally:
        owned.release.set()
        await other_task


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['waiting', 'query', 'round'])
async def test_last_synchronous_checker_cannot_change_final_admission_facts(owned, mutation):
    submitted = request()
    def check():
        if owned.agent._interaction_control_lock.locked():
            if mutation == 'waiting':
                owned.original.waiting_for_input = True
            elif mutation == 'query':
                submitted.inputs['query'] = 'authorized different text'
            else:
                owned.agent._active_interaction_round = replace(owned.original)
    with execution_origin_scope(owned.source):
        with pytest.raises(Exception, match='changed|no longer accepts'):
            await owned.agent._send_owned_steer(owned.original, submitted, check_current=check)
    assert_empty(owned)


@pytest.mark.asyncio
async def test_already_cancelling_original_facade_rejects_new_steer(owned):
    other_task = asyncio.create_task(owned.release.wait())
    owned.original._facade_task = other_task
    other_task.cancel()
    try:
        with execution_origin_scope(owned.source):
            with pytest.raises(Exception, match='changed|no longer accepts'):
                await owned.agent._send_owned_steer(owned.original, request(), check_current=lambda: None)
        assert_empty(owned)
    finally:
        await asyncio.gather(other_task, return_exceptions=True)
