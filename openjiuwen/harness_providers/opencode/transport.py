# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authenticated local HTTP and one bounded, non-reconnecting SSE consumer."""

import asyncio
import contextlib
import json
import math

import aiohttp

from .errors import OpenCodeError, http_error


def decode_json(value):
    def invalid(_):
        raise ValueError

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number

    try:
        return json.loads(value, parse_constant=invalid, parse_float=finite)
    except (ValueError, UnicodeError, RecursionError):
        raise OpenCodeError("invalid_native_json") from None


async def sse_events(content, limit):
    """Parse fragmented UTF-8/CRLF frames with bounds before JSON allocation."""
    pending = bytearray()
    data = []
    size = 0
    async for chunk in content.iter_chunked(min(limit + 1, 16384)):
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            size += len(line) + 1
            if size > limit:
                raise OpenCodeError("sse_frame_limit")
            line = line.removesuffix(b"\r")
            if not line:
                if data:
                    event = decode_json(b"\n".join(data))
                    if (
                        not isinstance(event, dict)
                        or not isinstance(event.get("type"), str)
                        or not isinstance(event.get("properties"), dict)
                    ):
                        raise OpenCodeError("invalid_sse_event")
                    yield event
                data, size = [], 0
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
        if len(pending) + size > limit:
            raise OpenCodeError("sse_frame_limit")
    # EOF, including a truncated frame, is never a turn completion.
    raise OpenCodeError("event_stream_closed", category="server_unavailable")


class Transport:
    def __init__(self, server, config):
        self.server, self.config = server, config
        self.queue = asyncio.Queue(maxsize=config.transport_capacity)
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.failure = None
        self.pump = None
        self.client = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth("opencode", server.password),
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=config.request_timeout_s),
        )

    async def request(self, method, path, body=None):
        try:
            async with self.client.request(
                method, self.server.url + path, json=body, allow_redirects=False
            ) as response:
                if not 200 <= response.status < 300:
                    raise http_error(response.status)
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > self.config.max_response_bytes:
                        raise OpenCodeError("http_response_limit")
                return decode_json(data) if data else None
        except TimeoutError:
            raise OpenCodeError("http_timeout", category="network_timeout") from None
        except aiohttp.ClientError:
            raise OpenCodeError("http_transport_failed", category="server_unavailable") from None

    async def _consume(self):
        try:
            async with self.client.get(
                self.server.url + "/event",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.config.request_timeout_s),
            ) as response:
                if response.status != 200:
                    raise http_error(response.status)
                if response.content_type != "text/event-stream":
                    raise OpenCodeError("invalid_event_content_type")
                async for event in sse_events(response.content, self.config.max_frame_bytes):
                    if event["type"] == "server.connected":
                        self.connected.set()
                    elif event["type"] != "server.heartbeat":
                        await self.queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = (
                exc
                if isinstance(exc, OpenCodeError)
                else OpenCodeError("event_transport_failed", category="server_unavailable")
            )
        finally:
            self.closed.set()

    async def connect(self):
        self.pump = asyncio.create_task(self._consume(), name="opencode-events")
        connected = asyncio.create_task(self.connected.wait())
        closed = asyncio.create_task(self.closed.wait())
        try:
            await asyncio.wait({connected, closed}, return_when=asyncio.FIRST_COMPLETED)
            if self.closed.is_set():
                raise self.failure or OpenCodeError("event_stream_closed")
        finally:
            for task in (connected, closed):
                task.cancel()
            await asyncio.gather(connected, closed, return_exceptions=True)

    async def next_event(self):
        if not self.queue.empty():
            return self.queue.get_nowait()
        if self.closed.is_set():
            raise self.failure or OpenCodeError("event_stream_closed", category="server_unavailable")
        item = asyncio.create_task(self.queue.get())
        closed = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait({item, closed}, return_when=asyncio.FIRST_COMPLETED)
            if item in done:
                return item.result()
            raise self.failure or OpenCodeError("event_stream_closed", category="server_unavailable")
        finally:
            for task in (item, closed):
                task.cancel()
            await asyncio.gather(item, closed, return_exceptions=True)

    async def close(self):
        self.closed.set()
        if self.pump:
            self.pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.pump
            self.pump = None
        await self.client.close()
