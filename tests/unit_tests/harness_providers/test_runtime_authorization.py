# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Permission transitions reuse original Turn and interaction controls."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness_protocol import (
    ExecutionAuthorization,
    HarnessCapability,
    HarnessInput,
    HarnessProtocolError,
    HarnessStateError,
    ToolApprovalDecision,
    ToolApprovalRequest,
    UserInputRequest,
)
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter
from openjiuwen.harness_providers.opencode import OpenCodeHarness
from tests.unit_tests.harness_providers.test_base import _ScriptedHarness
from tests.unit_tests.harness_providers.test_opencode import config, context


class PermissionHarness(_ScriptedHarness):
    card = replace(
        _ScriptedHarness.card,
        capabilities=_ScriptedHarness.card.capabilities
        | {
            HarnessCapability.RUNTIME_AUTHORIZATION,
        },
    )

    async def _apply_authorization(self, authorization, *, runtime_policy=None):
        if getattr(self, "fail_permission", False):
            raise HarnessProtocolError("native readback failed")
        self.applied = authorization


@pytest.mark.asyncio
async def test_running_update_waits_and_failed_readback_blocks_new_input():
    harness = PermissionHarness()
    await harness.start(context())
    try:
        harness.release.clear()
        await harness.send(HarnessInput("work"))
        update = asyncio.create_task(harness.update_authorization(ExecutionAuthorization(True)))
        await asyncio.sleep(0)
        assert not update.done()
        harness.release.set()
        await asyncio.wait_for(update, 2)
        assert harness.applied.full_access
        harness.fail_permission = True
        with pytest.raises(HarnessProtocolError):
            await harness.update_authorization(ExecutionAuthorization(False))
        with pytest.raises(HarnessStateError, match="unconfirmed"):
            await harness.send(HarnessInput("must not execute"))
        harness.fail_permission = False
        await harness.update_authorization(ExecutionAuthorization(False))
        assert not harness.applied.full_access
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_only_pending_tools_are_reconsidered():
    io = HarnessIOAdapter(PermissionHarness(), auto_approve_tools=False)
    tool = asyncio.create_task(
        io.handle(ToolApprovalRequest(request_id="tool-1", tool_name="read", call_id="call", arguments={}))
    )
    question = asyncio.create_task(io.handle(UserInputRequest(request_id="question-1", prompt="Which file?")))
    await asyncio.sleep(0)
    io.set_tool_auto_approval(True)
    assert (await tool).decision is ToolApprovalDecision.ALLOW
    assert not question.done()
    assert "tool-1" not in io.pending_interrupt_ids
    chunks = [await io._output_queue.get() for _ in range(3)]
    resolved = chunks[-1].chunk
    assert resolved.type == "chat.interaction_resolved"
    assert resolved.payload == {"interaction_id": "tool-1", "kind": "tool_approval"}
    io.set_tool_auto_approval(False)
    next_tool = asyncio.create_task(
        io.handle(ToolApprovalRequest(request_id="tool-2", tool_name="read", call_id="call", arguments={}))
    )
    await asyncio.sleep(0)
    assert not next_tool.done()
    await io.cancel("question-1")
    await io.cancel("tool-2")
    await asyncio.gather(question, next_tool)


@pytest.mark.asyncio
async def test_opencode_exact_readback_and_fixed_configuration():
    harness = OpenCodeHarness(config())
    harness._context = context()
    harness._session_id = "ses_original"
    harness._cycle_started = True
    native = {}

    async def request(method, path, body=None):
        assert path == "/session/ses_original"
        if method == "PATCH":
            native["permission"] = native.get("permission", []) + body["permission"]
        return {"id": "ses_original", **native}

    harness._transport = SimpleNamespace(request=request)
    original_config = harness._config
    await harness.update_authorization(ExecutionAuthorization(True))
    assert native["permission"][0] == {"permission": "*", "pattern": "*", "action": "allow"}
    harness._runtime_permissions = None  # Cold restore re-reads the native permission prefix.
    await harness.update_authorization(ExecutionAuthorization(False))
    assert native["permission"][-3]["action"] == "ask"
    assert harness._config is original_config
    harness._runtime_permissions = None
    harness._transport.request = AsyncMock(return_value={"id": "ses_wrong"})
    with pytest.raises(HarnessProtocolError, match="changed|confirm"):
        await harness.update_authorization(ExecutionAuthorization(True))
    assert harness._poisoned
    with pytest.raises(HarnessStateError):
        await harness.send(HarnessInput("denied"))
