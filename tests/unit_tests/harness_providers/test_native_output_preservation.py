# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native compatibility projection through the real protocol turn/pump lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness_protocol import HarnessContext, ItemLifecycleEvent, TurnEventKind, TurnLifecycleEvent
from openjiuwen.harness_protocol.serialization import harness_event_from_dict, harness_event_to_dict
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter
from openjiuwen.harness_providers.native import DeepAgentHarness
from openjiuwen.harness_providers.native.mapping import restore_chunk


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.close = AsyncMock()

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self.chunks:
            yield chunk


def _setup(monkeypatch, rounds, *, preserve=True, observe_tools=False):
    streams = [_Stream(chunks) for chunks in rounds]
    agent = MagicMock(spec=DeepAgent)
    agent.card = SimpleNamespace(id="native-test")
    agent.ensure_initialized = AsyncMock()
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    agent.send_input = AsyncMock()
    agent.attach_output = AsyncMock(side_effect=streams)
    agent.cancel_round = AsyncMock()
    session = SimpleNamespace(pre_run=AsyncMock(), post_run=AsyncMock())
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", lambda **kwargs: session)
    harness = DeepAgentHarness(lambda context: agent, preserve_output_chunks=preserve, observe_tools=observe_tools)
    return harness, agent, session, streams


async def _run(harness, *, preserve=True, answer=None, abort_waiting=False):
    events = []
    finished = asyncio.Event()

    async def observe(envelope):
        events.append(envelope)
        if isinstance(envelope.event, TurnLifecycleEvent) and envelope.event.kind in {
            TurnEventKind.FINISHED,
            TurnEventKind.FAILED,
            TurnEventKind.ABORTED,
        }:
            finished.set()

    adapter = HarnessIOAdapter(
        harness, event_observer=observe, preserve_native_chunks=preserve, auto_approve_tools=False
    )
    await adapter.start(HarnessContext(agent_name="native", agent_id="a", host_session_id="s", system_prompt=""))
    chunks = []

    async def drain():
        async for chunk in adapter.outputs():
            chunks.append(chunk)
            if chunk.type == INTERACTION and abort_waiting:
                await adapter.abort(immediate=True)
            elif chunk.type == INTERACTION and answer is not None:
                response = InteractiveInput()
                response.update(chunk.payload.id, answer)
                await adapter.send(response)

    consumer = asyncio.create_task(drain())
    try:
        receipt = await adapter.send("test")
        await asyncio.wait_for(finished.wait(), 3)
    finally:
        await adapter.stop()
        await asyncio.wait_for(consumer, 3)
    return receipt, events, chunks


def _chunks():
    return [
        OutputSchema(type="llm_reasoning", index=3, payload={"content": "thinking", "model_name": "m"}),
        OutputSchema(type="llm_output", index=9, payload={"content": "", "revision": 2}),
        OutputSchema(type="llm_output", index=10, payload={"content": "done", "subagent_id": "child"}),
        OutputSchema(
            type="tool_call",
            index=12,
            payload={
                "tool_call": {
                    "tool_call_id": "call-1",
                    "name": "read",
                    "arguments": {"path": "a"},
                    "display_name": "Read a",
                }
            },
        ),
        OutputSchema(
            type="tool_result",
            index=13,
            payload={
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "read",
                    "result": "legacy text",
                    "raw_output": {"success": True, "data": [1, 2]},
                    "rendered_result": "model text",
                    "success": True,
                }
            },
        ),
        OutputSchema(type="subagent.event", index=16, payload={"id": "child", "parent_id": "a", "revision": 3}),
        OutputSchema(type="goal.updated", index=17, payload={"id": "goal", "status": "running"}),
        OutputSchema(type="answer", index=20, payload={"output": "done", "result_type": "answer", "usage": {"x": 3}}),
    ]


