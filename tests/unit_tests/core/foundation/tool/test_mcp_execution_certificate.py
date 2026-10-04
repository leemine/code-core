"""MCP parse callbacks cannot consume the actual client's execution evidence."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.utils.schema_utils import SchemaUtils
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import bind_tool_authorizer, current_tool_execution
from openjiuwen.core.foundation.tool.mcp.base import MCPTool, McpToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.runner.callback.events import ToolCallEvents
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent

pytestmark = pytest.mark.asyncio


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(captured=[], calls=[], guarded=True, during_client=None)

    async def call_tool(tool_name, arguments):
        proof = current_tool_execution()
        if state.guarded:
            assert proof is not None and proof.is_current() and proof.is_current_origin()
        state.calls.append((tool_name, dict(arguments), proof))
        if state.during_client:
            await state.during_client(proof)
        return arguments

    tool = MCPTool(
        SimpleNamespace(call_tool=call_tool),
        McpToolCard(
            name="echo",
            id="echo",
            server_name="fixture",
            input_params={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
    )
    manager = AbilityManager(owner_id="mcp-certificate-owner")
    manager.add_ability(tool.card, tool)
    session = SimpleNamespace(get_session_id=lambda: "session", get_state=lambda *_args: None)

    async def allow(_operation):
        return True

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL and state.guarded:
                bind_tool_authorizer(ctx, allow)

    agent = SimpleNamespace(
        card=SimpleNamespace(id="agent", name="agent"), ability_manager=manager, agent_callback_manager=Callbacks()
    )
    original = SchemaUtils.format_with_schema

    def capture_during_schema(*args, **kwargs):
        # Instrument the synchronous parse phase to retain the actual method's
        # proof before FINISHED, without changing parsing or callback dispatch.
        state.captured.append(current_tool_execution())
        return original(*args, **kwargs)

    monkeypatch.setattr(SchemaUtils, "format_with_schema", capture_during_schema)

    async def invoke():
        return await manager.execute(
            AgentCallbackContext(agent=agent),
            ToolCall(id="call", type="function", name="echo", arguments=json.dumps({"text": "initial"})),
            session=session,
        )

    state.invoke, state.tool = invoke, tool
    yield state
    manager.teardown_tools()


async def test_parse_callbacks_mask_getter_and_captured_origin_then_restore(runtime):
    phases = []

    async def started(raw_inputs, **_kwargs):
        assert current_tool_execution() is None
        phases.append("started")
        raw_inputs["text"] = "started-transform"

    async def finished(formatted_inputs, **_kwargs):
        assert current_tool_execution() is None
        proof = runtime.captured[-1]
        assert proof is not None and not proof.is_current() and not proof.is_current_origin()

        async def child():
            assert current_tool_execution() is None
            assert not proof.is_current_origin()

        await asyncio.create_task(child())
        phases.append("finished")
        formatted_inputs["text"] = "actual-postparse"

    callbacks = [(ToolCallEvents.TOOL_PARSE_STARTED, started), (ToolCallEvents.TOOL_PARSE_FINISHED, finished)]
    for event, callback in callbacks:
        await Runner.callback_framework.register(event, callback)
    try:
        result = await runtime.invoke()
    finally:
        for event, callback in callbacks:
            await Runner.callback_framework.unregister(event, callback)
    assert "actual-postparse" in str(result)
    assert phases == ["started", "finished"]
    assert len(runtime.calls) == 1
    _, arguments, proof = runtime.calls[0]
    assert arguments == {"text": "actual-postparse"}
    assert proof is runtime.captured[-1]
    assert proof.operation.arguments["text"] == "initial"
    assert not proof.is_current_origin() and current_tool_execution() is None


@pytest.mark.parametrize("failure", ["abort", "cancel"])
async def test_failed_parse_never_reaches_client_and_does_not_leak_certificate(runtime, failure):
    async def finished(**_kwargs):
        assert current_tool_execution() is None
        assert not runtime.captured[-1].is_current_origin()
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise AbortError("synthetic parse abort")

    await Runner.callback_framework.register(ToolCallEvents.TOOL_PARSE_FINISHED, finished)
    try:
        result = await runtime.invoke()
        if failure == "cancel":
            # AbilityManager's batch facade preserves its interruption result.
            assert "[Interrupted]" in str(result)
        else:
            assert "synthetic parse abort" in str(result)
    finally:
        await Runner.callback_framework.unregister(ToolCallEvents.TOOL_PARSE_FINISHED, finished)
    assert not runtime.calls and current_tool_execution() is None
    assert runtime.captured[-1] is not None and not runtime.captured[-1].is_current_origin()


async def test_legacy_parse_transform_keeps_original_client_behavior(runtime):
    runtime.guarded = False

    async def finished(formatted_inputs, **_kwargs):
        assert current_tool_execution() is None
        formatted_inputs["text"] = "legacy-transform"

    await Runner.callback_framework.register(ToolCallEvents.TOOL_PARSE_FINISHED, finished)
    try:
        result = await runtime.invoke()
    finally:
        await Runner.callback_framework.unregister(ToolCallEvents.TOOL_PARSE_FINISHED, finished)
    assert "legacy-transform" in str(result)
    assert runtime.calls == [("echo", {"text": "legacy-transform"}, None)]
