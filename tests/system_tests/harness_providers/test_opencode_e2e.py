# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in fixed CLI + loopback model qualification (no remote model credentials).

RUN_OPENCODE_OC1=1 pytest tests/system_tests/harness_providers/test_opencode_e2e.py
Requires non-root Linux, cgroup v2, and an active systemd user manager.
"""

import asyncio
import json
import os
import signal
import socket
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from openjiuwen.harness_protocol import (
    AbortMode,
    HarnessInput,
    HostCapability,
    InteractionResponseStatus,
    McpServerConfig,
    McpTransport,
    ResumePolicy,
    ToolApprovalDecision,
    ToolApprovalRequest,
    ToolApprovalResponse,
    TurnEventKind,
    UserInputResponse,
)
from openjiuwen.harness_providers.base import ProviderStartupError
from openjiuwen.harness_providers.opencode import (
    OpenCodeHarness,
    OpenCodeHarnessConfig,
    OpenCodeModelConfig,
    OpenCodeNativePluginConfig,
    opencode_plugin_content_digest,
)
from openjiuwen.harness_providers.opencode.server import ManagedServer, write_private

from ._contract import assert_turn_invariants, collect_turn, make_context, terminal_of, tool_items

pytestmark = pytest.mark.skipif(os.environ.get("RUN_OPENCODE_OC1") != "1", reason="real managed CLI opt-in")


class ModelFixture:
    def __init__(self):
        self.actions, self.requests = [], []
        self.mcp_calls = []
        self.mcp_token = "oc5-loopback-token"
        self.mcp_url = ""
        self.entered = asyncio.Event()

    async def respond(self, request):
        body = await request.json()
        self.requests.append(body)
        action = self.actions.pop(0) if body.get("tools") and self.actions else {"text": "OC1-PONG"}
        self.entered.set()
        if action.get("slow"):
            await asyncio.sleep(5)
        if "tool" in action:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + str(len(self.requests)),
                        "type": "function",
                        "function": {"name": action["tool"], "arguments": json.dumps(action["args"])},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta, finish = {"content": action.get("text", "OC1-PONG")}, "stop"
        base = {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture"}
        chunks = [
            dict(base, choices=[{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]),
            dict(
                base,
                choices=[{"index": 0, "delta": {}, "finish_reason": finish}],
                usage={"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22},
            ),
        ]
        return web.Response(
            text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    async def mcp(self, request):
        if request.headers.get("Authorization") != "Bearer " + self.mcp_token:
            return web.Response(status=401)
        body = await request.json()
        method = body.get("method")
        self.mcp_calls.append(method)
        if "id" not in body:
            return web.Response(status=202)
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oc5-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Return the fixed OC5 MCP marker",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "OC5-MCP-MARKER"}]}
        else:
            result = {}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest_asyncio.fixture
async def runtime(tmp_path):
    model = ModelFixture()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", model.respond)
    app.router.add_route("*", "/mcp", model.mcp)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    model.mcp_url = f"http://127.0.0.1:{port}/mcp"
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir()
    (work / "opencode.json").write_text('{"model":"unadmitted/canary"}')
    config = OpenCodeHarnessConfig(
        cli_path=os.environ.get("OPENCODE_OC1_CLI", os.path.expanduser("~/.opencode/bin/opencode")),  # noqa: ASYNC240
        runtime_root=str(root),
        model=OpenCodeModelConfig("fixture", f"http://127.0.0.1:{port}/v1", "fixture-only"),
        full_access=True,
        turn_timeout_s=25,
    )
    harnesses = []

    def harness(cfg=config):
        value = OpenCodeHarness(cfg)
        harnesses.append(value)
        return value

    try:
        yield config, model, work, harness
    finally:
        for value in harnesses:
            await value.stop()
        await runner.cleanup()
        # Only task-owned sealed test directories; allow pytest's tmp cleanup.
        for directory in root.rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)


async def turn(harness, text):
    receipt = await harness.send(HarnessInput(text))
    events = await collect_turn(harness, receipt.turn_id)
    assert_turn_invariants(events, receipt.turn_id)
    return events, terminal_of(events)


@pytest.mark.asyncio
async def test_text_followup_tool_usage_and_cleanup(runtime):
    config, model, work, create = runtime
    h = create()
    await h.start(make_context(cwd=str(work)))
    server = h._server
    owner = dict(server.owner)
    for _ in range(2):
        _, terminal = await turn(h, "Reply OC1-PONG")
        assert terminal.kind is TurnEventKind.FINISHED, terminal.result
        assert terminal.result.final_output == "OC1-PONG"
        assert terminal.result.usage.input_tokens == 17
        assert terminal.result.usage.total_tokens == 22
    model.actions = [
        {"tool": "bash", "args": {"command": "printf OC1-TOOL", "description": "fixture"}},
        {"text": "OC1-TOOL-DONE"},
    ]
    events, terminal = await turn(h, "Run the fixture shell command")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert terminal.result.usage.input_tokens == 34
    assert terminal.result.usage.output_tokens == 10
    assert len({item for item, _ in tool_items(events)}) == 1
    assert any(
        block.kind == "tool_result" and "OC1-TOOL" in block.content
        for message in terminal.result.messages
        for block in message.content
    )
    await h.stop()
    assert (await server.properties(owner)).get("ActiveState") != "active"
    assert not (server.scope / "owner.json").exists()


def _native_plugin(
    source: Path,
    *,
    version="1.0.0",
    hooks=("event", "tool.execute.before", "tool.execute.after"),
    tools=(),
):
    return OpenCodeNativePluginConfig(
        plugin_id="ocp-guard",
        source_type="local",
        source_locator=str(source),
        version=version,
        content_sha256=opencode_plugin_content_digest(source),
        entrypoint="guard.js",
        export_name="Guard",
        required_hooks=hooks,
        required_tools=tools,
    )


@pytest.mark.asyncio
async def test_managed_native_plugin_hooks_tamper_gate_and_cleanup(runtime, tmp_path):
    config, model, work, create = runtime
    source = tmp_path / "ocp-guard"
    source.mkdir()
    entrypoint = source / "guard.js"
    entrypoint.write_text(
        "export const Guard = async () => ({\n"
        "  event: async () => {},\n"
        '  "tool.execute.before": async (_input, output) => {\n'
        '    if (String(output.args?.command || "").includes("OC-P-MUTATE")) '
        'output.args.command = "printf OC-P-PRODUCTION"\n'
        "  },\n"
        '  "tool.execute.after": async (_input, output) => { output.output += "|OC-P-AFTER" },\n'
        "})\n"
    )
    plugin = _native_plugin(source)
    h = create(replace(config, native_plugins=(plugin,)))
    await h.start(make_context(cwd=str(work), host_session_id="ocp-native"))
    server = h._server
    owner = dict(server.owner)
    assert server.native_config["plugin"] == list(server.plugin_stage.specs)
    assert all(spec.startswith("file:") and str(source) not in spec for spec in server.plugin_stage.specs)
    model.actions = [
        {"tool": "bash", "args": {"command": "printf OC-P-MUTATE", "description": "managed plugin"}},
        {"text": "OC-P-DONE"},
    ]
    _, terminal = await turn(h, "Run the managed plugin fixture")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert any(
        block.kind == "tool_result" and block.content == "OC-P-PRODUCTION|OC-P-AFTER"
        for message in terminal.result.messages
        for block in message.content
    )
    checkpoint = await h.export_checkpoint()
    assert checkpoint.data["native_plugin_fingerprint"] == server.plugin_fingerprint

    entrypoint.write_text(entrypoint.read_text() + "// drift\n")
    _, terminal = await turn(h, "This turn must fail before native dispatch")
    assert terminal.kind is TurnEventKind.FAILED
    assert terminal.result.error.code == "native_plugin_source_drift"
    assert (await server.properties(owner)).get("ActiveState") != "active"
    assert not (server.scope / "owner.json").exists()

    replacement = tmp_path / "ocp-guard-v2"
    replacement.mkdir()
    (replacement / "guard.js").write_text(entrypoint.read_text().removesuffix("// drift\n"))
    plugin_v2 = _native_plugin(replacement, version="2.0.0")
    resumed = create(replace(config, native_plugins=(plugin_v2,)))
    with pytest.raises(ProviderStartupError) as rejected:
        await resumed.start(
            make_context(
                cwd=str(work),
                host_session_id="ocp-native",
                checkpoint=checkpoint,
                resume_policy=ResumePolicy.REQUIRE_RESUME,
            )
        )
    assert rejected.value.error.code == "storage_identity_mismatch"
    fresh = create(replace(config, native_plugins=(plugin_v2,)))
    await fresh.start(make_context(cwd=str(work), host_session_id="ocp-changed-new"))
    model.actions = [
        {"tool": "bash", "args": {"command": "printf OC-P-MUTATE", "description": "new snapshot"}},
        {"text": "OC-P-NEW-DONE"},
    ]
    _, terminal = await turn(fresh, "Use the replacement plugin in a new session")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result


@pytest.mark.asyncio
async def test_managed_native_plugin_custom_tool_uses_host_permission_and_native_loader(runtime, tmp_path):
    config, model, work, create = runtime
    handler = InteractionHandler()
    source = tmp_path / "ocp-custom-tool"
    source.mkdir()
    (source / "guard.js").write_text(
        'import { writeFile } from "node:fs/promises"\n'
        "export const Guard = async () => ({\n"
        "  event: async () => {},\n"
        '  "tool.execute.before": async () => {},\n'
        '  "tool.execute.after": async () => {},\n'
        "  tool: { managed_marker: { description: 'managed marker', args: {}, "
        "execute: async () => { await writeFile(process.env.TMPDIR + '/custom-tool-executed', 'yes'); "
        "return 'OC-P-CUSTOM-TOOL' } } },\n"
        "})\n"
    )
    plugin = _native_plugin(source, tools=("managed_marker",))
    h = create(replace(config, full_access=False, native_plugins=(plugin,)))
    await h.start(
        make_context(
            cwd=str(work),
            host_session_id="ocp-native-tool",
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=handler,
        )
    )
    model.actions = [
        {"tool": "managed_marker", "args": {}},
        {"text": "OC-P-CUSTOM-DONE"},
    ]
    _, terminal = await turn(h, "Call the managed marker tool")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert any(
        isinstance(request, ToolApprovalRequest) and request.tool_name == "managed_marker"
        for request in handler.requests
    )
    assert any(
        block.kind == "tool_result" and block.content == "OC-P-CUSTOM-TOOL"
        for message in terminal.result.messages
        for block in message.content
    )
    assert (h._server.root / "tmp/custom-tool-executed").read_text() == "yes"
    await h.stop()

    denied = create(replace(config, full_access=False, native_plugins=(plugin,)))
    await denied.start(make_context(cwd=str(work), host_session_id="ocp-native-tool-denied"))
    model.actions = [{"tool": "managed_marker", "args": {}}]
    _, terminal = await turn(denied, "The managed marker must be rejected")
    assert terminal.kind is TurnEventKind.FAILED
    assert terminal.result.error.code == "interaction_declined"
    assert not (denied._server.root / "tmp/custom-tool-executed").exists()
    await denied.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("SQL2JAVA_PLUGIN_ROOT"), reason="prepared sql2java plugin opt-in")
async def test_sql2java_plugin_workflow_list_through_managed_native_loader(runtime):
    config, model, work, create = runtime
    source = Path(os.environ["SQL2JAVA_PLUGIN_ROOT"]).resolve(strict=True)  # noqa: ASYNC240
    plugin = OpenCodeNativePluginConfig(
        plugin_id="sql2java-workflow",
        source_type="local",
        source_locator=str(source),
        version=os.environ.get("SQL2JAVA_PLUGIN_VERSION", "local-regression"),
        content_sha256=opencode_plugin_content_digest(source),  # noqa: ASYNC240
        entrypoint="plugins/workflow-engine.ts",
        export_name="WorkflowEnginePlugin",
        required_hooks=(
            "chat.message",
            "chat.params",
            "event",
            "experimental.chat.system.transform",
            "tool.execute.before",
            "tool.execute.after",
        ),
        required_tools=("saveArtifact", "workflow"),
    )
    handler = InteractionHandler()
    h = create(
        replace(
            config,
            full_access=False,
            native_plugins=(plugin,),
            startup_timeout_s=90,
            turn_timeout_s=90,
        )
    )
    await h.start(
        make_context(
            cwd=str(work),
            host_session_id="ocp-sql2java",
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=handler,
        )
    )
    model.actions = [
        {"tool": "workflow", "args": {"action": "list"}},
        {"text": "OC-P-SQL2JAVA-DONE"},
    ]
    _, terminal = await turn(h, "List sql2java workflow runs")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert any(
        isinstance(request, ToolApprovalRequest) and request.tool_name == "workflow"
        for request in handler.requests
    )
    assert any(
        block.kind == "tool_result" and "No runs" in str(block.content)
        for message in terminal.result.messages
        for block in message.content
    )
    tools = {tool["function"]["name"] for tool in model.requests[0]["tools"]}
    assert {"workflow", "saveArtifact"} <= tools
    await h.stop()


@pytest.mark.asyncio
async def test_native_loader_hook_inventory_mismatch_fails_start_and_reaps(runtime, tmp_path):
    config, _, work, create = runtime
    source = tmp_path / "ocp-unexpected-hook"
    source.mkdir()
    (source / "guard.js").write_text(
        "export const Guard = async () => ({\n"
        "  event: async () => {},\n"
        '  "tool.execute.before": async () => {},\n'
        '  "tool.execute.after": async () => {},\n'
        '  "permission.ask": async () => {},\n'
        "})\n"
    )
    h = create(replace(config, native_plugins=(_native_plugin(source),), startup_timeout_s=5))
    with pytest.raises(ProviderStartupError):
        await h.start(make_context(cwd=str(work), host_session_id="ocp-inventory-mismatch"))
    assert h._server is None and h._transport is None


@pytest.mark.asyncio
async def test_portable_skill_discovery_call_and_new_session_disable(runtime, tmp_path):
    config, model, work, create = runtime
    source = tmp_path / "portable-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: portable-proof\ndescription: Use for the fixed OC5 portable proof.\n---\n"
        "Return OC5-SKILL-MARKER when this skill is loaded.\n"
    )
    enabled = create(replace(config, skills=(str(source),), skill_conflict="replace"))
    await enabled.start(make_context(cwd=str(work), host_session_id="oc5-skills-enabled"))
    discovered = await enabled._transport.request("GET", "/skill")
    assert any(item.get("name") == "portable-proof" for item in discovered)
    model.actions = [
        {"tool": "skill", "args": {"name": "portable-proof"}},
        {"text": "OC5-SKILL-DONE"},
    ]
    _, terminal = await turn(enabled, "Load the portable proof skill")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert any(
        block.kind == "tool_result" and "OC5-SKILL-MARKER" in str(block.content)
        for message in terminal.result.messages
        for block in message.content
    )
    copied = enabled._server.skill_path / "portable-proof/SKILL.md"
    assert copied.is_file()
    await enabled.stop()

    disabled = create(config)
    await disabled.start(make_context(cwd=str(work), host_session_id="oc5-skills-disabled"))
    discovered = await disabled._transport.request("GET", "/skill")
    assert not any(item.get("name") == "portable-proof" for item in discovered)
    assert copied.is_file()


@pytest.mark.asyncio
async def test_host_admitted_remote_mcp_is_discovered_and_called(runtime):
    _, model, work, create = runtime
    server = McpServerConfig(
        name="fixture",
        transport=McpTransport.HTTP,
        url=model.mcp_url,
        headers={"Authorization": "Bearer " + model.mcp_token},
    )
    h = create()
    context = replace(
        make_context(cwd=str(work), host_session_id="oc5-mcp"),
        host_capabilities=frozenset({HostCapability.MCP_SERVERS}),
        mcp_servers=(server,),
    )
    await h.start(context)
    assert h._server.native_config["mcp"]["fixture"]["oauth"] is False
    model.actions = [
        {"tool": "fixture_echo", "args": {}},
        {"text": "OC5-MCP-DONE"},
    ]
    _, terminal = await turn(h, "Call the fixture MCP tool")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert "tools/call" in model.mcp_calls
    assert any(
        block.kind == "tool_result" and "OC5-MCP-MARKER" in str(block.content)
        for message in terminal.result.messages
        for block in message.content
    )


@pytest.mark.asyncio
async def test_permission_without_host_is_denied_without_execution(runtime):
    config, model, work, create = runtime
    h = create(replace(config, full_access=False))
    await h.start(make_context(cwd=str(work)))
    model.actions = [{"tool": "bash", "args": {"command": "touch denied-canary", "description": "fixture"}}]
    _, terminal = await turn(h, "Run fixture tool")
    assert terminal.kind is TurnEventKind.FAILED
    assert terminal.result.error.code == "interaction_declined"
    assert not (work / "denied-canary").exists()


class InteractionHandler:
    def __init__(self):
        self.requests = []
        self.cancelled = []

    async def handle(self, request):
        self.requests.append(request)
        if isinstance(request, ToolApprovalRequest):
            return ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW)
        return UserInputResponse(request.request_id, InteractionResponseStatus.COMPLETED, "Yes")

    async def cancel(self, request_id, *, reason):
        self.cancelled.append((request_id, reason))


@pytest.mark.asyncio
async def test_real_permission_and_question_round_trip(runtime):
    config, model, work, create = runtime
    handler = InteractionHandler()
    h = create(replace(config, full_access=False))
    await h.start(
        make_context(
            cwd=str(work),
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL, HostCapability.USER_INPUT}),
            interactions=handler,
        )
    )
    model.actions = [
        {"tool": "bash", "args": {"command": "printf OC2-APPROVED", "description": "fixture"}},
        {"text": "OC2-APPROVED-DONE"},
    ]
    _, terminal = await turn(h, "Run the approved fixture command")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert any(isinstance(request, ToolApprovalRequest) for request in handler.requests)
    assert any(
        block.kind == "tool_result" and "OC2-APPROVED" in block.content
        for message in terminal.result.messages
        for block in message.content
    )
    model.actions = [
        {
            "tool": "question",
            "args": {
                "questions": [
                    {
                        "header": "OC2",
                        "question": "Continue?",
                        "options": [{"label": "Yes", "description": "Proceed"}],
                    }
                ]
            },
        },
        {"text": "OC2-QUESTION-DONE"},
    ]
    _, terminal = await turn(h, "Ask the fixture question")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result
    assert any(not isinstance(request, ToolApprovalRequest) for request in handler.requests)
    assert "OC2-QUESTION-DONE" in terminal.result.final_output


@pytest.mark.asyncio
async def test_completed_session_resumes_after_managed_service_restart(runtime):
    _, _, work, create = runtime
    ctx = make_context(cwd=str(work), host_session_id="oc2-resume")
    first = create()
    await first.start(ctx)
    _, terminal = await turn(first, "before restart")
    assert terminal.kind is TurnEventKind.FINISHED
    checkpoint = await first.export_checkpoint()
    session_id = first.provider_session_id
    await first.stop()

    resumed = create()
    await resumed.start(
        make_context(
            cwd=str(work),
            host_session_id="oc2-resume",
            checkpoint=checkpoint,
            resume_policy=ResumePolicy.REQUIRE_RESUME,
        )
    )
    assert resumed.provider_session_id == session_id
    _, terminal = await turn(resumed, "after restart")
    assert terminal.kind is TurnEventKind.FINISHED, terminal.result


@pytest.mark.asyncio
async def test_graceful_abort_has_native_interrupted_terminal(runtime):
    _, model, work, create = runtime
    h = create()
    await h.start(make_context(cwd=str(work)))
    model.actions = [{"slow": True}]
    receipt = await h.send(HarnessInput("abort fixture"))
    collecting = asyncio.create_task(collect_turn(h, receipt.turn_id))
    await asyncio.wait_for(model.entered.wait(), 10)
    await h.abort(mode=AbortMode.GRACEFUL)
    events = await asyncio.wait_for(collecting, 20)
    assert_turn_invariants(events, receipt.turn_id)
    assert terminal_of(events).kind is TurnEventKind.ABORTED


@pytest.mark.asyncio
async def test_stop_wakes_stream_and_peer_survives(runtime):
    _, model, work, create = runtime
    h, peer = create(), create()
    await h.start(make_context(cwd=str(work)))
    await peer.start(make_context(cwd=str(work)))
    model.actions = [{"slow": True}]
    receipt = await h.send(HarnessInput("slow fixture"))
    collecting = asyncio.create_task(collect_turn(h, receipt.turn_id))
    await asyncio.wait_for(model.entered.wait(), 10)
    await asyncio.wait_for(h.stop(), 15)
    events = await collecting
    assert_turn_invariants(events, receipt.turn_id)
    assert terminal_of(events).kind is TurnEventKind.ABORTED
    _, terminal = await turn(peer, "peer remains alive")
    assert terminal.kind is TurnEventKind.FINISHED


@pytest.mark.asyncio
async def test_scope_lock_rejects_second_owner_then_releases(runtime):
    _, _, work, create = runtime
    ctx = make_context(cwd=str(work))
    h, competitor = create(), create()
    await h.start(ctx)
    with pytest.raises(Exception, match="failed to start managed OpenCode"):
        await competitor.start(ctx)
    _, terminal = await turn(h, "original remains usable")
    assert terminal.kind is TurnEventKind.FINISHED
    await h.stop()
    await competitor.start(ctx)
    _, terminal = await turn(competitor, "fresh generation")
    assert terminal.kind is TurnEventKind.FINISHED


@pytest.mark.asyncio
async def test_source_drift_blocks_dispatch_and_reaps_service(runtime):
    _, model, work, create = runtime
    h = create()
    await h.start(make_context(cwd=str(work)))
    server, owner = h._server, dict(h._server.owner)
    source = server.root / "config/opencode/.gitignore"
    source.chmod(0o600)
    source.write_text("changed")
    source.chmod(0o400)
    _, terminal = await turn(h, "must not dispatch")
    assert terminal.kind is TurnEventKind.FAILED
    assert terminal.result.error.code == "source_drift"
    assert not model.requests
    assert (await server.properties(owner)).get("ActiveState") != "active"


@pytest.mark.asyncio
async def test_host_sigkill_orphan_recovery(runtime, tmp_path):
    config, _, work, create = runtime
    ctx = make_context(cwd=str(work), host_session_id="oc1-hard-crash")
    config_file, ready = tmp_path / "child-config.json", tmp_path / "child-ready.json"
    write_private(config_file, {"config": asdict(config), "cwd": str(work), "session": ctx.host_session_id})
    code = """import asyncio,json,sys
