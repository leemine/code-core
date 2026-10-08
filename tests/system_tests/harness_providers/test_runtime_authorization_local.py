# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fixed real CLIs with loopback model fixtures and isolated user directories."""

import asyncio
from dataclasses import replace

import pytest

from openjiuwen.harness_protocol import (
    ExecutionAuthorization,
    HarnessInput,
    HostCapability,
    ToolApprovalDecision,
    ToolApprovalResponse,
    TurnEventKind,
)
from openjiuwen.harness_providers.codex import CodexHarness
from tests.system_tests.harness_providers._codex_response_fixture import ResponsesFixture
from tests.system_tests.harness_providers._contract import collect_turn, make_context, terminal_of
from tests.system_tests.harness_providers.test_codex_read_roots_local import _context
from tests.system_tests.harness_providers.test_codex_read_roots_local import scope as scope
from tests.system_tests.harness_providers.test_codex_source_policy_local import _restricted_config
from tests.system_tests.harness_providers.test_opencode_e2e import runtime as runtime


class Approvals:
    def __init__(self):
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        return ToolApprovalResponse(request_id=request.request_id, decision=ToolApprovalDecision.ALLOW_FOR_SESSION)

    async def cancel(self, request_id, *, reason=None):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_full", [False, True])
async def test_real_opencode_same_session_default_full_default(runtime, initial_full):
    cfg, model, work, create = runtime
    h = create(replace(cfg, full_access=initial_full))
    approvals = Approvals()
    await h.start(
        make_context(
            cwd=str(work),
            interactions=approvals,
            host_capabilities=frozenset() if initial_full else frozenset({HostCapability.TOOL_APPROVAL}),
        )
    )
    session_id = h.provider_session_id
    for full_access in (initial_full, not initial_full, initial_full):
        await h.update_authorization(ExecutionAuthorization(full_access))
        before = len(approvals.requests)
        model.actions = [
            {"tool": "bash", "args": {"command": "printf PERMISSION-MARKER", "description": "permission probe"}},
            {"text": "DONE"},
        ]
        receipt = await h.send(HarnessInput("Run the fixture tool."))
        terminal = terminal_of(await collect_turn(h, receipt.turn_id))
        assert terminal.kind is TurnEventKind.FINISHED, terminal.result
        assert h.provider_session_id == session_id
        assert (len(approvals.requests) > before) is (not full_access)
    server, owner = h._server, dict(h._server.owner)
    await h.stop()
    assert (await server.properties(owner)).get("ActiveState") != "active"
    assert not (server.scope / "owner.json").exists()


@pytest.mark.asyncio
async def test_real_codex_same_thread_default_full_default(scope):
    # Explicit runtime authorization disallows ambient named permission overrides.
    (scope / "codex/config.toml").write_text('default_permissions = ":read-only"\n')
    with ResponsesFixture() as model:
        h = CodexHarness(_restricted_config(scope, model))
        processes = []
        try:
            await h.start(_context(scope))
            original = h.provider_session_id
            for full_access in (None, True, False):
                if full_access is not None:
                    await asyncio.wait_for(h.update_authorization(ExecutionAuthorization(full_access)), 25)
                processes.append(h._client._client._sync._proc)
                receipt = await h.send(HarnessInput("Return fixed marker."))
                terminal = terminal_of(await collect_turn(h, receipt.turn_id))
                assert terminal.kind is TurnEventKind.FINISHED, terminal.result
                assert h.provider_session_id == original
        finally:
            await h.stop()
        assert all(process.poll() is not None for process in processes)
        assert len(model.requests) == 3
