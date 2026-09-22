# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real SDK/CLI with scripted loopback responses; no remote model or user configuration."""

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from openjiuwen.harness_protocol import HarnessContext, HarnessInput, HostCapability, ResumePolicy, TurnEventKind
from openjiuwen.harness_providers.codex import (
    CodexHarness,
    CodexHarnessConfig,
    CodexModelConfig,
    CodexNativePluginConfig,
    native_plugin_content_digest,
)

from tests.system_tests.harness_providers._codex_response_fixture import ResponsesFixture


@pytest.fixture
def responses():
    with ResponsesFixture() as server:
        yield server


sdk = pytest.importorskip("openai_codex", reason="optional Codex SDK and bundled CLI required")


class _NoInteraction:
    async def handle(self, request):
        raise AssertionError("This fixture must not request additional authority")

    async def cancel(self, request_id, *, reason=None):
        pass


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux process environment inspection")
async def test_real_start_resume_confirms_host_policy_and_excludes_parent_environment(tmp_path, monkeypatch, responses):
    monkeypatch.setenv("A0_PARENT_SECRET", "must-not-reach-cli")
    home, codex_home, work = (tmp_path / name for name in ("home", "codex", "work"))
    for path in (home, codex_home, work):
        path.mkdir()
    snapshots = []
    client_factory = sdk.AsyncCodex

    def recording_client(*, config):
        client = client_factory(config=config)
        request = client._client.request

        async def record(method, params, **kwargs):
            result = await request(method, params, **kwargs)
            if method in ("thread/start", "thread/resume"):
                snapshots.append((method, result.model_dump(by_alias=True, mode="json")))
            return result

        client._client.request = record
        return client

    monkeypatch.setattr(sdk, "AsyncCodex", recording_client)
    config = CodexHarnessConfig(
        inherit_process_env=False,
        model=CodexModelConfig(
            model="gpt-5.6-sol", provider="a0_fixture", api_base=responses.base_url, api_key="local-only",
        ),
        env={"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": "/usr/bin:/bin", "A0_SELECTED": "yes"},
        # An ambient conflicting setting must not defeat explicit host policy.
        config_overrides=('approval_policy="never"', 'approvals_reviewer="auto_review"'),
    )
    context = HarnessContext(
        agent_name="a0", agent_id="a0", host_session_id="a0", system_prompt="Local startup probe",
        cwd=str(work), host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}), interactions=_NoInteraction(),
    )
    session_id = None
    checkpoint = None
    for resume in (False, True):
        harness = CodexHarness(config)
        proc = None
        try:
            selected = context
            if resume:
                selected = replace(context, resume_policy=ResumePolicy.REQUIRE_RESUME, checkpoint=checkpoint)
            await asyncio.wait_for(harness.start(selected), 25)
            proc = harness._client._client._sync._proc
            raw_env = await asyncio.to_thread(Path(f"/proc/{proc.pid}/environ").read_bytes)
            environ = raw_env.split(b"\0")
            assert b"A0_PARENT_SECRET=must-not-reach-cli" not in environ
            assert b"A0_SELECTED=yes" in environ
            assert f"CODEX_HOME={codex_home}".encode() in environ
            if resume:
                assert harness.provider_session_id == session_id
            else:
                session_id = harness.provider_session_id
                receipt = await harness.send(HarnessInput(content="Return the fixed local fixture marker."))
                events = [event async for event in harness.turn_events(receipt.turn_id)]
                assert events[-1].event.kind is TurnEventKind.FINISHED
                checkpoint = await harness.export_checkpoint()
        finally:
            await asyncio.wait_for(harness.stop(), 10)
        assert proc is not None and proc.poll() is not None
    assert [method for method, _ in snapshots] == ["thread/start", "thread/resume"]
    for _, effective in snapshots:
        assert effective["approvalPolicy"] == "untrusted"
        assert effective["approvalsReviewer"] == "user"
        assert effective["sandbox"]["type"] == "readOnly"


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [False, True, None])
@pytest.mark.parametrize("tool", ["exec_command", "apply_patch"])
async def test_real_cli_host_decision_controls_file_side_effect(tmp_path, responses, tool, allow):
    import json

    from openjiuwen.harness_protocol import ToolApprovalDecision, ToolApprovalResponse

    home, codex_home, work = (tmp_path / name for name in ("home", "codex", "work"))
    for path in (home, codex_home, work):
        path.mkdir()
    requests = []
    waiting = asyncio.Event()
    cancelled = asyncio.Event()

    class Handler:
        waiter = None

        async def handle(self, request):
            requests.append(request)
            if allow is None:
                waiting.set()
                self.waiter = asyncio.get_running_loop().create_future()
                try:
                    await self.waiter
                finally:
                    cancelled.set()
            return ToolApprovalResponse(
                request_id=request.request_id,
                decision=ToolApprovalDecision.ALLOW if allow else ToolApprovalDecision.DENY,
            )

        async def cancel(self, request_id, *, reason=None):
            if self.waiter is not None:
                self.waiter.cancel()

    if tool == "exec_command":
        item = {
            "type": "function_call", "name": tool, "id": "fc_a0", "call_id": "call_a0",
            "arguments": json.dumps({
                "cmd": "printf A0-MARKER > marker.txt", "workdir": str(work), "login": False,
            }),
        }
    else:
        item = {
            "type": "custom_tool_call", "name": tool, "id": "fc_a0", "call_id": "call_a0",
            "input": "*** Begin Patch\n*** Add File: marker.txt\n+A0-MARKER\n*** End Patch",
        }
    responses.items.append(item)
    config = CodexHarnessConfig(
        inherit_process_env=False,
        env={"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": "/usr/bin:/bin"},
        model=CodexModelConfig(
            model="gpt-5.6-sol", provider="a0_fixture", api_base=responses.base_url, api_key="local-only",
        ),
    )
    harness = CodexHarness(config)
    proc = None
    try:
        await harness.start(HarnessContext(
            agent_name="a0", agent_id="a0", host_session_id="a0", system_prompt="Local tool fixture",
            cwd=str(work), host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}), interactions=Handler(),
        ))
        proc = harness._client._client._sync._proc
        receipt = await harness.send(HarnessInput(content="Execute the prescribed local test tool once."))
        if allow is None:
            await asyncio.wait_for(waiting.wait(), 15)
            await asyncio.wait_for(harness.stop(), 10)
            await asyncio.wait_for(cancelled.wait(), 2)
            assert not harness._pending_interactions
            assert not await asyncio.to_thread((work / "marker.txt").exists)
            assert proc.poll() is not None
            return
        events = [event async for event in harness.turn_events(receipt.turn_id)]
        assert events[-1].event.kind is TurnEventKind.FINISHED
        outputs = [x for x in responses.requests[-1].get("input", []) if x.get("type") == "function_call_output"]
        assert len(requests) == 1, outputs
        assert requests[0].tool_name == ("shell" if tool == "exec_command" else "apply_patch")
        exists = await asyncio.to_thread((work / "marker.txt").exists)
        assert exists is allow
        if allow:
            assert (await asyncio.to_thread((work / "marker.txt").read_text)).strip() == "A0-MARKER"
    finally:
        await harness.stop()
    assert proc is not None and proc.poll() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_mcp", [False, True])
