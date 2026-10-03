"""Actual Tool/AbilityManager execution after input transformations."""

import asyncio
import json
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import (
    Tool,
    ToolCard,
    bind_tool_authorizer,
    current_tool_invocation,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import ToolCallEvents
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security.permission_engine.host import ToolPermissionHost

pytestmark = pytest.mark.asyncio


@pytest.fixture
def execution():
    effects = []
    holder = SimpleNamespace(callback=None, rail=None, contexts=[])

    class Probe(Tool):
        async def invoke(self, inputs, **kwargs):
            effects.append(inputs.copy())
            return inputs

        async def stream(self, inputs, **kwargs):
            effects.append(inputs.copy())
            yield inputs

    tool = Probe(ToolCard(id="final-authority-probe", name="probe", description="fixture"))
    manager = AbilityManager(owner_id="final-authority-owner")
    manager.add_ability(tool.card, tool)
    session = SimpleNamespace(get_session_id=lambda: "owned-session", get_state=lambda *_: None)

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
                holder.contexts.append(ctx)
                if holder.callback is not None:
                    bind_tool_authorizer(ctx, holder.callback)
                    bind_tool_authorizer(ctx, holder.callback)
                if holder.rail is not None:
                    await holder.rail.before_tool_call(ctx)

    agent = SimpleNamespace(
        card=SimpleNamespace(id="owner", name="owner"), ability_manager=manager, agent_callback_manager=Callbacks()
    )

    async def invoke(inputs=None):
        return await manager.execute(
            AgentCallbackContext(agent=agent),
            ToolCall(id="call", type="function", name="probe", arguments=json.dumps(inputs or {"path": "original"})),
            session=session,
        )

    holder.tool, holder.manager, holder.session = tool, manager, session
    holder.effects, holder.invoke, holder.agent = effects, invoke, agent
    yield holder
    manager.teardown_tools()


async def test_transform_then_start_callback_then_final_authority(execution):
    seen = []
    events = []

    async def transform(*args, **kwargs):
        events.append("transform")
        return (), {**kwargs, "inputs": {"path": "transformed"}}

    async def started(**kwargs):
        events.append("started")
        kwargs["inputs"][1]["inputs"]["path"] = "final"

    async def authority(operation):
        proof = current_tool_invocation()
        assert proof is not None and proof.executor is execution.tool
        assert proof.original_invoke.__self__ is execution.tool
        assert proof.original_invoke.__func__ is type(execution.tool).invoke
        assert proof.agent_context.session is execution.session
        events.append("authority")
        seen.append(operation.arguments["path"])
        return True

    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform")
    await framework.register(ToolCallEvents.TOOL_CALL_STARTED, started)
    execution.callback = authority
    try:
        await execution.invoke()
        assert execution.effects == [{"path": "final"}]
        assert seen == ["final"]
        assert events == ["transform", "started", "authority"]
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)
        await framework.unregister(ToolCallEvents.TOOL_CALL_STARTED, started)


@pytest.mark.parametrize("decision", [False, None, 1, "true", "error"])
async def test_final_authority_only_exact_true(execution, decision):
    async def authority(_):
        if decision == "error":
            raise RuntimeError("unavailable")
        return decision

    execution.callback = authority
    result = await execution.invoke()
    assert not execution.effects and "PERMISSION_DENIED" in str(result)


async def test_transform_redirect_is_rechecked(execution):
    async def transform(*args, **kwargs):
        return (), {**kwargs, "inputs": {"path": "secret"}}

    async def authority(operation):
        return operation.arguments["path"] == "original"

    execution.callback = authority
    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform")
    try:
        assert "PERMISSION_DENIED" in str(await execution.invoke())
        assert not execution.effects
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)


async def test_legacy_transform_and_direct_stream_preserved(execution):
    async def transform(*args, **kwargs):
        return (), {**kwargs, "inputs": {"path": "legacy"}}

    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform")
    try:
        await execution.invoke()
        assert execution.effects == [{"path": "legacy"}]
        assert [item async for item in execution.tool.stream({"path": "stream"})] == [{"path": "stream"}]
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)


@pytest.mark.parametrize("mutation", ["args", "session", "invoke", "registry"])
async def test_mutation_during_authorization_denies(execution, mutation):
    final = {}

    async def transform(*args, **kwargs):
        final.update(inputs={"path": "allowed"}, kwargs=kwargs)
        return (), {**kwargs, "inputs": final["inputs"]}

    async def authority(_):
        await asyncio.sleep(0)
        if mutation == "args":
            final["inputs"]["path"] = "secret"
        elif mutation == "session":
            current_tool_invocation().agent_context.session = object()
        elif mutation == "invoke":
            execution.tool.invoke = lambda *args, **kwargs: None
        else:
            execution.manager.remove_ability("probe")
        return True

    execution.callback = authority
    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform")
    try:
        assert "PERMISSION_DENIED" in str(await execution.invoke())
        assert not execution.effects
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)


