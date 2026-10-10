# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Direct mode is explicit, owns only its child and never claims stale PIDs."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.harness_providers.opencode import OpenCodeHarnessConfig
from openjiuwen.harness_providers.opencode.errors import OpenCodeError
from openjiuwen.harness_providers.opencode.server import ManagedServer, _config_identity


def server():
    value = ManagedServer(OpenCodeHarnessConfig(server_mode="direct", shutdown_timeout_s=0.02), None)
    value.owner = {"generation": "owned"}
    value.read_owner = Mock(return_value=value.owner)
    value.process = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(), wait=AsyncMock(return_value=0))
    value.control = AsyncMock(side_effect=AssertionError("must not invoke systemd"))
    return value


def test_mode_default_and_legacy_identity():
    assert OpenCodeHarnessConfig().server_mode == "systemd"
    assert "server_mode" not in _config_identity(OpenCodeHarnessConfig())
    assert _config_identity(OpenCodeHarnessConfig(server_mode="direct"))["server_mode"] == "direct"


@pytest.mark.parametrize("mode", ["auto", "", None, True, []])
def test_no_implicit_fallback_mode(mode):
    with pytest.raises(ValueError):
        OpenCodeHarnessConfig(server_mode=mode)


@pytest.mark.asyncio
async def test_direct_reaps_only_exact_child():
    value = server()
    await value.reap(value.owner)
    value.process.terminate.assert_called_once()
    value.process.kill.assert_not_called()
    value.control.assert_not_called()


@pytest.mark.asyncio
async def test_direct_escalates_server_only_and_waits():
    value = server()
    value.process.wait.side_effect = [TimeoutError(), 0]
    await value.reap(value.owner)
    value.process.kill.assert_called_once()
    assert value.process.wait.await_count == 2


@pytest.mark.asyncio
async def test_direct_unconfirmed_exit_does_not_clear_owner():
    value = server()
    process = value.process
    value.process.wait.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await value.stop()
    assert value.process is process and value.owner == {"generation": "owned"}


@pytest.mark.asyncio
async def test_foreign_or_changed_generation_is_never_signalled():
    value = server()
    with pytest.raises(OpenCodeError, match="direct_owner_recovery_required"):
        await value.reap({"generation": "previous"})
    value.read_owner.return_value = {"generation": "changed"}
    with pytest.raises(OpenCodeError, match="direct_owner_recovery_required"):
        await value.reap(value.owner)
    value.process.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_spawn_retains_child_for_rollback(monkeypatch):
    value = server()
    child = value.process
    value.process = None
    value.cli, value.cwd, value.log = Path("/fixture/opencode"), "/fixture/work", None
    entered, release = asyncio.Event(), asyncio.Event()

    async def spawn(*args, **kwargs):
        assert args[:2] == ("/fixture/opencode", "serve")
        entered.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(value._start_direct(12345, {}))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert value.process is child
    await value.reap(value.owner)
    child.terminate.assert_called_once()
