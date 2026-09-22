# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locked CLI readable-root capability and CodexHarness integration regressions."""

import asyncio
import json
import shlex
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from openjiuwen.harness_protocol import (
    HarnessContext,
    HarnessInput,
    HostCapability,
    InteractionResponseStatus,
    ProviderInteractionResponse,
    ResumePolicy,
    ToolApprovalDecision,
    ToolApprovalResponse,
    TurnEventKind,
)
from openjiuwen.harness_providers.base import ProviderStartupError
from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig, CodexModelConfig
from openjiuwen.harness_providers.codex.options import build_codex_config
from openjiuwen.harness_providers.codex.sdk_compat import isolate_process_environment
from pydantic import RootModel

from tests.system_tests.harness_providers._codex_response_fixture import ResponsesFixture

sdk = pytest.importorskip("openai_codex", reason="optional locked SDK/CLI required")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux sandbox probe")


@pytest.fixture
def scope():
    cache = Path(__file__).resolve().parents[3] / ".pytest_cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="a0-readroots-", dir=cache) as directory:
        root = Path(directory)
        for name in ("home", "codex", "work/private", "reference", "outside"):
            (root / name).mkdir(parents=True)
        for name in ("work", "reference", "outside", "work/private"):
            (root / name / "marker.txt").write_text("A0-MARKER-" + name)
        (root / "work/link.txt").symlink_to(root / "outside/marker.txt")
        (root / "work/relative.txt").symlink_to("../outside/marker.txt")
        binary = Path(sdk.client._resolve_codex_bin(sdk.CodexConfig()))
        rules = {
            ":minimal": "read", str(root / "work"): "read", str(root / "reference"): "read",
            str(root / "work/private"): "deny", str(root / "codex/tmp"): "read", str(binary.parent): "read",
        }
        config = 'default_permissions = "a0-read"\n[permissions.a0-read.filesystem]\n'
        config += "\n".join(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in rules.items())
        config += "\n[permissions.a0-read.network]\nenabled=false\n"
        (root / "codex/config.toml").write_text(config)
        yield root


def _config(root, responses=None):
    return CodexHarnessConfig(
        inherit_process_env=False,
        env={"HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"), "PATH": "/usr/bin:/bin"},
        model=CodexModelConfig(model="gpt-5.6-sol", provider="a0_fixture",
                               api_base=responses.base_url, api_key="local-only") if responses else None,
    )


@asynccontextmanager
async def _client(root, responses=None):
    config = _config(root, responses)
    client = sdk.AsyncCodex(config=build_codex_config(
        sdk=sdk, config=config, model=config.model, cwd=str(root / "work"), env=config.env, mcp_servers=(),
    ))
    isolate_process_environment(client, sdk)
    client._client._sync._approval_handler = lambda method, params: {"decision": "decline"}
    proc = None
    try:
        await client._ensure_initialized()
        proc = client._client._sync._proc
        yield client
    finally:
        await asyncio.wait_for(client.close(), 10)
        if proc is not None:
            assert proc.poll() is not None


async def _rpc(client, method, params):
    response = await client._client.request(method, params, response_model=RootModel[dict])
    return response.root


@pytest.mark.asyncio
@pytest.mark.parametrize("target,allowed", [
    ("work/marker.txt", True), ("reference/marker.txt", True), ("outside/marker.txt", False),
    ("work/link.txt", False), ("work/relative.txt", False), ("work/private/marker.txt", False),
    ("work/../outside/marker.txt", False),
])
async def test_readable_roots_enforce_actual_reads(scope, target, allowed):
    async with _client(scope) as client:
        result = await _rpc(client, "command/exec", {
            "command": ["/bin/cat", str(scope / target)], "cwd": str(scope / "work"),
            "permissionProfile": "a0-read", "timeoutMs": 3000,
        })
        print(json.dumps({"target": target, "result": result}))
        assert (result["exitCode"] == 0) is allowed
        assert ("A0-MARKER-" in result["stdout"]) is allowed
        if not allowed:
            assert "No such file" in result["stderr"] or "Permission denied" in result["stderr"]


@pytest.mark.asyncio
async def test_unknown_profile_is_rejected_before_command(scope):
    from openai_codex.errors import CodexRpcError

    async with _client(scope) as client:
        with pytest.raises(CodexRpcError) as error:
            await _rpc(client, "command/exec", {
                "command": ["/bin/cat", str(scope / "outside/marker.txt")],
                "permissionProfile": "missing-a0-profile", "timeoutMs": 3000,
            })
        print("unknown profile rejected:", error.value)