@pytest.mark.asyncio
async def test_native_round_trip_preserves_order_fields_and_one_terminal(monkeypatch):
    original = _chunks()
    harness, agent, session, streams = _setup(monkeypatch, [original])
    receipt, events, chunks = await _run(harness)
    assert [chunk.model_dump() for chunk in chunks] == [chunk.model_dump() for chunk in original]
    restored = [restore_chunk(harness_event_from_dict(harness_event_to_dict(e)).event) for e in events]
    assert [c.model_dump() for c in restored if c is not None] == [c.model_dump() for c in original]
    lifecycle = [e.event for e in events if isinstance(e.event, TurnLifecycleEvent)]
    assert [e.kind for e in lifecycle] == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
    assert lifecycle[-1].result.final_output == "done"
    tools = [e for e in events if isinstance(e.event, ItemLifecycleEvent)]
    assert len(tools) == 2
    assert {e.item_id for e in tools} == {"call-1"}
    assert tools[1].event.data["result"] == {"success": True, "data": (1, 2)}
    assert tools[1].event.data["rendered_result"] == "model text"
    assert all(e.turn_id == receipt.turn_id for e in events if e.turn_id is not None)
    agent.add_rail.assert_not_called()
    agent.send_input.assert_awaited_once()
    agent.stop.assert_awaited_once()
    session.post_run.assert_awaited_once()
    streams[0].close.assert_awaited_once_with(abort_active_round=False)


@pytest.mark.asyncio
async def test_default_projection_retains_legacy_flat_behavior(monkeypatch):
    harness, agent, _, _ = _setup(monkeypatch, [_chunks()], preserve=False, observe_tools=True)
    _, _, chunks = await _run(harness, preserve=False)
    assert [c.type for c in chunks] == ["llm_reasoning", "llm_output", "tool_call", "tool_result"]
    assert chunks[-1].payload["result"] == {"success": True, "data": [1, 2]}
    assert chunks[-1].payload["rendered_result"] == "model text"
    agent.add_rail.assert_called_once()


@pytest.mark.asyncio
async def test_interaction_is_not_mirrored_and_resumes_same_turn(monkeypatch):
    interrupt = OutputSchema(type=INTERACTION, index=0, payload={"id": "q1", "value": {"message": "Continue?"}})
    final = OutputSchema(type="answer", index=6, payload={"output": "resumed"})
    harness, agent, _, _ = _setup(monkeypatch, [[interrupt], [final]])
    receipt, events, chunks = await _run(harness, answer="yes")
    assert [c.type for c in chunks] == [INTERACTION, "answer"]
    assert agent.send_input.await_count == 2
    response = agent.send_input.await_args_list[1].args[0].inputs["query"]
    assert isinstance(response, InteractiveInput)
    assert response.user_inputs["q1"] == "yes"
    terminals = [
        e for e in events if isinstance(e.event, TurnLifecycleEvent) and e.event.kind is TurnEventKind.FINISHED
    ]
    assert len(terminals) == 1
    assert terminals[0].turn_id == receipt.turn_id


@pytest.mark.asyncio
async def test_execution_error_preserves_chunk_and_failed_terminal(monkeypatch):
    error = OutputSchema(type="execution.error", index=7, payload={"message": "failed", "code": "E1", "detail": [1]})
    harness, _, _, _ = _setup(monkeypatch, [[error]])
    _, events, chunks = await _run(harness)
    assert chunks[0].model_dump() == error.model_dump()
    terminal = [e.event for e in events if isinstance(e.event, TurnLifecycleEvent)][-1]
    assert terminal.kind is TurnEventKind.FAILED
    assert terminal.result.error.code == "E1"


def test_external_provider_cannot_enable_native_projection():
    with pytest.raises(ValueError, match="Native"):
        HarnessIOAdapter(SimpleNamespace(card=SimpleNamespace(name="codex")), preserve_native_chunks=True)


@pytest.mark.asyncio
async def test_abort_waiting_releases_interaction_without_resending(monkeypatch):
    interrupt = OutputSchema(type=INTERACTION, index=0, payload={"id": "q1", "value": "Continue?"})
    harness, agent, _, _ = _setup(monkeypatch, [[interrupt]])
    _, events, chunks = await _run(harness, abort_waiting=True)
    assert [c.type for c in chunks] == [INTERACTION]
    lifecycle = [e.event.kind for e in events if isinstance(e.event, TurnLifecycleEvent)]
    assert lifecycle == [TurnEventKind.STARTED, TurnEventKind.ABORTED]
    agent.send_input.assert_awaited_once()
    agent.cancel_round.assert_awaited_once()