from pathlib import Path
from openjiuwen.harness_protocol import HarnessContext
from openjiuwen.harness_providers.opencode import OpenCodeHarness,OpenCodeHarnessConfig
async def main():
    d=json.loads(Path(sys.argv[1]).read_text())
    h=OpenCodeHarness(OpenCodeHarnessConfig.from_mapping(d['config']))
    await h.start(HarnessContext('e2e-agent','e2e_e2e-agent',d['session'],'',cwd=d['cwd']))
    Path(sys.argv[2]).write_text(json.dumps({'owner':h._server.owner,'scope':str(h._server.scope)}))
    await asyncio.Event().wait()
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(config_file),
        str(ready),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    old = None
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                assert child.returncode is None
                await asyncio.sleep(0.05)
        old = json.loads(ready.read_text())
        child.kill()  # This test owns the exact host process; no global process matching.
        await child.wait()
        h = create()
        await h.start(ctx)
        assert h._server.owner != old["owner"]
        assert (await h._server.properties(old["owner"])).get("ActiveState") != "active"
        _, terminal = await turn(h, "after hard host crash")
        assert terminal.kind is TurnEventKind.FINISHED
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        if old:
            cleanup = ManagedServer(config, ctx)
            await cleanup.reap(old["owner"])


@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.asyncio
async def test_stubborn_native_tool_reaped_on_stop_or_server_crash(runtime, crash):
    _, model, work, create = runtime
    import shlex

    h = create()
    await h.start(make_context(cwd=str(work)))
    server, owner = h._server, dict(h._server.owner)
    marker = work / "tool.pid"
    script = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    script += f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    model.actions = [
        {"tool": "bash", "args": {"command": "python3 -c " + shlex.quote(script), "description": "owned fixture"}}
    ]
    receipt = await h.send(HarnessInput("run owned stubborn tool"))
    collecting = asyncio.create_task(collect_turn(h, receipt.turn_id))
    async with asyncio.timeout(15):
        while not marker.exists():  # noqa: ASYNC110 - cross-process file, no in-process Event
            await asyncio.sleep(0.05)
    pid = int(marker.read_text())
    if crash:
        # MainPID is our lease wrapper. Kill its exact native CLI child so the
        # test exercises Server death, not only the supervisor's own death.
        main_pid = int((await server.properties(owner))["MainPID"])
        children = Path(f"/proc/{main_pid}/task/{main_pid}/children").read_text().split()  # noqa: ASYNC240
        assert len(children) == 1
        native_pid = int(children[0])
        command = Path(f"/proc/{native_pid}/cmdline").read_bytes().split(b"\0")  # noqa: ASYNC240
        assert command[:2] == [str(server.cli).encode(), b"serve"]
        os.kill(native_pid, signal.SIGKILL)
    else:
        await asyncio.wait_for(h.stop(), 15)
    events = await asyncio.wait_for(collecting, 20)
    assert_turn_invariants(events, receipt.turn_id)
    assert terminal_of(events).kind is (TurnEventKind.FAILED if crash else TurnEventKind.ABORTED)
    assert not Path(f"/proc/{pid}").exists()  # noqa: ASYNC240 - local procfs observation
    assert (await server.properties(owner)).get("ActiveState") != "active"