@pytest.mark.asyncio
async def test_profile_survives_cold_resume_with_real_tool_calls(scope):
    with ResponsesFixture() as responses:
        thread_id = None
        for resume in (False, True):
            async with _client(scope, responses) as client:
                params = {"cwd": str(scope / "work"), "approvalPolicy": "untrusted", "approvalsReviewer": "user"}
                if resume:
                    params["threadId"] = thread_id
                effective = await _rpc(client, "thread/resume" if resume else "thread/start", params)
                assert effective["activePermissionProfile"]["id"] == "a0-read"
                if resume:
                    assert effective["thread"]["id"] == thread_id
                thread_id = effective["thread"]["id"]
                # Generated 0.144.4 response types discard this security metadata.
                response_type = (sdk.generated.v2_all.ThreadResumeResponse if resume
                                 else sdk.generated.v2_all.ThreadStartResponse)
                typed = response_type.model_validate(effective).model_dump(by_alias=True)
                assert "activePermissionProfile" not in typed
                before = len(responses.requests)
                for index, target in enumerate(("work/marker.txt", "outside/marker.txt", "work/link.txt")):
                    responses.items.append({
                        "type": "function_call", "name": "exec_command", "id": f"fc_{resume}_{index}",
                        "call_id": f"call_{resume}_{index}",
                        "arguments": json.dumps({"cmd": f"cat {shlex.quote(str(scope / target))}", "login": False}),
                    })
                result = await asyncio.wait_for(
                    sdk.AsyncThread(client, thread_id).run("Run the fixed read probes."), 20,
                )
                assert result.error is None
                outputs = [item for r in responses.requests[before:] for item in r.get("input", [])
                           if item.get("type") == "function_call_output" and
                           item.get("call_id", "").startswith(f"call_{resume}_")]
                rendered = json.dumps(outputs)
                assert "A0-MARKER-work" in rendered
                assert "A0-MARKER-outside" not in rendered
                assert {item["call_id"] for item in outputs} == {f"call_{resume}_{i}" for i in range(3)}
                print(json.dumps({"resume": resume, "activePermissionProfile": effective["activePermissionProfile"],
                                  "outputs": outputs}))



class _DenyApproval:
    async def handle(self, request):
        return ToolApprovalResponse(request_id=request.request_id, decision=ToolApprovalDecision.DENY)

    async def cancel(self, request_id, *, reason=None):
        pass


def _context(scope):
    return HarnessContext(
        agent_name="a0", agent_id="a0", host_session_id="a0", system_prompt="Read profile integration probe",
        cwd=str(scope / "work"), host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
        interactions=_DenyApproval(),
    )


@pytest.mark.asyncio
async def test_core_preserves_named_read_roots_on_start_and_cold_resume(scope):
    with ResponsesFixture() as responses:
        checkpoint, thread_id, fingerprint = None, None, None
        for resume in (False, True):
            before = len(responses.requests)
            targets = ("work/marker.txt", "reference/marker.txt", "outside/marker.txt", "work/link.txt")
            for index, target in enumerate(targets):
                responses.items.append({
                    "type": "function_call", "name": "exec_command", "id": f"fc_core_{resume}_{index}",
                    "call_id": f"call_core_{resume}_{index}",
                    "arguments": json.dumps({"cmd": f"cat {shlex.quote(str(scope / target))}", "login": False}),
                })
            harness = CodexHarness(_config(scope, responses))
            context = _context(scope)
            if resume:
                context = replace(context, checkpoint=checkpoint, resume_policy=ResumePolicy.REQUIRE_RESUME)
            proc = None
            try:
                await asyncio.wait_for(harness.start(context), 15)
                proc = harness._client._client._sync._proc
                if resume:
                    assert harness.provider_session_id == thread_id
                    assert harness._permission_fingerprint == fingerprint
                thread_id, fingerprint = harness.provider_session_id, harness._permission_fingerprint
                assert len(fingerprint) == 64
                receipt = await harness.send(HarnessInput(content="Run the fixed read probes."))
                events = [event async for event in harness.turn_events(receipt.turn_id)]
                assert events[-1].event.kind is TurnEventKind.FINISHED
                checkpoint = await harness.export_checkpoint()
                outputs = [item for r in responses.requests[before:] for item in r.get("input", [])
                           if item.get("type") == "function_call_output" and
                           item.get("call_id", "").startswith(f"call_core_{resume}_")]
                rendered = json.dumps(outputs)
                assert "A0-MARKER-work" in rendered and "A0-MARKER-reference" in rendered
                assert "A0-MARKER-outside" not in rendered
                assert {item["call_id"] for item in outputs} == {f"call_core_{resume}_{i}" for i in range(4)}
                print(json.dumps({"core_resume": resume, "fingerprint": fingerprint, "outputs": outputs}))
            finally:
                await asyncio.wait_for(harness.stop(), 10)
            assert proc is not None and proc.poll() is not None