async def test_invoke_replacement_cannot_run_before_authority(execution):
    async def replacement(*args, **kwargs):
        execution.effects.append("bypass")

    async def authority(_):
        return True

    execution.callback = authority
    execution.tool.invoke = replacement
    assert "PERMISSION_DENIED" in str(await execution.invoke())
    assert not execution.effects


async def test_scope_expires_and_copied_context_cannot_reuse_proof(execution):
    saved = []

    async def authority(operation):
        saved.append((copy_context(), current_tool_invocation()))
        return True

    execution.callback = authority
    await execution.invoke()
    ctx, proof = saved[0]
    assert not proof.is_current() and ctx.run(current_tool_invocation) is None
    with pytest.raises(ValueError, match="active, exact"):
        bind_tool_authorizer(execution.contexts[0], authority)
    execution.callback = None
    await execution.invoke()
    assert len(saved) == 1 and len(execution.effects) == 2


async def test_inherited_context_cannot_start_unbound_child(execution):
    saved = []

    async def authority(_):
        saved.append(copy_context())
        return True

    execution.callback = authority
    await execution.invoke()
    execution.callback = None
    child = saved[0].run(asyncio.create_task, execution.invoke())
    result = await child
    assert "PERMISSION_DENIED" in str(result) and len(execution.effects) == 1


async def test_permission_rail_runs_existing_checks_and_final_check(execution):
    seen = []

    async def authority(scene):
        seen.append((current_tool_invocation() is not None, scene.tool_args["path"]))
        return True

    execution.rail = PermissionInterruptRail(
        config={"enabled": True, "defaults": {"*": "allow"}, "file_guard": {"enabled": False}},
        host=ToolPermissionHost(authorize_tool=authority),
    )
    await execution.invoke()
    assert seen == [(False, "original"), (False, "original"), (True, "original")]
    assert len(execution.effects) == 1


async def test_protected_stream_is_explicitly_denied(execution):
    async def authority(_):
        with pytest.raises(PermissionError, match="Tool.stream"):
            async for _item in execution.tool.stream({"path": "bypass"}):
                pass
        return True

    execution.callback = authority
    await execution.invoke()
    assert execution.effects == [{"path": "original"}]


async def test_non_tool_ability_is_explicitly_denied(execution):
    async def authority(_):
        return True

    execution.callback = authority
    execution.manager.remove_ability("probe")
    execution.manager._workflows["probe"] = SimpleNamespace(id="workflow")
    result = await execution.invoke()
    assert "PERMISSION_DENIED" in str(result) and not execution.effects


async def test_transform_cannot_replace_actual_session(execution):
    async def transform(*args, **kwargs):
        return args, {**kwargs, "session": SimpleNamespace(get_session_id=lambda: "other")}

    async def authority(_):
        return True

    execution.callback = authority
    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform")
    try:
        result = await execution.invoke()
        assert "PERMISSION_DENIED" in str(result) and not execution.effects
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)


async def test_parallel_calls_keep_distinct_proofs(execution):
    proofs = []

    async def authority(operation):
        proof = current_tool_invocation()
        await asyncio.sleep(0)
        assert current_tool_invocation() is proof and proof.operation is operation
        proofs.append(proof)
        return True

    execution.callback = authority
    await asyncio.gather(execution.invoke({"path": "left"}), execution.invoke({"path": "right"}))
    assert sorted(item["path"] for item in execution.effects) == ["left", "right"]
    assert proofs[0].agent_context is not proofs[1].agent_context
    assert all(not proof.is_current() for proof in proofs)


async def test_legacy_copied_context_stays_legacy_after_parent_finishes(execution):
    saved = []

    async def started(**kwargs):
        saved.append(copy_context())

    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_CALL_STARTED, started)
    try:
        await execution.invoke()
    finally:
        await framework.unregister(ToolCallEvents.TOOL_CALL_STARTED, started)
    await saved[0].run(asyncio.create_task, execution.invoke())
    assert len(execution.effects) == 2


async def test_transform_cannot_add_unrepresented_runtime_kwargs(execution):
    async def transform(*args, **kwargs):
        return args, {**kwargs, "private_path": "secret"}

    async def authority(_):
        return True

    execution.callback = authority
    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform")
    try:
        assert "PERMISSION_DENIED" in str(await execution.invoke())
        assert not execution.effects
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)
