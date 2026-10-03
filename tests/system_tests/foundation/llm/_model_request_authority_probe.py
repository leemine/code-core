# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded real SDK/HTTP loopback authority probe; no external service or user key."""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig, ModelRequestDenied


async def run():
    requests, targets, tasks = [], [], set()
    permitted = True

    async def handler(reader, writer):
        nonlocal permitted
        task = asyncio.current_task()
        tasks.add(task)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = head.decode().split("\r\n")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            headers = {key.lower(): value for key, value in headers.items()}
            body = json.loads(await reader.readexactly(int(headers["content-length"])))
            requests.append(
                {
                    "path": lines[0].split()[1],
                    "model": body["model"],
                    "stream": body.get("stream", False),
                    "synthetic_credential_matches": headers.get("authorization") == "Bearer probe-only-fixture",
                }
            )
            if len(requests) == 1:
                permitted = False
                status = "429 Too Many Requests"
                data = b'{"error":{"message":"fixture retry"}}'
                extra_headers = b"retry-after-ms: 1\r\n"
            elif body.get("stream"):
                status = "200 OK"
                chunk = {
                    "id": "fixture",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None}],
                }
                data = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                extra_headers = b"content-type: text/event-stream\r\n"
            else:
                status = "200 OK"
                data = json.dumps(
                    {
                        "id": "fixture",
                        "object": "chat.completion",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
                        ],
                    }
                ).encode()
                extra_headers = b"content-type: application/json\r\n"
            writer.write(
                f"HTTP/1.1 {status}\r\ncontent-length: {len(data)}\r\nconnection: close\r\n".encode()
                + extra_headers
                + b"\r\n"
                + data
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    async def authority(target):
        targets.append(asdict(target))
        return {"Authorization": "Bearer probe-only-fixture"} if permitted else None

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        guarded = Model(
            ModelClientConfig(
                client_provider="OpenAI",
                api_key="not-a-credential",
                api_base=f"http://127.0.0.1:{port}/v1",
                max_retries=2,
                timeout=5,
            ),
            ModelRequestConfig(model="fixture-model"),
            request_authority=authority,
        )
        try:
            await guarded.invoke("fixture")
        except ModelRequestDenied as error:
            assert error.fatal and not error.recoverable
        else:
            raise AssertionError("revoked retry was accepted")
        assert len(requests) == 1 and len(targets) == 2
        permitted = True
        assert (await guarded.invoke("fixture")).content == "OK"
        assert "".join(chunk.content for chunk in [part async for part in guarded.stream("fixture")]) == "OK"
        assert len(requests) == 3 and len(targets) == 4
        assert all(item["synthetic_credential_matches"] for item in requests)
        result = {
            "status": "passed",
            "requests": requests,
            "authority_targets": targets,
            "checks": [
                "real SDK HTTP invoke",
                "429 retry rechecks current authority and sends nothing after revocation",
                "fresh invocation resolves synthetic credential",
                "real HTTP SSE stream",
            ],
            "external_provider": "not run",
            "user_credentials": "not read",
        }
    finally:
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    result["cleanup"] = "owned loopback listener and connections closed"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(run(), timeout=30))
    args.output.write_text(json.dumps(result, indent=2))
