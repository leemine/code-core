# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real Chrome download completion, cancellation and next-task isolation."""

import asyncio
import os
import shutil
import socket

import aiohttp
import pytest
from aiohttp import web

from openjiuwen.harness.tools.browser_move.drivers.managed_browser import ManagedBrowserDriver
from openjiuwen.harness.tools.browser_move.playwright_runtime.downloads import BrowserDownloads
from openjiuwen.harness.tools.browser_move.playwright_runtime.profiles import BrowserProfile

pytestmark = pytest.mark.skipif(os.getenv("RUN_BROWSER_DOWNLOAD_LOCAL") != "1", reason="explicit real Chrome opt-in")


@pytest.mark.asyncio
@pytest.mark.timeout(45)
async def test_pending_cancel_and_next_task_have_separate_completed_files(tmp_path):
    chrome = shutil.which("google-chrome")
    assert chrome, "Chrome is required for explicit opt-in"
    disconnected = asyncio.Event()

    async def download(request):
        if request.path == "/complete":
            return web.Response(
                body=b"R1-10F DOWNLOAD", headers={"Content-Disposition": "attachment; filename=result.txt"}
            )
        response = web.StreamResponse(
            headers={"Content-Disposition": "attachment; filename=slow.bin", "Content-Length": str(1024 * 1024 * 50)}
        )
        await response.prepare(request)
        try:
            for _ in range(800):
                await response.write(b"x" * 65536)
                await asyncio.sleep(0.03)
        except (ConnectionResetError, aiohttp.ClientConnectionError):
            disconnected.set()
        return response

    app = web.Application()
    app.router.add_get("/{name}", download)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    driver = ManagedBrowserDriver(
        BrowserProfile(
            name="download-test",
            driver_type="managed",
            debug_port=port,
            user_data_dir=str(tmp_path / "profile"),
            browser_binary=chrome,
            extra_args=["--headless=new"],
        )
    )
    leases = []
    try:
        endpoint = await asyncio.to_thread(driver.start)
        async with aiohttp.ClientSession(trust_env=False) as client:
            first = BrowserDownloads(str(tmp_path / "outputs"), owner_key=str(tmp_path / "profile"))
            leases.append(first)
            await first.start(endpoint)
            async with client.put(endpoint + "/json/new?" + origin + "/slow") as response:
                response.raise_for_status()
            async with asyncio.timeout(10):
                while not first._pending:  # noqa: ASYNC110 - bounded CDP event observation
                    await asyncio.sleep(0.05)
            await first.close()
            await asyncio.wait_for(disconnected.wait(), 3)
            assert first._completed == []
            assert driver.owns_process
            second = BrowserDownloads(str(tmp_path / "outputs"), owner_key=str(tmp_path / "profile"))
            leases.append(second)
            await second.start(endpoint)
            async with client.put(endpoint + "/json/new?" + origin + "/complete") as response:
                response.raise_for_status()
            async with asyncio.timeout(10):
                receipts = []
                while not receipts:
                    receipts = await second.completed(grace_s=0.1)
            assert len(receipts) == 1
            from pathlib import Path

            assert await asyncio.to_thread(Path(receipts[0]["path"]).read_bytes) == b"R1-10F DOWNLOAD"
            assert first.root != second.root
            await second.close()
    finally:
        await driver.stop_gracefully()
        for lease in leases:
            await lease.discard_after_exit()
        await server.cleanup()
    assert not driver.owns_process
    assert not BrowserDownloads._owners
