"""A failed controller stop must retain the same DeepAgent for retry."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError, build_error
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent


@pytest.mark.asyncio
@pytest.mark.parametrize("unbind_fails", [False, True])
async def test_controller_stop_failure_is_visible_and_same_owner_can_retry(monkeypatch, unbind_fails):
    agent = DeepAgent(AgentCard(name="stop-retry", description="synthetic"))
    controller = MagicMock()
    failure = build_error(StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR, error_msg="owned task exit unconfirmed")
    controller.stop = AsyncMock(side_effect=[failure, None])
    controller.unbind_session = AsyncMock(side_effect=failure if unbind_fails else None)
    agent._loop_controller = controller
    agent._interaction_session = MagicMock(close_stream=AsyncMock())
    agent._interaction_started = True
    monkeypatch.setattr(agent._interaction_output, "shutdown", AsyncMock())
    monkeypatch.setattr(agent, "_cancel_active_round", AsyncMock())
    with pytest.raises(BaseError, match="unconfirmed"):
        await agent.stop()
    assert agent._interaction_started and agent.loop_controller is controller
    await agent.stop()
    assert not agent._interaction_started and controller.stop.await_count == 2
