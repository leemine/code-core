"""Original kernel ingress -> EventBus -> real NativeHarness/task scheduler.

Only model execution and the scheduler's scan decision are fixtures. These tests
prove the wake event's source, never authority for rows a database scan returns.
"""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.agent.coordination import (
    CoordinationKernel,
    EventBus,
    InnerEventMessage,
    InnerEventType,
)
from openjiuwen.agent_teams.context import get_session_id
from openjiuwen.agent_teams.harness import HarnessState
from openjiuwen.agent_teams.messager.base import MessagerTransportConfig
from openjiuwen.agent_teams.messager.inprocess import InProcessMessager, _Bus
from openjiuwen.agent_teams.schema.events import EventMessage, TeamTopic
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.core.controller.schema import (
    ExecutionOrigin,
    current_execution_origin,
    execution_origin_scope,
)
from tests.unit_tests.agent_teams.harness import test_execution_origin as origin_fixtures
from tests.unit_tests.agent_teams.harness.fixtures import wait_for_state, wait_invoke_running

native = origin_fixtures.native


@pytest.mark.asyncio
@pytest.mark.parametrize("sourced,restore", [(True, False), (False, False), (True, True)])
async def test_user_input_keeps_only_its_original_source(native, sourced, restore):
    harness, _, seen = native
    bus = EventBus(role=TeamRole.HUMAN_AGENT)
    kernel = CoordinationKernel(SimpleNamespace())
    kernel._event_bus = bus
    original = ExecutionOrigin(object()) if sourced else None
    with execution_origin_scope(original):
        await kernel.enqueue_user_input({"query": "original-input"})
    if restore:
        queued = bus._event_queue.get_nowait()
        bus._event_queue.task_done()
        # Loading a saved event inside another live scope cannot authorize it.
        with execution_origin_scope(ExecutionOrigin(object())):
            await bus.enqueue(InnerEventMessage.model_validate_json(queued.model_dump_json()))
    done = asyncio.Event()
    callback_sources = []

    async def receive(event):
        callback_sources.append(current_execution_origin())
        await harness.send(event.payload["content"])
        done.set()

    unrelated = ExecutionOrigin(object())
    with execution_origin_scope(unrelated):
        await bus.start(wake_callback=receive)
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert await wait_for_state(harness, HarnessState.IDLE)
        expected = None if restore else original
        assert callback_sources == [expected]
        assert [item[0] for item in seen] == [expected]
        assert current_execution_origin() is None
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_mixed_kernel_input_does_not_join_existing_native_source(native):
    harness, model, seen = native
    model.sleep_seconds = 0.2
    original = ExecutionOrigin(object())
    await harness.send("first", origin=original)
    await wait_invoke_running(model)
    bus = EventBus(role=TeamRole.HUMAN_AGENT)
    kernel = CoordinationKernel(SimpleNamespace())
    kernel._event_bus = bus
    done = asyncio.Event()
    rejected = []

    async def receive(event):
        try:
            await harness.send(event.payload["content"])
        except ValueError as error:
            rejected.append(str(error))
        finally:
            done.set()

    await bus.start(wake_callback=receive)
    try:
        with execution_origin_scope(ExecutionOrigin(object())):
            await kernel.enqueue_user_input("different")
        await asyncio.wait_for(done.wait(), 2)
        assert rejected == ["mixed execution origins"]
        assert await wait_for_state(harness, HarnessState.IDLE)
        assert [item[0] for item in seen] == [original]
    finally:
        await bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
async def test_self_task_scan_keeps_event_source_not_publisher_scope(native, restore):
    harness, _, seen = native
    bus = EventBus(role=TeamRole.HUMAN_AGENT)
    messager = InProcessMessager(config=MessagerTransportConfig(node_id="leader"))
    messager._bus = _Bus()  # Private original inprocess implementation, no global subscribers.
    host = SimpleNamespace(
        infra=SimpleNamespace(messager=messager),
        member_name="leader",
        role=TeamRole.LEADER,
        state=SimpleNamespace(event_listeners=[]),
    )
    kernel = CoordinationKernel(host)
    kernel._event_bus = bus
    done = asyncio.Event()
    events = []

    async def scan(event):
        events.append(event)
        await harness.send("scan-wake")
        done.set()

    kernel._dispatcher = SimpleNamespace(dispatch=AsyncMock())
    kernel._scheduler = SimpleNamespace(is_active=True, on_event=scan)
    await kernel.subscribe_transport("origin-test")
    original = ExecutionOrigin(object())
    message = EventMessage(event_type="task_created", sender_id="leader", payload={}).with_execution_origin(original)
    if restore:
        message = EventMessage.model_validate_json(message.model_dump_json())
    with execution_origin_scope(ExecutionOrigin(object())):
        await messager.publish(TeamTopic.TASK.build(get_session_id(), "origin-test"), message)
        await bus.start(wake_callback=kernel._build_wake_callback())
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert await wait_for_state(harness, HarnessState.IDLE)
        assert len(events) == 1 and events[0].event_type is InnerEventType.SCHEDULER_SCAN
        assert events[0].payload == {}  # No task-row source or grant fabricated.
        assert [item[0] for item in seen] == [None if restore else original]
        assert current_execution_origin() is None
    finally:
        await bus.stop()
        await kernel.unsubscribe_transport()


def test_inner_event_source_is_private_and_never_loaded_from_json():
    original = ExecutionOrigin({"host-secret": "not-on-wire"})
    event = InnerEventMessage(event_type=InnerEventType.USER_INPUT).with_execution_origin(original)
    assert copy.deepcopy(event).execution_origin is original
    raw = event.model_dump_json()
    assert "origin" not in raw and "not-on-wire" not in raw
    with execution_origin_scope(ExecutionOrigin(object())):
        assert InnerEventMessage.model_validate_json(raw).execution_origin is None
        assert (
            InnerEventMessage.model_validate(
                {
                    "event_type": "user_input",
                    "_execution_origin": original,
                }
            ).execution_origin
            is None
        )


@pytest.mark.asyncio
async def test_inner_callback_scope_expires_for_late_inherited_task():
    bus = EventBus(role=TeamRole.HUMAN_AGENT)
    kernel = CoordinationKernel(SimpleNamespace())
    kernel._event_bus = bus
    original = ExecutionOrigin(object())
    release, completed = asyncio.Event(), asyncio.Event()
    tasks = []

    async def late():
        await release.wait()
        return current_execution_origin()

    async def receive(_event):
        assert current_execution_origin() is original
        tasks.append(asyncio.create_task(late()))
        completed.set()

    with execution_origin_scope(original):
        await kernel.enqueue_user_input("queued")
    await bus.start(wake_callback=receive)
    try:
        await asyncio.wait_for(completed.wait(), 2)
        # join includes callback completion, unlike just observing an empty queue.
        await asyncio.wait_for(bus._event_queue.join(), 2)
        release.set()
        assert await tasks[0] is None
        assert current_execution_origin() is None
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bus.stop()
