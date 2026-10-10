# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native cancellation precedes service teardown; owned cleanup remains authoritative."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness_providers.opencode import OpenCodeHarness, OpenCodeHarnessConfig
from openjiuwen.harness_providers.opencode.errors import OpenCodeError


def make_harness(*, timeout=1):
    calls = []
    harness = OpenCodeHarness(OpenCodeHarnessConfig(shutdown_timeout_s=timeout))
    harness._session_id = "ses_owned"

    async def request(method, path):
        calls.append((method, path))
        return True if method == "POST" else {}

    async def close():
        calls.append("transport.close")
        transport.closed.set()

    async def stop():
        calls.append("server.stop")

    transport = SimpleNamespace(
        closed=asyncio.Event(), request=AsyncMock(side_effect=request), close=AsyncMock(side_effect=close)
    )
    server = SimpleNamespace(stop=AsyncMock(side_effect=stop))
    harness._transport, harness._server = transport, server
    return harness, transport, server, calls


@pytest.mark.asyncio
async def test_native_idle_before_transport_close_and_owned_service_stop():
    harness, transport, server, calls = make_harness()
    transport.request.side_effect = [True, {"ses_owned": {"type": "busy"}}, {"ses_owned": {"type": "idle"}}]
    await harness._close_session()
    assert [c.args for c in transport.request.await_args_list] == [
        ("POST", "/session/ses_owned/abort"),
        ("GET", "/session/status"),
        ("GET", "/session/status"),
    ]
    assert calls == ["transport.close", "server.stop"]
    server.stop.assert_awaited_once()
    assert harness._transport is harness._server is None


@pytest.mark.asyncio
async def test_busy_other_session_does_not_delay_owned_close():
    harness, transport, _, _ = make_harness()
    transport.request.side_effect = [True, {"ses_other": {"type": "busy"}}]
    await harness._close_session()
    assert transport.request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["request", "timeout", "abort_response", "status_response"])
async def test_native_failure_still_runs_owned_cleanup(failure):
    harness, transport, server, calls = make_harness(timeout=0.02)
    if failure == "request":
        transport.request.side_effect = OpenCodeError("http_transport_failed")
    elif failure == "timeout":

        async def never(*_):
            await asyncio.Event().wait()

        transport.request.side_effect = never
    else:
        transport.request.side_effect = [False] if failure == "abort_response" else [True, []]
    await harness._close_session()
    assert calls == ["transport.close", "server.stop"]
    server.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_unconfirmed_owned_exit_preserves_handles_for_retry():
    harness, transport, server, _ = make_harness()
    server.stop.side_effect = [OpenCodeError("owned_exit_unconfirmed"), None]
    with pytest.raises(OpenCodeError, match="owned_exit_unconfirmed"):
        await harness._close_session()
    assert harness._server is server and harness._transport is transport
    count = transport.request.await_count
    await harness._close_session()
    assert transport.request.await_count == count  # closed transport is not reused
    assert server.stop.await_count == 2
    assert harness._server is harness._transport is None


@pytest.mark.asyncio
async def test_transport_close_failure_does_not_skip_owned_cleanup():
    harness, transport, server, _ = make_harness()
    transport.close.side_effect = RuntimeError("transport close failed")
    with pytest.raises(RuntimeError, match="transport close failed"):
        await harness._close_session()
    server.stop.assert_awaited_once()
    assert harness._server is server and harness._transport is transport


@pytest.mark.asyncio
async def test_reader_failure_and_stop_share_one_teardown():
    harness, transport, server, _ = make_harness()
    entered, released = asyncio.Event(), asyncio.Event()

    async def stop():
        entered.set()
        await released.wait()

    server.stop.side_effect = stop
    first = asyncio.create_task(harness._close_session())
    await entered.wait()
    second = asyncio.create_task(harness._close_session())
    released.set()
    await asyncio.gather(first, second)
    server.stop.assert_awaited_once()
    transport.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_start_without_session_skips_native_request():
    harness, transport, server, _ = make_harness()
    harness._session_id = None
    await harness._close_session()
    transport.request.assert_not_called()
    server.stop.assert_awaited_once()
