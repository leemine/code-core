# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native steer acceptance, single-reader completion and exact queue receipts."""
import asyncio

import pytest

from openjiuwen.harness_protocol import DeliveryMode, HarnessInput, HarnessStateError, TurnEventKind
from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig
from tests.unit_tests.harness_providers.test_codex import (
    _FakeHandle, _Status, _context, _install_fake_sdk, _terminal, _turn, _turn_completed,
)


class _RejectedSteer(Exception):
    code = -32600
    message = "no active turn to steer"


@pytest.mark.asyncio
@pytest.mark.parametrize("early", [True, False])
async def test_explicit_nonacceptance_queues_once_without_failing_original(monkeypatch, early):
    _, state = _install_fake_sdk(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(handle):
        entered.set()
        await release.wait()
        return _turn_completed(handle.id, _Status.completed)

    async def reject(handle, text):
        raise _RejectedSteer()

    monkeypatch.setattr(_FakeHandle, "steer", reject)
    state.scripts = [[complete], [_turn_completed("turn-follow", _Status.completed)]]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    if early:
        state.turn_gate.clear()
    first = await harness.send(HarnessInput(content="first"))
    consumer = asyncio.create_task(_turn(harness, first.turn_id))
    if early:
        await asyncio.sleep(.01)
    else:
        await asyncio.wait_for(entered.wait(), 2)
    delivery = asyncio.create_task(harness.send(HarnessInput(content="follow"), mode=DeliveryMode.STEER))
    await asyncio.sleep(0)
    if early:
        assert not delivery.done()
        state.turn_gate.set()
    receipt = await asyncio.wait_for(delivery, 2)
    assert receipt.accepted_mode is DeliveryMode.FOLLOW_UP
    assert receipt.turn_id != first.turn_id
    release.set()
    assert _terminal(await asyncio.wait_for(consumer, 2)).kind is TurnEventKind.FINISHED
    assert _terminal(await asyncio.wait_for(_turn(harness, receipt.turn_id), 2)).kind is TurnEventKind.FINISHED
    assert [handle.id for handle in state.handles] == ["turn-first", "turn-follow"]
    assert all(handle.interrupts == 0 for handle in state.handles)
    await harness.stop()


@pytest.mark.asyncio
async def test_cancelled_early_steer_is_not_delivered(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    state.turn_gate.clear()
    state.scripts = [[_turn_completed("turn-first", _Status.completed)]]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    first = await harness.send(HarnessInput(content="first"))
    consumer = asyncio.create_task(_turn(harness, first.turn_id))
    await asyncio.sleep(.01)
    delivery = asyncio.create_task(harness.send(HarnessInput(content="cancelled"), mode=DeliveryMode.STEER))
    await asyncio.sleep(0)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery
    state.turn_gate.set()
    assert _terminal(await asyncio.wait_for(consumer, 2)).kind is TurnEventKind.FINISHED
    assert len(state.handles) == 1 and state.handles[0].steers == []
    await harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_start_failure_or_stop_releases_early_steer_without_replay(monkeypatch, stop):
    _, state = _install_fake_sdk(monkeypatch)
    state.turn_gate.clear()
    state.scripts = [[_turn_completed("turn-first", _Status.interrupted)]]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    entered = asyncio.Event()
    original = harness._thread.turn

    async def start(text):
        entered.set()
        if stop:
            return await original(text)
        await state.turn_gate.wait()
        raise RuntimeError("SDK start failed")

    monkeypatch.setattr(harness._thread, "turn", start)
    first = await harness.send(HarnessInput(content="first"))
    consumer = asyncio.create_task(_turn(harness, first.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    delivery = asyncio.create_task(harness.send(HarnessInput(content="follow"), mode=DeliveryMode.STEER))
    await asyncio.sleep(0)
    stopping = asyncio.create_task(harness.stop()) if stop else None
    if stop:
        await asyncio.sleep(.01)
    state.turn_gate.set()
    with pytest.raises(HarnessStateError):
        await asyncio.wait_for(delivery, 2)
    terminal = _terminal(await asyncio.wait_for(consumer, 2))
    assert terminal.kind is (TurnEventKind.ABORTED if stop else TurnEventKind.FAILED)
    assert not any(handle.steers for handle in state.handles)
    if stopping:
        await asyncio.wait_for(stopping, 2)
    else:
        # Failed native startup deliberately retains unknown exit ownership.
        assert len(state.handles) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["unknown", "wrong-code"])
async def test_ambiguous_active_steer_never_replays(monkeypatch, error_kind):
    _, state = _install_fake_sdk(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    error = RuntimeError("steer response lost")
    if error_kind == "wrong-code":
        error = _RejectedSteer()
        error.code = -32603

    async def complete(handle):
        entered.set()
        await release.wait()
        return _turn_completed(handle.id, _Status.completed)

    async def reject(handle, text):
        raise error

    state.scripts = [[complete]]
    monkeypatch.setattr(_FakeHandle, "steer", reject)
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    first = await harness.send(HarnessInput(content="first"))
    consumer = asyncio.create_task(_turn(harness, first.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(type(error)) as caught:
        await harness.send(HarnessInput(content="uncertain"), mode=DeliveryMode.STEER)
    assert caught.value is error
    release.set()
    assert _terminal(await asyncio.wait_for(consumer, 2)).kind is TurnEventKind.FINISHED
    await harness.stop()
    assert len(state.handles) == 1
