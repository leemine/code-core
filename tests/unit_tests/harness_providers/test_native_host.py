# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host ports retain resources and Python input identity within one Native harness."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness_protocol import HarnessContext, HarnessInput, HarnessState, TurnEventKind, TurnLifecycleEvent
from openjiuwen.harness_providers.base import ProviderStartupError
from openjiuwen.harness_providers.native import DeepAgentHarness, NativeHostHooks
from tests.unit_tests.harness_providers.test_native_output_preservation import _Stream


def _parts():
    session = SimpleNamespace(get_session_id=lambda: "session", pre_run=AsyncMock(), post_run=AsyncMock())
    agent = MagicMock(spec=DeepAgent)
    agent.card = SimpleNamespace(id="a")
    agent.ensure_initialized = AsyncMock()
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    agent.send_input = AsyncMock()
    return agent, session


def _context():
    return HarnessContext(agent_name="native", agent_id="a", host_session_id="session", system_prompt="")


@pytest.mark.asyncio
async def test_host_session_and_guard_order_and_exact_request_identity():
    agent, session = _parts()
    order = []
    agent.ensure_initialized.side_effect = lambda: order.append("initialize")
    session.pre_run.side_effect = lambda **kw: order.append("pre_run")
    agent.start.side_effect = lambda **kw: order.append("start")
    handoff = object()
    original = SendInputRequest(request_id="host-request", inputs={"query": "hi", "handoff": handoff})
    stream = _Stream([])
    agent.attach_output = AsyncMock(return_value=stream)

    async def make_session(context, instance):
        assert instance is agent
        order.append("session")
        return session

    async def before(instance, host_session):
        assert host_session is session
        order.append("guard")

    async def dispatch(instance, request, content, resuming):
        assert harness.state is HarnessState.RUNNING
        agent.attach_output.assert_awaited_once()
        assert not resuming
        assert content.content == "hi"
        await instance.send_input(original)
        return True

    harness = DeepAgentHarness(
        lambda context: agent,
        session_id="session",
        observe_tools=False,
        host_hooks=NativeHostHooks(make_session, before, dispatch),
    )
    await harness.start(_context())
    receipt = await harness.send(HarnessInput(content="hi"))
    events = [e async for e in harness.turn_events(receipt.turn_id)]
    await harness.stop()
    await harness.stop()
    assert order == ["initialize", "session", "pre_run", "guard", "start"]
    assert agent.send_input.await_args.args[0] is original
    assert agent.send_input.await_args.args[0].inputs["handoff"] is handoff
    assert [e.event.kind for e in events if isinstance(e.event, TurnLifecycleEvent)] == [
        TurnEventKind.STARTED,
        TurnEventKind.FINISHED,
    ]
    agent.stop.assert_awaited_once()
    session.post_run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pre_run", "guard", "start"])
async def test_partial_start_rolls_back_once(failure):
    agent, session = _parts()
    before = AsyncMock()
    failing = {"pre_run": session.pre_run, "guard": before, "start": agent.start}[failure]
    failing.side_effect = RuntimeError("startup failed")
    hooks = NativeHostHooks(AsyncMock(return_value=session), before)
    harness = DeepAgentHarness(lambda context: agent, session_id="session", host_hooks=hooks)
    with pytest.raises(ProviderStartupError):
        await harness.start(_context())
    await harness.stop()
    agent.stop.assert_awaited_once()
    session.post_run.assert_awaited_once()
    assert harness.agent is None
    assert harness.state is HarnessState.TERMINATED


@pytest.mark.asyncio
async def test_running_agent_is_not_adopted_or_stopped():
    agent, session = _parts()
    agent._interaction_started = True
    make_session = AsyncMock(return_value=session)
    harness = DeepAgentHarness(lambda context: agent, host_hooks=NativeHostHooks(make_session))
    with pytest.raises(ProviderStartupError):
        await harness.start(_context())
    agent.stop.assert_not_awaited()
    make_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_host_session_is_released_without_starting_agent():
    agent, session = _parts()
    harness = DeepAgentHarness(
        lambda context: agent, session_id="other", host_hooks=NativeHostHooks(AsyncMock(return_value=session))
    )
    with pytest.raises(ProviderStartupError):
        await harness.start(_context())
    agent.start.assert_not_awaited()
    session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_streaming_control_does_not_wait_for_output():
    agent, session = _parts()
    stream = _Stream([])
    agent.attach_output = AsyncMock(return_value=stream)
    dispatch = AsyncMock(return_value=False)
    harness = DeepAgentHarness(
        lambda context: agent,
        session_id="session",
        host_hooks=NativeHostHooks(AsyncMock(return_value=session), dispatch_input=dispatch),
    )
    await harness.start(_context())
    receipt = await harness.send(HarnessInput(content="goal status"))
    events = [e async for e in harness.turn_events(receipt.turn_id)]
    await harness.stop()
    agent.send_input.assert_not_awaited()
    stream.close.assert_awaited_once_with(abort_active_round=False)
    assert events[-1].event.kind is TurnEventKind.FINISHED


@pytest.mark.asyncio
async def test_concurrent_harness_cannot_claim_same_initializing_agent():
    import asyncio

    agent, session = _parts()
    entered, release = asyncio.Event(), asyncio.Event()

    async def initialize():
        entered.set()
        await release.wait()

    agent.ensure_initialized.side_effect = initialize
    hooks = NativeHostHooks(AsyncMock(return_value=session))
    first = DeepAgentHarness(lambda context: agent, session_id="session", host_hooks=hooks)
    second = DeepAgentHarness(lambda context: agent, session_id="session", host_hooks=hooks)
    start = asyncio.create_task(first.start(_context()))
    try:
        await entered.wait()
        with pytest.raises(ProviderStartupError):
            await second.start(_context())
        agent.stop.assert_not_awaited()
    finally:
        release.set()
        await start
        await first.stop()
    agent.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_start_releases_owned_session():
    import asyncio

    agent, session = _parts()
    entered = asyncio.Event()

    async def before(instance, host_session):
        entered.set()
        await asyncio.Event().wait()

    harness = DeepAgentHarness(
        lambda context: agent, session_id="session", host_hooks=NativeHostHooks(AsyncMock(return_value=session), before)
    )
    start = asyncio.create_task(harness.start(_context()))
    await entered.wait()
    assert harness.agent is None
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    agent.stop.assert_awaited_once()
    session.post_run.assert_awaited_once()
    assert harness.agent is None


@pytest.mark.asyncio
async def test_cancel_while_acquiring_output_never_sends_input():
    import asyncio
    from openjiuwen.harness_protocol import AbortMode

    agent, session = _parts()
    entered, release = asyncio.Event(), asyncio.Event()
    stream = _Stream([])

    async def attach():
        entered.set()
        await release.wait()
        return stream

    agent.attach_output = AsyncMock(side_effect=attach)
    agent.cancel_round = AsyncMock()
    harness = DeepAgentHarness(
        lambda context: agent, session_id="session", host_hooks=NativeHostHooks(AsyncMock(return_value=session))
    )
    await harness.start(_context())
    receipt = await harness.send(HarnessInput(content="hi"))
    try:
        await entered.wait()
        await harness.abort(mode=AbortMode.FORCE)
        release.set()
        events = [event async for event in harness.turn_events(receipt.turn_id)]
        agent.send_input.assert_not_awaited()
        assert events[-1].event.kind is TurnEventKind.ABORTED
    finally:
        release.set()
        await harness.stop()
