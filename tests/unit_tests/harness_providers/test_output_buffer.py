# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Capacity, lossless overflow, and control independence of output staging."""

import asyncio

import pytest

from openjiuwen.harness_providers.output_buffer import (
    OutputBudget,
    OutputBudgetExceeded,
    OutputBuffer,
    OutputLimits,
    OutputText,
)


def test_spool_preserves_large_structured_result_and_reclaims_budget():
    budget = OutputBudget(OutputLimits(memory_bytes=64, storage_bytes=10000, item_bytes=9000))
    queue = OutputBuffer(budget, memory_items=1)
    values = [{"result": "中文" * 500}, {"question": "approve?"}, {"terminal": "finished"}]
    for value in values:
        queue.put_nowait(value)
    assert budget.memory_bytes <= 64
    assert 0 < budget.storage_bytes <= 10000
    assert [queue.get_nowait() for _ in values] == values
    assert (budget.items, budget.memory_bytes, budget.storage_bytes) == (0, 0, 0)
    assert queue._file is None


def test_shared_budget_covers_unclaimed_mailbox_and_handoff():
    budget = OutputBudget(OutputLimits(max_items=2))
    left, right = OutputBuffer(budget), OutputBuffer(budget)
    left.put_nowait("a")
    right.put_nowait("b")
    with pytest.raises(OutputBudgetExceeded, match="count"):
        right.put_nowait("c")
    right.put_nowait(left.get_nowait())
    assert [right.get_nowait(), right.get_nowait()] == ["b", "a"]
    assert budget.items == 0


@pytest.mark.parametrize(
    "limits,value,match",
    [
        (OutputLimits(item_bytes=10), "x" * 100, "single"),
        (OutputLimits(memory_bytes=1, storage_bytes=10), "x" * 100, "spool"),
    ],
)
def test_rejection_is_explicit_and_does_not_charge_or_corrupt_queue(limits, value, match):
    buffer = OutputBuffer(OutputBudget(limits))
    with pytest.raises(OutputBudgetExceeded, match=match):
        buffer.put_nowait(value)
    assert buffer.empty()
    assert buffer.budget.items == 0
    buffer.close()
    assert buffer.budget.storage_bytes == 0


@pytest.mark.asyncio
async def test_failure_and_eof_wake_waiters_without_queue_slots():
    buffer = OutputBuffer()
    waiter = asyncio.create_task(buffer.get())
    await asyncio.sleep(0)
    buffer.fail(OutputBudgetExceeded("full"))
    with pytest.raises(OutputBudgetExceeded):
        await asyncio.wait_for(waiter, 1)
    buffer = OutputBuffer()
    buffer.put_nowait("accepted")
    buffer.fail(OutputBudgetExceeded("full"))
    assert await buffer.get() == "accepted"
    with pytest.raises(OutputBudgetExceeded):
        await buffer.get()
    buffer = OutputBuffer()
    buffer.finish()
    with pytest.raises(EOFError):
        await buffer.get()


def test_text_spills_unicode_without_truncation_and_replace_is_atomic_on_limit():
    budget = OutputBudget(OutputLimits(storage_bytes=1000))
    text = OutputText(memory_bytes=8, max_bytes=900, budget=budget)
    text.append("中文" * 100)
    assert text._file._rolled
    assert text.read() == "中文" * 100
    with pytest.raises(OutputBudgetExceeded):
        text.replace("x" * 1001)
    assert text.read() == "中文" * 100
    text.replace("done")
    assert budget.storage_bytes == 4
    text.close()
    text.close()
    assert budget.storage_bytes == 0


def test_text_total_budget_shared_across_turns():
    budget = OutputBudget(OutputLimits(storage_bytes=12))
    left, right = OutputText(budget=budget), OutputText(budget=budget)
    left.append("a" * 8)
    with pytest.raises(OutputBudgetExceeded, match="total"):
        right.append("b" * 8)
    right.append("b" * 4)
    left.close()
    right.append("c" * 8)
    assert right.read() == "bbbbcccccccc"
    right.close()
    assert budget.storage_bytes == 0


def test_disk_write_failure_is_explicit(monkeypatch):
    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("openjiuwen.harness_providers.output_buffer.tempfile.TemporaryFile", fail)
    buffer = OutputBuffer(OutputBudget(OutputLimits(memory_bytes=1)))
    with pytest.raises(OutputBudgetExceeded, match="write failed"):
        buffer.put_nowait("result")
    assert buffer.budget.items == 0
    buffer.close()


def test_text_short_read_is_not_reported_as_complete_output():
    text = OutputText(memory_bytes=1)
    text.append("complete result")
    text._file.truncate(3)
    try:
        with pytest.raises(OutputBudgetExceeded, match="read failed"):
            text.read()
    finally:
        text.close()