@pytest.mark.parametrize("allow_skill", [False, True])
async def test_real_native_plugin_skill_read_and_mcp_call(tmp_path, responses, allow_mcp, allow_skill):
    import json
    import shlex
    import sys

    home, codex_home, work = (tmp_path / name for name in ("home", "codex", "work"))
    market = tmp_path / "market"
    plugin = market / "plugins" / "a0-probe"
    for path in (
        home,
        codex_home,
        codex_home / "skills",
        work,
        market / ".agents/plugins",
        plugin / ".codex-plugin",
        plugin / "skills/marker",
    ):
        path.mkdir(parents=True)
    (market / ".agents/plugins/marketplace.json").write_text(json.dumps({
        "name": "a0-local", "plugins": [{
            "name": "a0-probe", "source": {"source": "local", "path": "./plugins/a0-probe"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        }],
    }))
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
        "name": "a0-probe", "version": "0.0.1", "description": "A0 local probe",
        "skills": "./skills/", "mcpServers": "./.mcp.json",
    }))
    (plugin / "skills/marker/SKILL.md").write_text(
        "---\nname: marker\ndescription: Read the A0 local test marker.\n---\nA0-SKILL-CONTENT\n",
    )
    marker = work / "mcp-called.txt"
    pidfile = work / "mcp.pid"
    server_code = (
        "import os\nfrom pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "m=FastMCP('a0')\n@m.tool()\ndef marker() -> str:\n"
        '    """Return the isolated test marker."""\n'
        f"    Path({str(marker)!r}).write_text('A0-MCP-CALLED')\n"
        "    return 'A0-MCP-RESULT'\nm.run(transport='stdio')\n"
    )
    (plugin / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"a0_probe": {"command": sys.executable, "args": ["-c", server_code]}},
    }))
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": "/usr/bin:/bin"}
    binary = str(sdk.client._resolve_codex_bin(sdk.CodexConfig()))
    for args in (("plugin", "marketplace", "add", str(market)), ("plugin", "add", "a0-probe@a0-local")):
        process = await asyncio.create_subprocess_exec(
            binary, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
            assert process.returncode == 0, (stdout, stderr)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    config_path = codex_home / "config.toml"
    installed_config = config_path.read_text(encoding="utf-8")
    permissions = {
        ":minimal": "read",
        str(work): "read",
        str(codex_home): "read",
        str(Path(sys.executable).parent): "read",
    }
    permission_config = 'default_permissions = "c1-plugin"\n[permissions.c1-plugin.filesystem]\n'
    permission_config += "\n".join(
        f"{json.dumps(path)} = {json.dumps(access)}" for path, access in permissions.items()
    )
    permission_config += "\n[permissions.c1-plugin.network]\nenabled=false\n"
    config_path.write_text(permission_config + installed_config, encoding="utf-8")
    installed_plugin = codex_home / "plugins/cache/a0-local/a0-probe/0.0.1"
    skill = installed_plugin / "skills/marker/SKILL.md"
    responses.items.extend([
        {
            "type": "function_call", "name": "exec_command", "id": "fc_skill", "call_id": "call_skill",
            "arguments": json.dumps({"cmd": f"cat {shlex.quote(str(skill))}", "login": False}),
        },
        {
            "type": "function_call", "namespace": "mcp__a0_probe", "name": "marker",
            "id": "fc_mcp", "call_id": "call_mcp",
            "arguments": "{}",
        },
    ])
    native_plugin = CodexNativePluginConfig(
        plugin_id="a0-probe@a0-local",
        source_type="local",
        source_locator=str(plugin),
        version="0.0.1",
        content_sha256=native_plugin_content_digest(installed_plugin),
        mcp_server_names=("a0_probe",),
    )
    config = CodexHarnessConfig(
        inherit_process_env=False, env=env,
        startup_source_roots=(str(work), str(codex_home / "plugins"), str(codex_home / "skills"), str(market)),
        native_plugins=(native_plugin,),
        model=CodexModelConfig(
            model="gpt-5.6-sol", provider="a0_fixture", api_base=responses.base_url, api_key="local-only",
        ),
    )
    from openjiuwen.harness_protocol import ToolApprovalDecision, ToolApprovalResponse

    approvals = []

    class PluginReadApproval:
        async def handle(self, request):
            approvals.append(request)
            if request.tool_name == "shell":
                assert str(skill) in str(request.arguments["command"])
                decision = ToolApprovalDecision.ALLOW if allow_skill else ToolApprovalDecision.DENY
            else:
                assert request.tool_name == "mcp__a0_probe"
                assert request.arguments["_meta"]["codex_approval_kind"] == "mcp_tool_call"
                decision = ToolApprovalDecision.ALLOW if allow_mcp else ToolApprovalDecision.DENY
            return ToolApprovalResponse(request_id=request.request_id, decision=decision)

        async def cancel(self, request_id, *, reason=None):
            pass

    harness = CodexHarness(config)
    proc = None
    try:
        await harness.start(HarnessContext(
            agent_name="a0", agent_id="a0", host_session_id="a0", system_prompt="Local plugin fixture",
            cwd=str(work), host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=PluginReadApproval(),
        ))
        proc = harness._client._client._sync._proc
        inventory = await harness._client._client.request(
            "mcpServerStatus/list", {"threadId": harness.provider_session_id},
            response_model=sdk.generated.v2_all.ListMcpServerStatusResponse,
        )
        assert any(server.name == "a0_probe" and "marker" in server.tools for server in inventory.data)
        receipt = await harness.send(HarnessInput(content="Read the local plugin skill and call its marker tool."))
        events = [event async for event in harness.turn_events(receipt.turn_id)]
        assert events[-1].event.kind is TurnEventKind.FINISHED, events[-1].event.result.error
        outputs = [x for r in responses.requests for x in r.get("input", []) if x.get("type") == "function_call_output"]
        assert (await asyncio.to_thread(marker.exists)) is allow_mcp, outputs
        assert any(request.tool_name == "mcp__a0_probe" for request in approvals)
        rendered = json.dumps(outputs)
        assert ("A0-SKILL-CONTENT" in rendered) is allow_skill
        if allow_mcp:
            assert await asyncio.to_thread(marker.read_text) == "A0-MCP-CALLED"
            assert "A0-MCP-RESULT" in rendered
        else:
            assert "user rejected MCP tool call" in rendered
        assert "a0-probe:marker" in json.dumps(responses.requests[0])
    finally:
        await harness.stop()
    assert proc is not None and proc.poll() is not None
    pid = int(await asyncio.to_thread(pidfile.read_text))
    def wait_for_mcp_exit():
        deadline = time.monotonic() + 5
        while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not Path(f"/proc/{pid}").exists()

    await asyncio.to_thread(wait_for_mcp_exit)
    if allow_skill and allow_mcp:
        current = config_path.read_text(encoding="utf-8")
        disabled = current.replace("enabled = true", "enabled = false")
        assert disabled != current
        config_path.write_text(disabled, encoding="utf-8")
        disabled_harness = CodexHarness(replace(config, native_plugins=(replace(native_plugin, enabled=False),)))
        try:
            await disabled_harness.start(HarnessContext(
                agent_name="a0-disabled",
                agent_id="a0-disabled",
                host_session_id="a0-disabled",
                system_prompt="Disabled plugin fixture",
                cwd=str(work),
                host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
                interactions=PluginReadApproval(),
            ))
            inventory = await disabled_harness._client._client.request(
                "mcpServerStatus/list",
                {"threadId": disabled_harness.provider_session_id},
                response_model=sdk.generated.v2_all.ListMcpServerStatusResponse,
            )
            assert not inventory.data
        finally:
            await disabled_harness.stop()
