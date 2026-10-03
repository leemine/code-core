"""Mandatory authority cannot be bypassed by optional or remembered approval."""

from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, RejectResult
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security.permission_engine.host import ToolPermissionHost
from openjiuwen.harness.security.permission_engine.models import PermissionConfirmResponse


def call():
    return ToolCall(id="c", type="function", name="read_file", arguments='{"path":"marker.txt"}')


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [False, None, 1, "true", "error"])
async def test_mandatory_authority_fails_closed_with_allow_defaults(decision):
    async def authorize(_):
        if decision == "error":
            raise RuntimeError("authority unavailable")
        return decision

    async def optional(_):
        return ("approve",)

    rail = PermissionInterruptRail(
        config={"enabled": True, "defaults": {"*": "allow"}},
        host=ToolPermissionHost(authorize_tool=authorize, permission_scene_hook=optional),
    )
    result = await rail.resolve_interrupt(SimpleNamespace(session=None), call(), None, {"read_file": True})
    assert isinstance(result, RejectResult)


@pytest.mark.asyncio
async def test_legacy_optional_hook_error_remains_optional():
    async def optional(_):
        raise RuntimeError("optional hook unavailable")

    rail = PermissionInterruptRail(
        config={"enabled": True, "defaults": {"*": "allow"}},
        host=ToolPermissionHost(permission_scene_hook=optional),
    )
    assert isinstance(await rail.resolve_interrupt(SimpleNamespace(session=None), call(), None), ApproveResult)


@pytest.mark.asyncio
async def test_revocation_during_confirmation_denies_and_does_not_persist():
    allowed = True
    persisted = []

    async def authorize(_):
        return allowed

    async def confirm(_):
        nonlocal allowed
        allowed = False
        return PermissionConfirmResponse(approved=True, auto_confirm=True)

    rail = PermissionInterruptRail(
        config={"enabled": True, "tools": {"read_file": "ask"}},
        host=ToolPermissionHost(
            authorize_tool=authorize,
            request_permission_confirmation=confirm,
            persist_allow_rule=lambda cfg: persisted.append(cfg) or True,
        ),
    )
    assert isinstance(await rail.resolve_interrupt(SimpleNamespace(session=None), call(), None), RejectResult)
    assert not persisted


@pytest.mark.asyncio
async def test_resumed_approval_and_remembered_rules_do_not_override_authority():
    async def authorize(_):
        return False

    rail = PermissionInterruptRail(host=ToolPermissionHost(authorize_tool=authorize))
    result = await rail.resolve_interrupt(
        SimpleNamespace(session=None), call(), {"approved": True}, {"read_file": True}
    )
    assert isinstance(result, RejectResult)


@pytest.mark.asyncio
async def test_authority_allow_does_not_override_ordinary_deny():
    async def authorize(_):
        return True

    rail = PermissionInterruptRail(
        config={"enabled": True, "tools": {"read_file": "deny"}},
        host=ToolPermissionHost(authorize_tool=authorize),
    )
    assert isinstance(await rail.resolve_interrupt(SimpleNamespace(session=None), call(), None), RejectResult)
