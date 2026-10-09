"""Existing rail assembly defers host environment resolution until execution."""

from types import SimpleNamespace

import pytest

from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperation, SysOperationCard
from openjiuwen.harness.rails import SysOperationRail

pytestmark = pytest.mark.level0


@pytest.mark.asyncio
async def test_rail_registers_live_environment_without_reading_at_init(tmp_path):
    values = []
    allowed = True

    def environment():
        values.append("called")
        if not allowed:
            raise PermissionError("revoked synthetic credential")
        return {"AUDIT_CHILD_ENV": "rail-child-only"}

    operation = SysOperation(
        SysOperationCard(
            id="rail-shell-environment",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(shell_allowlist=None),
        )
    )
    rail = SysOperationRail(bash_environment_provider=environment)
    rail.set_sys_operation(operation)
    manager = AbilityManager(owner_id="rail-shell-environment")
    agent = SimpleNamespace(
        card=SimpleNamespace(id="rail-shell-environment"),
        system_prompt_builder=SimpleNamespace(language="en"),
        ability_manager=manager,
    )
    try:
        rail.init(agent)
        assert not values
        tool = next(tool for tool in rail.tools if tool.card.name == "bash")
        result = await tool.invoke({"command": 'printf "%s" "$AUDIT_CHILD_ENV"', "workdir": str(tmp_path)})
        assert result.success and "rail-child-only" in str(result.data)
        allowed = False
        result = await tool.invoke({"command": "echo forbidden", "workdir": str(tmp_path)})
        assert not result.success and result.error == "Shell environment unavailable."
        assert values == ["called", "called"]
    finally:
        rail.uninit(agent)
        manager.teardown_tools()
