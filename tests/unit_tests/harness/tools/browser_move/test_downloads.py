# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Chrome event ownership, completion receipts and cancellation confirmation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import web

from openjiuwen.harness.tools.browser_move.playwright_runtime.downloads import BrowserDownloads
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import BrowserAgentRuntime


@pytest_asyncio.fixture
async def cdp():
    state = SimpleNamespace(commands=[], socket=None, refuse_reset=False, acknowledge_cancel=True)

    async def version(request):
        return web.json_response({"webSocketDebuggerUrl": f"ws://{request.host}/cdp"})

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state.socket = ws
        async for message in ws:
            call = message.json()
            state.commands.append(call)
            if call["method"] == "Browser.cancelDownload" and state.acknowledge_cancel:
                await ws.send_json(
                    {
                        "method": "Browser.downloadProgress",
                        "params": {"guid": call["params"]["guid"], "state": "canceled"},
                    }
                )
            error = state.refuse_reset and call["params"].get("behavior") == "default"
            await ws.send_json({"id": call["id"], **({"error": {}} if error else {"result": {}})})
        return ws

    app = web.Application()
    app.router.add_get("/json/version", version)
    app.router.add_get("/cdp", socket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        yield state
    finally:
        for owner in tuple(BrowserDownloads._owners.values()):
            await owner.discard_after_exit()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_download_receipt_requires_terminal_file_and_is_reported_once(tmp_path, cdp):
    lease = BrowserDownloads(str(tmp_path / "out"), owner_key="chrome-a")
    await lease.start(cdp.endpoint)
    assert cdp.commands[0]["params"]["eventsEnabled"] is True
    await cdp.socket.send_json(
        {"method": "Browser.downloadWillBegin", "params": {"guid": "guid-1", "suggestedFilename": "../../report.txt"}}
    )
    await asyncio.sleep(0.02)
    (lease.root / "guid-1.crdownload").write_bytes(b"partial")
    with pytest.raises(TimeoutError):
        await lease.completed(grace_s=0, timeout_s=0.03)
    (lease.root / "guid-1.crdownload").unlink()
    (lease.root / "guid-1").write_bytes(b"finished")
    await cdp.socket.send_json(
        {"method": "Browser.downloadProgress", "params": {"guid": "guid-1", "state": "completed"}}
    )
    receipts = await lease.completed(grace_s=0.02)
    assert receipts == [
        {"name": "report.txt", "path": str(lease.root / "guid-1-report.txt"), "size": 8, "status": "completed"}
    ]
    assert await lease.completed(grace_s=0) == []
    await lease.close()
    assert cdp.commands[-1]["params"] == {"behavior": "default", "eventsEnabled": False}
    assert "chrome-a" not in BrowserDownloads._owners


@pytest.mark.asyncio
async def test_conflicting_task_cannot_reset_owner_and_close_cancels_guid(tmp_path, cdp):
    owner = BrowserDownloads(str(tmp_path / "out"), owner_key="chrome-a")
    await owner.start(cdp.endpoint)
    other = BrowserDownloads(str(tmp_path / "other"), owner_key="chrome-a")
    with pytest.raises(RuntimeError, match="already owned"):
        await other.start(cdp.endpoint)
    await other.close()
    assert BrowserDownloads._owners["chrome-a"] is owner
    await cdp.socket.send_json(
        {"method": "Browser.downloadWillBegin", "params": {"guid": "guid-1", "suggestedFilename": "x"}}
    )
    await asyncio.sleep(0.02)
    await owner.close()
    assert cdp.commands[-2]["method"] == "Browser.cancelDownload"
    assert "chrome-a" not in BrowserDownloads._owners


@pytest.mark.asyncio
async def test_reset_error_retains_lease_until_confirmed_browser_exit(tmp_path, cdp):
    lease = BrowserDownloads(str(tmp_path / "out"), owner_key="chrome-a")
    await lease.start(cdp.endpoint)
    cdp.refuse_reset = True
    with pytest.raises(RuntimeError, match="CDP command failed"):
        await lease.close()
    assert BrowserDownloads._owners["chrome-a"] is lease
    runtime = BrowserAgentRuntime.__new__(BrowserAgentRuntime)
    runtime._downloads = lease
    runtime._service = SimpleNamespace(
        reset=AsyncMock(side_effect=RuntimeError("exit unconfirmed")),
        release_task_binding=AsyncMock(return_value=False),
    )
    with pytest.raises(RuntimeError, match="exit unconfirmed"):
        await runtime.release_task_resources()
    assert BrowserDownloads._owners["chrome-a"] is lease
    runtime._service.release_task_binding.assert_not_awaited()
    runtime._service.reset.side_effect = None
    await runtime.release_task_resources()
    assert "chrome-a" not in BrowserDownloads._owners
    runtime._service.reset.assert_awaited_with(graceful=True)


@pytest.mark.asyncio
async def test_completed_symlink_is_rejected(tmp_path, cdp):
    lease = BrowserDownloads(str(tmp_path / "out"), owner_key="chrome-a")
    await lease.start(cdp.endpoint)
    external = tmp_path / "outside"
    external.write_text("private")
    (lease.root / "bad").symlink_to(external)
    lease._event("Browser.downloadWillBegin", {"guid": "bad", "suggestedFilename": "x"})
    lease._event("Browser.downloadProgress", {"guid": "bad", "state": "completed"})
    with pytest.raises(RuntimeError, match="finalized"):
        await lease.completed(grace_s=0)
    await lease.close()
    assert external.read_text() == "private"


@pytest.mark.asyncio
async def test_closed_event_connection_cannot_authorize_further_actions(tmp_path, cdp):
    lease = BrowserDownloads(str(tmp_path / "out"), owner_key="chrome-a")
    await lease.start(cdp.endpoint)
    lease.check_ready()
    await cdp.socket.close()
    await lease._reader
    with pytest.raises(ValueError, match="lease is not ready"):
        lease.check_ready()
    with pytest.raises(RuntimeError, match="event stream closed"):
        await lease.completed(grace_s=0)
    assert BrowserDownloads._owners["chrome-a"] is lease


@pytest.mark.asyncio
async def test_untracked_temporary_file_requires_browser_exit(tmp_path, cdp):
    lease = BrowserDownloads(str(tmp_path / "out"), owner_key="chrome-a")
    await lease.start(cdp.endpoint)
    (lease.root / "untracked.crdownload").write_bytes(b"unfinished")
    with pytest.raises(RuntimeError, match="exit is unconfirmed"):
        await lease.close()
    assert BrowserDownloads._owners["chrome-a"] is lease
