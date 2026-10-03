"""Current authority checks in external approval routes."""

from types import SimpleNamespace

import pytest

from openjiuwen.harness_protocol import (
    HarnessContext,
    ToolApprovalDecision,
    ToolApprovalResponse,
    UnsupportedHarnessCapabilityError,
)
from openjiuwen.harness_providers.codex import CodexHarness


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,params,key,denial",
    [
        ("item/commandExecution/requestApproval", {}, "decision", "decline"),
        ("item/fileChange/requestApproval", {}, "decision", "decline"),
        ("mcpServer/elicitation/request", {"serverName": "fixture"}, "action", "decline"),
    ],
)
@pytest.mark.parametrize("after", [False, "error", None, 1])
async def test_codex_rechecks_authority_after_approval(method, params, key, denial, after):
    allowed = True
    seen = []

    async def authorize(request):
        seen.append(request)
        if allowed == "error":
            raise RuntimeError("authority unavailable")
        return allowed

    async def approval(request):
        nonlocal allowed
        allowed = after
        return ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW_FOR_SESSION)

    harness = CodexHarness()
    harness._context = HarnessContext("a", "a", "s", "", tool_authorizer=authorize)
    harness._request_interaction = approval
    assert (await harness._route_approval(method, params))[key] == denial
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_codex_revoked_authority_does_not_open_approval():
    async def authorize(_):
        return False

    async def unexpected(_):
        pytest.fail("revoked authority must not request ordinary approval")

    harness = CodexHarness()
    harness._context = HarnessContext("a", "a", "s", "", tool_authorizer=authorize)
    harness._request_interaction = unexpected
    assert await harness._route_approval("item/fileChange/requestApproval", {}) == {"decision": "decline"}


@pytest.mark.asyncio
async def test_codex_late_allow_after_abort_is_declined():
    async def approval(request):
        harness._active_turn.abort_requested = True
        return ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW)

    harness = CodexHarness()
    harness._context = HarnessContext("a", "a", "s", "")
    harness._active_turn = SimpleNamespace(turn_id="t", abort_requested=False, stop_requested=False)
    harness._request_interaction = approval
    assert await harness._route_approval("item/fileChange/requestApproval", {}) == {"decision": "decline"}


def test_codex_rejects_mandatory_authorization_before_process_allocation():
    async def authorize(_):
        return False

    harness = CodexHarness()
    with pytest.raises(UnsupportedHarnessCapabilityError, match="view_image"):
        harness._validate_context(HarnessContext("a", "a", "s", "", tool_authorizer=authorize))
    assert harness._client is None
