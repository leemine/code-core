# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Characterize non-command reads with locked CLI and task-owned sentinel files.

Passing counterexamples document remaining boundaries, not isolation acceptance.
"""

import asyncio
import json
import struct
import sys
import zlib
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from openjiuwen.harness_protocol import (
    HarnessInput,
    McpServerConfig,
    McpTransport,
    ToolApprovalDecision,
    ToolApprovalResponse,
    TurnEventKind,
)
from openjiuwen.harness_providers.codex import CodexHarness
from openjiuwen.harness_providers.skills import SkillSource

from tests.system_tests.harness_providers._codex_response_fixture import ResponsesFixture
from tests.system_tests.harness_providers.test_codex_read_roots_local import _config, _context, _rpc
from tests.system_tests.harness_providers.test_codex_read_roots_local import scope as scope

sdk = pytest.importorskip("openai_codex", reason="optional locked SDK/CLI required")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux sandbox probe")


class RecordingApproval:
    def __init__(self, allow=False):
        self.allow = allow
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        return ToolApprovalResponse(
            request_id=request.request_id,
            decision=ToolApprovalDecision.ALLOW if self.allow else ToolApprovalDecision.DENY,
        )

    async def cancel(self, request_id, *, reason=None):
        pass


@asynccontextmanager
async def _started(root, responses, *, handler=None, skills=(), mcp=(), approval_mode="prompt", overrides=()):
    config = replace(_config(root, responses), skills=skills, mcp_default_tools_approval_mode=approval_mode,
                     config_overrides=overrides)
    harness = CodexHarness(config)
    proc = None
    try:
        await asyncio.wait_for(harness.start(replace(
            _context(root), interactions=handler or RecordingApproval(), mcp_servers=mcp,
        )), 20)
        proc = harness._client._client._sync._proc
        yield harness
    finally:
        await asyncio.wait_for(harness.stop(), 10)
        if proc is not None:
            assert proc.poll() is not None


async def _send(harness, content="Return the fixed fixture marker."):
    receipt = await harness.send(HarnessInput(content=content))
    events = [event async for event in harness.turn_events(receipt.turn_id)]
    assert events[-1].event.kind is TurnEventKind.FINISHED


async def _command_read(harness, root, target):
    return await _rpc(harness._client, "command/exec", {
        "command": ["/bin/cat", str(target)], "cwd": str(root / "work"),
        "permissionProfile": "a0-read", "timeoutMs": 3000,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["work", "symlink", "ancestor"])
async def test_agents_auto_load_is_outside_command_read_boundary(scope, location):
    marker = "A0-AGENTS-BODY-" + location
    if location == "ancestor":
        (scope / ".git").mkdir()
        target = scope / "AGENTS.md"
    else:
        (scope / "work/.git").mkdir()
        target = scope / ("work/AGENTS.md" if location == "work" else "outside/AGENTS.md")
    target.write_text(marker)
    if location == "symlink":
        (scope / "work/AGENTS.md").symlink_to(target)
    handler = RecordingApproval()
    with ResponsesFixture() as responses:
        async with _started(scope, responses, handler=handler) as harness:
            command = await _command_read(harness, scope, target)
            await _send(harness)
            visible = marker in json.dumps(responses.requests[0])
            print(json.dumps({"agents_location": location, "auto_body_visible": visible,
                              "command_exit": command["exitCode"], "approvals": len(handler.requests)}))
            assert visible
            assert (command["exitCode"] == 0) is (location == "work")
            assert not handler.requests


def _skill(directory):
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text("---\nname: a0-channel\ndescription: A0-SKILL-DESCRIPTION\n---\nA0-SKILL-BODY\n")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["home", "symlink"])
async def test_skill_discovery_and_explicit_native_input_outside_read_roots(scope, location):
    (scope / "work/.git").mkdir()
    if location == "home":
        target = _skill(scope / "home/.agents/skills/a0-channel")
    else:
        target = _skill(scope / "outside/a0-channel")
        scan = scope / "work/.agents/skills"
        scan.mkdir(parents=True)
        (scan / "a0-channel").symlink_to(target.parent)
    handler = RecordingApproval()
    with ResponsesFixture() as responses:
        async with _started(scope, responses, handler=handler) as harness:
            command = await _command_read(harness, scope, target)
            await _send(harness)
            initial = json.dumps(responses.requests[0])
            before = len(responses.requests)
            result = await asyncio.wait_for(harness._thread.run([
                sdk.TextInput("Return fixed marker."), sdk.SkillInput(name="a0-channel", path=str(target)),
            ]), 20)
            assert result.error is None
            selected = json.dumps(responses.requests[before:])
            print(json.dumps({"skill_location": location, "metadata_visible": "A0-SKILL-DESCRIPTION" in initial,
                              "initial_body_visible": "A0-SKILL-BODY" in initial,
                              "selected_body_visible": "A0-SKILL-BODY" in selected,
                              "command_exit": command["exitCode"], "approvals": len(handler.requests)}))
            assert "A0-SKILL-DESCRIPTION" in initial
            assert "A0-SKILL-BODY" not in initial
            assert "A0-SKILL-BODY" in selected
            assert command["exitCode"] != 0 and not handler.requests


@pytest.mark.asyncio
async def test_portable_skill_copy_precedes_cli_permission_profile(scope):
    target = _skill(scope / "outside/a0-channel")
    with ResponsesFixture() as responses:
        async with _started(scope, responses, skills=(SkillSource(str(target.parent)),)) as harness:
            copied = scope / "work/.agents/skills/a0-channel/SKILL.md"
            assert copied.read_text() == target.read_text()
            original_read = await _command_read(harness, scope, target)
            copied_read = await _command_read(harness, scope, copied)
            assert original_read["exitCode"] != 0
            assert "A0-SKILL-BODY" in copied_read["stdout"]
            print(json.dumps({"portable_skill_copied": True, "source_command_denied": True,
                              "copied_command_readable": True}))


def _png(path):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 2, 2, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"\0" + b"\xff\0\0" * 2 + b"\0" + b"\0\xff\0" * 2))
    path.write_bytes(data + chunk(b"IEND", b""))


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("location", ["work", "outside"])
async def test_local_image_native_read_vs_core_text_serialization(scope, native, location):
    target = scope / location / "fixture.png"
    _png(target)
    handler = RecordingApproval()
    with ResponsesFixture() as responses:
        async with _started(scope, responses, handler=handler) as harness:
            if native:
                result = await asyncio.wait_for(harness._thread.run([
                    sdk.TextInput("Return fixed marker."), sdk.LocalImageInput(path=str(target)),
                ]), 20)
                assert result.error is None
            else:
                await _send(harness, [{"type": "localImage", "path": str(target)}])
            payload = json.dumps(responses.requests[0])
            has_image = '"type": "input_image"' in payload
            print(json.dumps({"native_local_image": native, "location": location,
                              "image_payload_sent": has_image, "approvals": len(handler.requests)}))
            assert has_image is native
            if not native:
                assert str(target) in payload
            assert not handler.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_mode,allow", [("prompt", False), ("prompt", True), ("approve", False)])
async def test_stdio_mcp_startup_and_tool_reads_do_not_inherit_command_profile(scope, approval_mode, allow):
    marker = scope / "outside/marker.txt"
    startup, called, pidfile = (scope / "work" / name for name in ("startup.txt", "called.txt", "mcp.pid"))
    code = (
        "import os\nfrom pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        f"Path({str(startup)!r}).write_text(Path({str(marker)!r}).read_text())\n"
        "m=FastMCP('a0_read')\n@m.tool()\ndef read_marker() -> str:\n"
        '    """Read the isolated marker."""\n'
        f"    Path({str(called)!r}).write_text('called')\n"
        f"    return Path({str(marker)!r}).read_text()\n"
        "m.run(transport='stdio')\n"
    )
    server = McpServerConfig(name="a0_read", transport=McpTransport.STDIO, command=(sys.executable, "-c", code))
    handler = RecordingApproval(allow=allow)
    pid = None
    with ResponsesFixture() as responses:
        responses.items.append({
            "type": "function_call", "namespace": "mcp__a0_read", "name": "read_marker",
            "id": "fc_read", "call_id": "call_read", "arguments": "{}",
        })
        async with _started(scope, responses, handler=handler, mcp=(server,), approval_mode=approval_mode) as harness:
            inventory = await _rpc(harness._client, "mcpServerStatus/list", {"threadId": harness.provider_session_id})
            assert any(item["name"] == "a0_read" and "read_marker" in item["tools"] for item in inventory["data"])
            pid = int(pidfile.read_text())
            assert startup.read_text() == "A0-MARKER-outside" and not handler.requests
            command = await _command_read(harness, scope, marker)
            assert command["exitCode"] != 0
            await _send(harness)
            outputs = [item for request in responses.requests for item in request.get("input", [])
                       if item.get("type") == "function_call_output"]
            visible = "A0-MARKER-outside" in json.dumps(outputs)
            expected = allow or approval_mode == "approve"
            assert visible is expected and called.exists() is expected
            if approval_mode == "prompt":
                assert len(handler.requests) == 1 and handler.requests[0].tool_name == "mcp__a0_read"
            else:
                assert not handler.requests
            print(json.dumps({"mcp_mode": approval_mode, "host_would_allow": allow, "startup_read_succeeded": True,
                              "tool_body_visible": visible, "command_denied": True}))
    for _ in range(50):
        if not await asyncio.to_thread(Path(f"/proc/{pid}").exists):
            break
        await asyncio.sleep(0.1)
    assert not await asyncio.to_thread(Path(f"/proc/{pid}").exists)


@pytest.mark.asyncio
async def test_agents_loading_can_be_disabled_without_widening_command_roots(scope):
    (scope / "work/.git").mkdir()
    target = scope / "outside/AGENTS.md"
    target.write_text("A0-AGENTS-DISABLED-BODY")
    (scope / "work/AGENTS.md").symlink_to(target)
    with ResponsesFixture() as responses:
        async with _started(scope, responses, overrides=("project_doc_max_bytes=0",)) as harness:
            await _send(harness)
            assert "A0-AGENTS-DISABLED-BODY" not in json.dumps(responses.requests)
            command = await _command_read(harness, scope, target)
            assert command["exitCode"] != 0
            print(json.dumps({"agents_loading_disabled": True, "command_denied": True}))


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["work", "outside", "symlink"])
async def test_model_view_image_read_boundary(scope, location):
    target = scope / ("outside" if location == "symlink" else location) / "fixture.png"
    _png(target)
    if location == "symlink":
        link = scope / "work/linked.png"
        link.symlink_to(target)
        target = link
    handler = RecordingApproval()
    with ResponsesFixture() as responses:
        responses.items.append({
            "type": "function_call", "name": "view_image", "id": "fc_image", "call_id": "call_image",
            "arguments": json.dumps({"path": str(target)}),
        })
        async with _started(scope, responses, handler=handler) as harness:
            await _send(harness)
            outputs = [item for request in responses.requests[1:] for item in request.get("input", [])
                       if item.get("type") == "function_call_output"]
            payload = json.dumps(responses.requests[1:])
            has_image = '"type": "input_image"' in payload
            print(json.dumps({"view_image_location": location, "image_payload_sent": has_image,
                              "approvals": len(handler.requests), "outputs": outputs}))
            assert {item["call_id"] for item in outputs} == {"call_image"}
            assert has_image is (location == "work")
            if location != "work":
                assert "unable to locate image" in json.dumps(outputs)
            assert not handler.requests
