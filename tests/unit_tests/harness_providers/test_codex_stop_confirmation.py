# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Codex must retain its server until the original native reader drains."""

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.harness_protocol import DeliveryMode, HarnessInput, HarnessProtocolError, TurnEventKind
from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig
from tests.unit_tests.harness_providers.test_codex import (
    _auth_failure,
    _context,
    _fallback_config,
    _install_fake_sdk,
    _Status,
    _terminal,
    _turn,
    _turn_completed,
)


@pytest.mark.asyncio
async def test_stop_keeps_server_alive_after_interrupt_ack_until_native_terminal(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    entered = asyncio.Event()
    terminal_ready = asyncio.Event()

    async def delayed_terminal(handle):
        entered.set()
        await terminal_ready.wait()
        return _turn_completed(handle.id, _Status.interrupted)

    state.scripts = [[delayed_terminal]]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    receipt = await harness.send(HarnessInput(content="slow-owned-tool"))
    consumer = asyncio.create_task(_turn(harness, receipt.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    acknowledged = asyncio.Event()
    original_interrupt = state.handles[0].interrupt

    async def interrupt():
        await original_interrupt()
        acknowledged.set()

    monkeypatch.setattr(state.handles[0], "interrupt", interrupt)
    stopping = asyncio.create_task(harness.stop())
    try:
        await asyncio.wait_for(acknowledged.wait(), 2)
        # The RPC acknowledgment has arrived, while the native tool cleanup
        # and its turn/completed notification have deliberately not arrived.
        await asyncio.sleep(0)
        assert not state.clients[0].closed
        assert not stopping.done()
    finally:
        terminal_ready.set()
        await asyncio.wait_for(stopping, 2)
        events = await asyncio.wait_for(consumer, 2)
    assert _terminal(events).kind is TurnEventKind.ABORTED
    assert state.clients[0].closed


@pytest.mark.asyncio
async def test_stop_timeout_retains_reader_and_retry_uses_its_late_terminal(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    monkeypatch.setattr("openjiuwen.harness_providers.codex.harness._DRAIN_TIMEOUT_S", 0.02)
    entered, terminal_ready = asyncio.Event(), asyncio.Event()

    async def delayed_terminal(handle):
        entered.set()
        await terminal_ready.wait()
        return _turn_completed(handle.id, _Status.interrupted)

    state.scripts = [[delayed_terminal]]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    receipt = await harness.send(HarnessInput(content="held"))
    consumer = asyncio.create_task(_turn(harness, receipt.turn_id))
    await entered.wait()
    owner = harness._native_turn
    with pytest.raises(TimeoutError):
        await harness.stop()
    assert harness._native_turn is owner
    assert owner.reader is not None and not owner.reader.done()
    assert not state.clients[0].closed
    terminal_ready.set()
    assert _terminal(await asyncio.wait_for(consumer, 2)).kind is TurnEventKind.ABORTED
    await asyncio.wait_for(harness.stop(), 2)
    assert state.clients[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("script", [[], [_turn_completed("unrelated-native-turn", _Status.completed)]])
async def test_eof_or_other_turn_terminal_cannot_confirm_exit(monkeypatch, script):
    _, state = _install_fake_sdk(monkeypatch)
    state.scripts = [script]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    receipt = await harness.send(HarnessInput(content="owned"))
    assert _terminal(await _turn(harness, receipt.turn_id)).kind is TurnEventKind.FAILED
    with pytest.raises(HarnessProtocolError, match="unconfirmed"):
        await harness.stop()
    assert not state.clients[0].closed
    assert harness._native_turn is not None and not harness._native_turn.confirmed


@pytest.mark.asyncio
async def test_stop_waits_for_outstanding_start_then_drains_same_turn(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    state.turn_gate.clear()
    state.scripts = [[_turn_completed("turn-starting", _Status.interrupted)]]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    entered = asyncio.Event()
    original_turn = harness._thread.turn

    async def start(text):
        entered.set()
        return await original_turn(text)

    monkeypatch.setattr(harness._thread, "turn", start)
    receipt = await harness.send(HarnessInput(content="starting"))
    consumer = asyncio.create_task(_turn(harness, receipt.turn_id))
    await entered.wait()
    stopping = asyncio.create_task(harness.stop())
    await asyncio.sleep(0)
    assert not stopping.done() and not state.clients[0].closed
    state.turn_gate.set()
    await asyncio.wait_for(stopping, 2)
    assert _terminal(await consumer).kind is TurnEventKind.ABORTED
    assert len(state.handles) == 1 and state.clients[0].closed


@pytest.mark.asyncio
async def test_idle_retry_drains_original_read_without_cancelling_it(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    cancelled = []

    async def silent_until_interrupt(handle):
        handle.release.clear()
        try:
            await handle.release.wait()
        except asyncio.CancelledError:
            cancelled.append(handle.id)
            raise
        return _turn_completed(handle.id, _Status.interrupted)

    state.scripts = [[silent_until_interrupt], [_turn_completed("turn-idle", _Status.completed)]]
    harness = CodexHarness(CodexHarnessConfig(
        inherit_process_env=False, turn_idle_timeout_s=0.02, turn_idle_retries=1,
    ))
    await harness.start(_context())
    receipt = await harness.send(HarnessInput(content="idle"))
    assert _terminal(await _turn(harness, receipt.turn_id)).kind is TurnEventKind.FINISHED
    assert len(state.handles) == 2 and not cancelled
    await harness.stop()


@pytest.mark.asyncio
async def test_sdk_close_retry_keeps_process_handle_until_wait_confirms_exit(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    calls = []

    def wait(*, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError("owned process is still alive")
        return 0

    process = SimpleNamespace(wait=wait)
    transport = SimpleNamespace(_proc=process)
    client = state.clients[0]
    client._client = SimpleNamespace(_sync=transport)
    original_close = client.close

    async def sdk_close():
        transport._proc = None
        await original_close()

    monkeypatch.setattr(client, "close", sdk_close)
    with pytest.raises(TimeoutError):
        await harness.stop()
    assert harness._client is client and harness._closing_process is process
    await harness.stop()
    assert calls == [2, 2] and harness._closing_process is None


@pytest.mark.asyncio
async def test_stop_during_fallback_connection_closes_late_client_without_sending(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    state.scripts = [[_auth_failure("turn-auth")]]
    harness = CodexHarness(_fallback_config())
    await harness.start(_context())
    entered, release = asyncio.Event(), asyncio.Event()
    original = harness._connect_session

    async def connect(context, *, model, resume_thread_id):
        entered.set()
        await release.wait()
        await original(context, model=model, resume_thread_id=resume_thread_id)

    monkeypatch.setattr(harness, "_connect_session", connect)
    receipt = await harness.send(HarnessInput(content="auth"))
    consumer = asyncio.create_task(_turn(harness, receipt.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    stop_entered = asyncio.Event()
    original_close = harness._close_session

    async def close():
        stop_entered.set()
        await original_close()

    monkeypatch.setattr(harness, "_close_session", close)
    stopping = asyncio.create_task(harness.stop())
    await asyncio.wait_for(stop_entered.wait(), 2)
    assert harness.active_turn.stop_requested
    release.set()
    await asyncio.wait_for(stopping, 2)
    assert _terminal(await consumer).kind is TurnEventKind.ABORTED
    assert len(state.clients) == 2 and all(client.closed for client in state.clients)
    assert len(state.handles) == 1
    assert harness._client is None and harness._thread is None
    assert not harness.fallback_activated


@pytest.mark.asyncio
async def test_pending_steer_failure_still_drains_original_native_reader(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    state.turn_gate.clear()
    state.scripts = [[_turn_completed("turn-steered", _Status.completed)]]
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    entered = asyncio.Event()
    original = harness._thread.turn

    async def start(text):
        entered.set()
        return await original(text)

    async def reject(handle, text):
        raise RuntimeError("native turn already ended before steer")

    monkeypatch.setattr(harness._thread, "turn", start)
    monkeypatch.setattr(harness, "_steer_handle", reject)
    receipt = await harness.send(HarnessInput(content="steered"))
    consumer = asyncio.create_task(_turn(harness, receipt.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    delivery = asyncio.create_task(harness.send(HarnessInput(content="late steer"), mode=DeliveryMode.STEER))
    await asyncio.sleep(0)
    state.turn_gate.set()
    with pytest.raises(RuntimeError, match="native turn already ended"):
        await asyncio.wait_for(delivery, 2)
    assert _terminal(await asyncio.wait_for(consumer, 2)).kind is TurnEventKind.FAILED
    assert harness._native_turn.confirmed
    await asyncio.wait_for(harness.stop(), 2)
    assert len(state.handles) == 1 and state.clients[0].closed


@pytest.mark.asyncio
async def test_concurrent_session_cleanup_closes_exact_client_once(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    original = state.clients[0].close

    async def close():
        calls.append(1)
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(state.clients[0], "close", close)
    first = asyncio.create_task(harness._close_session())
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(harness._close_session())
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    await harness.stop()
    assert calls == [1] and state.clients[0].closed


@pytest.mark.asyncio
async def test_late_connection_close_failure_blocks_stop_until_same_owner_exits(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    state.scripts = [[_auth_failure("turn-auth")]]
    harness = CodexHarness(_fallback_config())
    await harness.start(_context())
    entered, release, stop_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_connect = harness._connect_session
    original_close = harness._close_session
    failed = True

    async def connect(context, *, model, resume_thread_id):
        entered.set()
        await release.wait()
        await original_connect(context, model=model, resume_thread_id=resume_thread_id)

    async def close():
        stop_entered.set()
        await original_close()

    original_sdk_close = type(state.clients[0]).close

    async def sdk_close(client):
        if client is not state.clients[0] and failed:
            raise TimeoutError("late client still owns a live process")
        await original_sdk_close(client)

    monkeypatch.setattr(harness, "_connect_session", connect)
    monkeypatch.setattr(type(state.clients[0]), "close", sdk_close)
    receipt = await harness.send(HarnessInput(content="auth"))
    consumer = asyncio.create_task(_turn(harness, receipt.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    monkeypatch.setattr(harness, "_close_session", close)
    stopping = asyncio.create_task(harness.stop())
    await asyncio.wait_for(stop_entered.wait(), 2)
    release.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(stopping, 2)
    assert len(state.clients) == 2
    assert harness._closing_client is state.clients[1] and not state.clients[1].closed
    assert _terminal(await consumer).kind is TurnEventKind.ABORTED
    failed = False
    await asyncio.wait_for(harness.stop(), 2)
    assert all(client.closed for client in state.clients)
    assert harness._closing_client is None and len(state.handles) == 1


@pytest.mark.asyncio
async def test_killed_scope_wrapper_does_not_prove_tool_tree_cleanup(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context())
    process = SimpleNamespace(wait=lambda **kwargs: -9)
    state.clients[0]._client = SimpleNamespace(
        _sync=SimpleNamespace(_proc=process, _jiuwen_process_scope=True),
    )
    with pytest.raises(HarnessProtocolError, match="process tree exit is unconfirmed"):
        await harness.stop()
    assert harness._client is state.clients[0] and harness._closing_process is process
    with pytest.raises(HarnessProtocolError, match="process tree exit is unconfirmed"):
        await harness.stop()
