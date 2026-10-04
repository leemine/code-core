"""Actual method lifetime evidence, distinct from final authorization proof."""

import asyncio
import json
from contextvars import copy_context
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import (
    Tool,
    ToolCard,
    ToolExecution,
    bind_tool_authorizer,
    current_tool_execution,
    current_tool_invocation,
    invoke_tool_with_authority,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import ToolCallEvents
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.harness_protocol import BeforeToolContext

pytestmark = pytest.mark.asyncio


@pytest.fixture
def runtime():
    state = SimpleNamespace(body=None, guarded=True, authorizers=[], proofs=[], events=[], certificates=[])

    class Probe(Tool):
        async def invoke(self, inputs, **kwargs):
            certificate = current_tool_execution()
            state.certificates.append(certificate)
            assert current_tool_invocation() is None
            if state.body:
                return await state.body(inputs, kwargs)
            return inputs

        async def stream(self, inputs, **kwargs):
            yield inputs

    tool = Probe(ToolCard(id="execution-certificate", name="probe", description="fixture"))
    manager = AbilityManager(owner_id="execution-certificate-owner")
    manager.add_ability(tool.card, tool)
    session = SimpleNamespace(get_session_id=lambda: "session", get_state=lambda *_: None)

    async def allow(operation):
        assert current_tool_execution() is None
        state.proofs.append(current_tool_invocation())
        return True

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL and state.guarded:
                for authorizer in (allow, *state.authorizers):
                    bind_tool_authorizer(ctx, authorizer)

    agent = SimpleNamespace(
        card=SimpleNamespace(name="owner", id="owner"), ability_manager=manager, agent_callback_manager=Callbacks()
    )

    async def invoke():
        return await manager.execute(
            AgentCallbackContext(agent=agent),
            ToolCall(id="call", type="function", name="probe", arguments=json.dumps({"value": "initial"})),
            session=session,
        )

    source = BeforeToolContext("host-agent", "session", "turn", "call", "remote_probe", {"value": "initial"})
    host = SimpleNamespace(current=True, executor=tool, kwargs={"session": session}, predicate=None)

    async def invoke_host():
        return await invoke_tool_with_authority(
            tool,
            {"value": "initial"},
            operation=source,
            authorizer=allow,
            runtime_kwargs=host.kwargs,
            is_current=host.predicate or (lambda: host.current),
            resolve_executor=lambda: host.executor,
        )

    state.tool, state.manager, state.session, state.agent = tool, manager, session, agent
    state.invoke, state.invoke_host, state.source, state.host = invoke, invoke_host, source, host
    yield state
    manager.teardown_tools()


async def test_actual_method_only_and_immutable_authorized_operation(runtime):
    phases = []
    framework = Runner.callback_framework
    events = [
        ToolCallEvents.TOOL_INVOKE_INPUT,
        ToolCallEvents.TOOL_CALL_STARTED,
        ToolCallEvents.TOOL_CALL_FINISHED,
        ToolCallEvents.TOOL_INVOKE_OUTPUT,
    ]

    async def observer(**kwargs):
        phases.append(current_tool_execution())

    for event in events:
        await framework.register(event, observer)

    async def body(inputs, kwargs):
        certificate = current_tool_execution()
        assert isinstance(certificate, ToolExecution)
        assert certificate.executor is runtime.tool and certificate.agent_context.session is runtime.session
        assert certificate.owning_task is asyncio.current_task()
        assert certificate.original_invoke.__self__ is runtime.tool
        assert certificate.original_invoke.__func__ is type(runtime.tool).invoke
        assert certificate.source_operation is None
        assert certificate.is_current() and certificate.is_current_origin()
        assert all(not proof.is_current() for proof in runtime.proofs)
        with pytest.raises(FrozenInstanceError):
            certificate.executor = object()
        # Internal parser mutation is deliberately not a new authorization grant.
        inputs["value"] = "post-parse"
        assert certificate.operation.arguments["value"] == "initial"
        assert certificate.is_current()
        return inputs

    runtime.body = body
    try:
        await runtime.invoke()
    finally:
        for event in events:
            await framework.unregister(event, observer)
    assert len(phases) == 4 and phases == [None] * 4
    assert current_tool_execution() is None
    assert not runtime.certificates[0].is_current_origin()


async def test_error_callbacks_and_saved_context_cannot_observe_certificate(runtime):
    contexts = []
    seen = []

    async def body(*_):
        contexts.append(copy_context())
        raise RuntimeError("fixture failure")

    async def error(**kwargs):
        seen.append(current_tool_execution())

    runtime.body = body
    await Runner.callback_framework.register(ToolCallEvents.TOOL_CALL_ERROR, error)
    try:
        await runtime.invoke()
    finally:
        await Runner.callback_framework.unregister(ToolCallEvents.TOOL_CALL_ERROR, error)
    assert seen and all(value is None for value in seen)
    assert contexts[0].run(current_tool_execution) is None
    assert not contexts[0].run(runtime.certificates[0].is_current_origin)


async def test_last_authorizer_denial_creates_no_certificate(runtime):
    async def deny(_):
        assert current_tool_execution() is None
        return False

    runtime.authorizers.append(deny)
    assert "PERMISSION_DENIED" in str(await runtime.invoke())
    assert runtime.certificates == []


async def test_legacy_still_runs_without_certificate(runtime):
    runtime.guarded = False
    await runtime.invoke()
    await runtime.tool.invoke({"value": "direct"})
    assert runtime.certificates == [None, None]
    assert [value async for value in runtime.tool.stream({"value": "stream"})] == [{"value": "stream"}]


@pytest.mark.parametrize("kind", ["native", "host", "task_bound_host"])
async def test_child_can_only_validate_captured_origin_and_never_capture(runtime, kind):
    checks = []
    original_task = asyncio.current_task()
    if kind == "task_bound_host":
        runtime.host.predicate = lambda: asyncio.current_task() is original_task

    async def body(*_):
        certificate = current_tool_execution()

        async def sdk_child():
            checks.append((current_tool_execution(), certificate.is_current(), certificate.is_current_origin()))

        await asyncio.create_task(sdk_child())
        assert current_tool_execution() is certificate and certificate.is_current()
        if kind != "native":
            assert certificate.source_operation is runtime.source
        return "ok"

    runtime.body = body
    await (runtime.invoke() if kind == "native" else runtime.invoke_host())
    assert checks == [(None, False, kind != "task_bound_host")]


@pytest.mark.parametrize("mutation", ["executor", "card", "name", "invoke", "session", "agent", "call_id"])
async def test_actual_execution_origin_detects_replacement(runtime, mutation):
    async def body(*_):
        certificate = current_tool_execution()
        ctx = certificate.agent_context
        if mutation == "executor":
            runtime.manager.remove_ability("probe")
        elif mutation == "card":
            runtime.tool._card = ToolCard(id="other", name="probe", description="fixture")
        elif mutation == "name":
            runtime.tool.card.name = "other"
        elif mutation == "invoke":
            runtime.tool.invoke = lambda *_: None
        elif mutation == "session":
            ctx.session = object()
        elif mutation == "agent":
            ctx.agent = object()
        else:
            ctx.inputs.tool_call.id = "other"
        assert not certificate.is_current() and not certificate.is_current_origin()
        assert current_tool_execution() is None
        return "ok"

    runtime.body = body
    await runtime.invoke()


@pytest.mark.parametrize("mutation", ["source", "kwargs", "registry"])
async def test_host_origin_rechecks_original_source_and_runtime_kwargs(runtime, mutation):
    async def body(*_):
        certificate = current_tool_execution()
        if mutation == "source":
            runtime.host.current = False
        elif mutation == "kwargs":
            runtime.host.kwargs["session"] = object()
        else:
            runtime.host.executor = object()
        assert not certificate.is_current_origin()
        return "ok"

    runtime.body = body
    await runtime.invoke_host()


async def test_cancellation_immediately_invalidates_origin_and_saved_child(runtime):
    started = asyncio.Event()
    release = asyncio.Event()
    children = []
    checks = []

    async def body(*_):
        certificate = current_tool_execution()

        async def child():
            await release.wait()
            checks.append(certificate.is_current_origin())

        children.append(asyncio.create_task(child()))
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert not certificate.is_current_origin()
            raise

    runtime.body = body
    task = asyncio.create_task(runtime.invoke())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.gather(*children)
    assert checks == [False]


async def test_nested_direct_tool_callbacks_do_not_borrow_parent(runtime):
    observations = []

    class Other(Tool):
        async def invoke(self, inputs, **kwargs):
            raise AssertionError("nested unknown executor must not run")

        async def stream(self, inputs, **kwargs):
            yield inputs

    nested = Other(ToolCard(id="nested-other", name="other", description="fixture"))

    async def observer(**kwargs):
        if kwargs.get("tool_name") == "other":
            observations.append(current_tool_execution())

    framework = Runner.callback_framework
    events = [ToolCallEvents.TOOL_INVOKE_INPUT, ToolCallEvents.TOOL_CALL_STARTED, ToolCallEvents.TOOL_CALL_ERROR]
    for event in events:
        await framework.register(event, observer)

    async def body(*_):
        certificate = current_tool_execution()
        with pytest.raises(PermissionError):
            await nested.invoke({"value": "child"}, session=runtime.session)
        assert current_tool_execution() is certificate
        return "ok"

    runtime.body = body
    try:
        await runtime.invoke()
    finally:
        for event in events:
            await framework.unregister(event, observer)
    assert observations and all(value is None for value in observations)


async def test_nested_ability_manager_has_no_implicit_authority(runtime):
    async def body(*_):
        certificate = current_tool_execution()
        runtime.guarded = False
        assert "PERMISSION_DENIED" in str(await runtime.invoke())
        assert current_tool_execution() is certificate
        return "ok"

    runtime.body = body
    await runtime.invoke()
    assert len(runtime.certificates) == 1


async def test_four_layer_unwrap_registered_original_and_legacy_identity(runtime):
    method = runtime.tool.invoke
    for _ in range(4):
        method = method.__wrapped__
    assert method.__self__ is runtime.tool
    assert method.__func__ is type(runtime.tool).invoke

    async def body(*_):
        assert current_tool_execution().original_invoke is method
        return "ok"

    runtime.body = body
    await runtime.invoke()


async def test_certificate_uses_final_transformed_authorized_operation(runtime):
    async def transform(*args, **kwargs):
        assert current_tool_execution() is None
        return (), {**kwargs, "inputs": {"value": "authorized-transform"}}

    async def body(inputs, kwargs):
        certificate = current_tool_execution()
        assert certificate.operation.arguments["value"] == inputs["value"] == "authorized-transform"
        return inputs

    runtime.body = body
    await Runner.callback_framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform")
    try:
        await runtime.invoke()
    finally:
        await Runner.callback_framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)


async def test_concurrent_actual_methods_keep_distinct_certificates(runtime):
    seen = []

    async def body(*_):
        certificate = current_tool_execution()
        seen.append(certificate)
        await asyncio.sleep(0)
        assert current_tool_execution() is certificate
        return "ok"

    runtime.body = body
    await asyncio.gather(runtime.invoke(), runtime.invoke())
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert all(not certificate.is_current_origin() for certificate in seen)
