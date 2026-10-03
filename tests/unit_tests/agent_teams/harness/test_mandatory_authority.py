"""Do not inherit a mandatory security capability without its startup wiring."""

import pytest

from openjiuwen.agent_teams.harness import create_native_harness_protocol
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.schema.extension_spec import AgentTemplateSpec
from openjiuwen.harness_protocol import HarnessContext, UnsupportedHarnessCapabilityError


@pytest.mark.asyncio
async def test_mandatory_authority_rejected_before_allocation_and_legacy_still_builds(monkeypatch):
    adapter = create_native_harness_protocol(AgentTemplateSpec(agent_card=AgentCard(id="a", name="a")))
    allocations = []
    authority_calls = []

    def allocate(context):
        allocations.append(context)
        raise RuntimeError("test-only allocation reached")

    async def authorize(request):
        authority_calls.append(request)
        return True

    monkeypatch.setattr(adapter, "_build_native", allocate)
    values = {"system_prompt": "", "agent_name": "a", "agent_id": "a", "host_session_id": "s"}
    with pytest.raises(UnsupportedHarnessCapabilityError, match="mandatory tool authorization"):
        await adapter.start(HarnessContext(**values, tool_authorizer=authorize))
    assert allocations == authority_calls == []
    assert adapter.native_harness is None
    assert adapter._agent_session is None
    assert not adapter._cycle_started and not adapter._cleanup_pending
    await adapter.stop()

    with pytest.raises(RuntimeError, match="test-only allocation reached"):
        await adapter.start(HarnessContext(**values))
    assert len(allocations) == 1
    assert allocations[0].tool_authorizer is None
    assert not adapter._cycle_started and not adapter._cleanup_pending
    await adapter.stop()