@pytest.mark.asyncio
async def test_core_rejects_changed_profile_before_cold_resume(scope, monkeypatch):
    with ResponsesFixture() as responses:
        harness = CodexHarness(_config(scope, responses))
        try:
            await harness.start(_context(scope))
            receipt = await harness.send(HarnessInput(content="Return fixed marker."))
            events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert events[-1].event.kind is TurnEventKind.FINISHED
            checkpoint = await harness.export_checkpoint()
        finally:
            await harness.stop()
        config_path = scope / "codex/config.toml"
        content = config_path.read_text().replace('enabled=false', 'enabled=true')
        config_path.write_text(content)
        clients, calls = [], []
        original = sdk.AsyncCodex

        def recording_client(*, config):
            client = original(config=config)
            clients.append(client)
            request = client._client.request

            async def record(method, params, **kwargs):
                calls.append(method)
                return await request(method, params, **kwargs)

            client._client.request = record
            return client

        monkeypatch.setattr(sdk, "AsyncCodex", recording_client)
        harness = CodexHarness(_config(scope, responses))
        try:
            with pytest.raises(ProviderStartupError) as error:
                await harness.start(replace(_context(scope), checkpoint=checkpoint,
                                            resume_policy=ResumePolicy.REQUIRE_RESUME))
            assert "permission configuration changed" in str(error.value.__cause__)
            assert calls == ["config/read"]
            assert clients[0]._client._sync._proc is None
            print(json.dumps({"changed_profile_rejected": True, "requests": calls}))
        finally:
            await harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ratify", [True, False])
async def test_real_auth_fallback_and_declined_restore_preserve_profile(scope, monkeypatch, ratify):
    approvals, snapshots, procs = [], [], []
    original = sdk.AsyncCodex

    def recording_client(*, config):
        client = original(config=config)
        request = client._client.request

        async def record(method, params, **kwargs):
            result = await request(method, params, **kwargs)
            if method in ("thread/start", "thread/resume"):
                snapshots.append(result.model_dump(mode="json"))
                procs.append(client._client._sync._proc)
            return result

        client._client.request = record
        return client

    class Handler(_DenyApproval):
        async def handle(self, request):
            if getattr(request, "request_type", None) == "auth_fallback":
                approvals.append(request)
                return ProviderInteractionResponse(
                    request_id=request.request_id,
                    status=InteractionResponseStatus.COMPLETED if ratify else InteractionResponseStatus.DECLINED,
                )
            return await super().handle(request)

    monkeypatch.setattr(sdk, "AsyncCodex", recording_client)
    with ResponsesFixture(status_code=401) as native, ResponsesFixture() as fallback:
        config = replace(_config(scope, native), fallback_model=_config(scope, fallback).model)
        harness = CodexHarness(config)
        context = replace(_context(scope), interactions=Handler(), host_capabilities=frozenset({
            HostCapability.TOOL_APPROVAL, HostCapability.PROVIDER_INTERACTION,
        }))
        selected = fallback if ratify else native
        for index, target in enumerate(("work/marker.txt", "outside/marker.txt")):
            selected.items.append({
                "type": "function_call", "name": "exec_command", "id": f"fc_fallback_{index}",
                "call_id": f"call_fallback_{index}",
                "arguments": json.dumps({"cmd": f"cat {shlex.quote(str(scope / target))}", "login": False}),
            })
        try:
            await harness.start(context)
            fingerprint = harness._permission_fingerprint
            receipt = await harness.send(HarnessInput(content="Run fixed fallback probes."))
            events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert len(approvals) == 1
            assert harness.fallback_activated is ratify
            assert len(snapshots) == (2 if ratify else 3)
            assert {item["thread"]["id"] for item in snapshots} == {harness.provider_session_id}
            assert all(item["activePermissionProfile"]["id"] == "a0-read" for item in snapshots)
            assert harness._permission_fingerprint == fingerprint
            if not ratify:
                assert events[-1].event.kind is TurnEventKind.FAILED
                assert not fallback.requests
                native.status_code = 200
                receipt = await harness.send(HarnessInput(content="Read after declined fallback restoration."))
                events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert events[-1].event.kind is TurnEventKind.FINISHED
            outputs = [item for r in selected.requests for item in r.get("input", [])
                       if item.get("type") == "function_call_output"]
            assert "A0-MARKER-work" in json.dumps(outputs)
            assert "A0-MARKER-outside" not in json.dumps(outputs)
            assert {item["call_id"] for item in outputs} == {"call_fallback_0", "call_fallback_1"}
            print(json.dumps({"fallback_ratified": ratify, "connections": len(snapshots), "outputs": outputs}))
        finally:
            await asyncio.wait_for(harness.stop(), 10)
        assert all(proc.poll() is not None for proc in procs)
