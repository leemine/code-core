# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import asyncio

import pytest

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness_protocol import (
    AbortMode,
    HarnessCapability,
    InteractionResponseStatus,
    OutputEvent,
    OutputKind,
    OutputOperation,
    UserInputRequest,
)
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter
from tests.unit_tests.harness_providers.test_io_adapter import _context, _drain, _FakeHarness


@pytest.mark.asyncio
async def test_output_capacity_failure_cancels_pending_and_keeps_stop_live():
    from openjiuwen.harness_providers.output_buffer import OutputBudgetExceeded, OutputLimits

    harness = _FakeHarness(capabilities=frozenset({HarnessCapability.GRACEFUL_ABORT}))
    adapter = HarnessIOAdapter(harness, output_limits=OutputLimits(max_items=1))
    await adapter.start(_context())
    pending = asyncio.create_task(adapter.handle(UserInputRequest(request_id="q", turn_id="t", prompt="continue?")))
    await asyncio.sleep(0)
    await harness.emit(
        OutputEvent(output_id="a", kind=OutputKind.TEXT, content="over capacity", operation=OutputOperation.DELTA)
    )
    response = await asyncio.wait_for(pending, 1)
    assert response.status is InteractionResponseStatus.CANCELLED
    await asyncio.wait_for(adapter.stop(), 1)
    outputs = adapter.output_envelopes()
    assert (await anext(outputs)).chunk.type == INTERACTION
    with pytest.raises(OutputBudgetExceeded):
        await anext(outputs)
    assert harness.aborts == [AbortMode.GRACEFUL]
    assert not adapter.pending_interrupt_ids
    assert not adapter._output_text


@pytest.mark.asyncio
async def test_full_memory_queue_does_not_block_answer_or_abort():
    from openjiuwen.harness_providers.output_buffer import OutputLimits

    harness = _FakeHarness(capabilities=frozenset({HarnessCapability.GRACEFUL_ABORT}))
    adapter = HarnessIOAdapter(harness, output_limits=OutputLimits(memory_bytes=1))
    await adapter.start(_context())
    pending = asyncio.create_task(adapter.handle(UserInputRequest(request_id="q", turn_id="t", prompt="continue?")))
    await asyncio.sleep(0)
    assert adapter._output_queue.budget.storage_bytes > 0
    answer = InteractiveInput()
    answer.update("q", "yes")
    await asyncio.wait_for(adapter.send(answer), 1)
    assert (await asyncio.wait_for(pending, 1)).status is InteractionResponseStatus.COMPLETED
    await asyncio.wait_for(adapter.abort(), 1)
    await adapter.stop()
    assert (await _drain(adapter))[0].type == INTERACTION
    assert adapter._output_queue.budget.storage_bytes == 0


def test_output_prefix_digest_preserves_unicode_snapshots_and_bounds_identifiers():
    from openjiuwen.harness_providers.output_buffer import OutputBudgetExceeded

    adapter = HarnessIOAdapter(_FakeHarness())
    prefix = "中文" * 200000
    delta = adapter._project_output(
        OutputEvent(output_id="a", kind=OutputKind.TEXT, content=prefix, operation=OutputOperation.DELTA)
    )
    assert delta.payload["content"] == prefix
    final = adapter._project_output(
        OutputEvent(output_id="a", kind=OutputKind.TEXT, content=prefix + "尾", operation=OutputOperation.FINAL)
    )
    assert final.payload["content"] == "尾"
    digest, chars = adapter._output_text[(None, "a")]
    assert len(digest.digest()) == 32 and chars == len(prefix) + 1
    assert (
        adapter._project_output(
            OutputEvent(output_id="a", kind=OutputKind.TEXT, content="replacement", operation=OutputOperation.FINAL)
        )
        is None
    )
    with pytest.raises(OutputBudgetExceeded, match="identifier"):
        adapter._project_output(OutputEvent(output_id="a" * 1025, kind=OutputKind.TEXT, content="x"))
