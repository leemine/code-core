# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exclusive, task-scoped downloads for a host-managed Chrome CDP connection."""

from __future__ import annotations

import asyncio
import json
import re
import stat
import uuid
from pathlib import Path
from typing import ClassVar

import aiohttp


class BrowserDownloads:
    """Own Chrome's download behavior until cancellation/reset is confirmed.

    CDP attachment with noDefaults does not enable Playwright download events.
    This connection owns its events and files; it never uses another task's
    Chrome downloads or treats a temporary file as a completed result.
    """

    _owners: ClassVar[dict[str, "BrowserDownloads"]] = {}

    def __init__(self, root: str, *, owner_key: str):
        supplied = Path(root).absolute()
        if supplied.resolve() != supplied:
            raise ValueError("browser download root must not contain symlinks")
        self.root = supplied / uuid.uuid4().hex
        self.owner_key = owner_key
        self._client = None
        self._socket = None
        self._reader = None
        self._commands = {}
        self._serial = 0
        self._pending = {}
        self._completed = []
        self._reported = 0
        self._failure = None
        self._closing = False

    async def start(self, endpoint: str):
        """Claim this profile and enable events on an owned CDP connection."""
        if self._socket is not None:
            return
        previous = self._owners.get(self.owner_key)
        if previous is not None and previous is not self:
            raise RuntimeError("browser download behavior is already owned")
        self._owners[self.owner_key] = self
        self.root.mkdir(parents=True, mode=0o700)
        if self.root.resolve() != self.root:
            raise ValueError("browser download root changed")
        self._client = aiohttp.ClientSession(trust_env=False)
        # Retain ownership if a lost reply leaves Chrome configuration unknown.
        async with asyncio.timeout(10):
            async with self._client.get(endpoint.rstrip("/") + "/json/version") as response:
                response.raise_for_status()
                version = await response.json()
            self._socket = await self._client.ws_connect(version["webSocketDebuggerUrl"])
            self._reader = asyncio.create_task(self._receive())
            await self._command(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allowAndName",
                    "downloadPath": str(self.root),
                    "eventsEnabled": True,
                },
            )

    async def _command(self, method, params):
        if self._reader is not None and self._reader.done():
            raise RuntimeError("browser download connection closed")
        self._serial += 1
        serial = self._serial
        future = asyncio.get_running_loop().create_future()
        self._commands[serial] = future
        try:
            await self._socket.send_json({"id": serial, "method": method, "params": params})
            async with asyncio.timeout(5):
                return await future
        finally:
            self._commands.pop(serial, None)

    async def _receive(self):
        try:
            async for message in self._socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                if "id" in event:
                    future = self._commands.get(event["id"])
                    if future is not None and not future.done():
                        if "error" in event:
                            future.set_exception(RuntimeError("browser download CDP command failed"))
                        else:
                            future.set_result(event.get("result"))
                    continue
                self._event(event.get("method"), event.get("params", {}))
        except Exception:
            self._failure = "browser download event stream failed"
        finally:
            if not self._closing:
                self._failure = "browser download event stream closed"
            for future in self._commands.values():
                if not future.done():
                    future.set_exception(RuntimeError("browser download connection closed"))

    def _event(self, method, event):
        guid = event.get("guid", "")
        if not isinstance(guid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", guid) is None:
            return
        if method == "Browser.downloadWillBegin":
            name = str(event.get("suggestedFilename") or "download").replace("\\", "/").rsplit("/", 1)[-1]
            name = re.sub(r"[\x00-\x1f\x7f]", "_", name).strip()[:160]
            self._pending[guid] = name if name not in {"", ".", ".."} else "download"
        elif method == "Browser.downloadProgress" and guid in self._pending:
            if event.get("state") == "canceled":
                self._pending.pop(guid)
                self._failure = "browser download canceled"
            elif event.get("state") == "completed":
                name = self._pending.pop(guid)
                source = self.root / guid
                target = self.root / f"{guid}-{name}"
                try:
                    details = source.lstat()
                    if not stat.S_ISREG(details.st_mode) or source.resolve().parent != self.root:
                        raise ValueError("invalid download file")
                    if target.exists() or target.is_symlink():
                        raise ValueError("download target already exists")
                    source.rename(target)
                    self._completed.append(
                        {"name": name, "path": str(target), "size": details.st_size, "status": "completed"}
                    )
                except (OSError, ValueError):
                    self._failure = "browser download file could not be finalized"

    def check_ready(self):
        """Fail closed before another Browser action if this task lost ownership."""
        if (
            self._owners.get(self.owner_key) is not self
            or self._socket is None
            or self._reader is None
            or self._reader.done()
            or self._failure
        ):
            raise ValueError("browser download lease is not ready")

    async def completed(self, *, grace_s: float = 0.5, timeout_s: float = 60):
        """Wait for in-flight downloads and return new, finalized files once."""
        async with asyncio.timeout(timeout_s):
            await asyncio.sleep(grace_s)
            while self._pending:
                if self._reader is not None and self._reader.done():
                    raise RuntimeError("browser download connection closed")
                await asyncio.sleep(0.05)
        if self._failure:
            raise RuntimeError(self._failure)
        receipts = self._completed[self._reported :]
        self._reported = len(self._completed)
        return receipts

    async def close(self):
        """Cancel active GUIDs and reset behavior before releasing the lease."""
        if self._owners.get(self.owner_key) is not self:
            return
        if self._socket is not None:
            for guid in tuple(self._pending):
                await self._command("Browser.cancelDownload", {"guid": guid})
            async with asyncio.timeout(5):
                while self._pending:
                    if self._reader.done():
                        raise RuntimeError("browser download cancellation unconfirmed")
                    await asyncio.sleep(0.05)
            await self._command("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": False})
        if any(self.root.glob("*.crdownload")):
            raise RuntimeError("browser temporary download exit is unconfirmed")
        await self.discard_after_exit()

    async def discard_after_exit(self):
        """Release local ownership after reset ACKs or caller-confirmed Chrome exit."""
        self._closing = True
        if self._socket is not None:
            await self._socket.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._client is not None:
            await self._client.close()
        self._socket = self._reader = self._client = None
        if self._owners.get(self.owner_key) is self:
            self._owners.pop(self.owner_key)
