# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic OpenCode configuration, control, recovery and stream contracts."""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.harness_protocol import (
    AbortMode,
    CheckpointReason,
    CheckpointSaveReceipt,
    ExecutionAuthorization,
    HarnessCapability,
    HarnessContext,
    HarnessInput,
    HarnessProtocolError,
    HostCapability,
    InteractionCancelReason,
    InteractionResponseStatus,
    ItemEventKind,
    ItemLifecycleEvent,
    McpServerConfig,
    McpTransport,
    OutputOperation,
    ResumePolicy,
    ToolApprovalDecision,
    ToolApprovalResponse,
    TurnEventKind,
    TurnStatus,
    UnsupportedHarnessCapabilityError,
    UserInputResponse,
)
from openjiuwen.harness_providers.base import TurnTiming
from openjiuwen.harness_providers.construction import resolve_provider
from openjiuwen.harness_providers.opencode import OpenCodeHarness, OpenCodeHarnessConfig, OpenCodeModelConfig, _launcher
from openjiuwen.harness_providers.opencode.errors import OpenCodeError
from openjiuwen.harness_providers.opencode.mapping import Accumulator
from openjiuwen.harness_providers.opencode.options import environment, native_config, validate_readback
from openjiuwen.harness_providers.opencode.server import ManagedServer, lease, write_private
from openjiuwen.harness_providers.opencode.source import private_directory, sealed_snapshot
from openjiuwen.harness_providers.opencode.transport import Transport, sse_events
from tests.system_tests.harness_providers._contract import assert_turn_invariants, collect_turn, terminal_of


def context(**kwargs):
    return HarnessContext("oc", "agent", "host", "", **kwargs)


def config(**kwargs):
    return OpenCodeHarnessConfig(model=OpenCodeModelConfig("fixture", "http://127.0.0.1:1/v1", "test-secret"), **kwargs)


def event(kind, **props):
    return {"type": kind, "properties": {"sessionID": "ses_s", **props}}


def info(mid="msg_a", **kwargs):
    return {"id": mid, "role": "assistant", "parentID": "msg_u", "sessionID": "ses_s", "time": {}, **kwargs}


def textpart(text="hello", **kwargs):
    return {"id": "prt_t", "messageID": "msg_a", "sessionID": "ses_s", "type": "text", "text": text, **kwargs}


@pytest.mark.parametrize(
    "value",
    [
        {"unknown": 1},
        {"full_access": "true"},
        {"runtime_root": "relative"},
        {"turn_timeout_s": float("nan")},
        {"max_frame_bytes": True},
        {"model": {"model": "m", "api_base": "file:///tmp"}},
        {"model": {"model": "{env:BAD}", "api_base": "https://model"}},
    ],
)
def test_reject_config(value):
    with pytest.raises((ValueError, TypeError)):
        OpenCodeHarnessConfig.from_mapping(value)


def test_authorization_and_separate_model_identity():
    provider = resolve_provider("opencode")
    assert provider.card.name == "opencode"
    parsed = provider.compile_authorization({"full_access": True}, ExecutionAuthorization(False))
    assert parsed["full_access"] is False
    assert provider.legacy_authorization({}).full_access is False
    assert "test-secret" not in repr(config())
    assert native_config(config())["permission"]["*"] == "ask"
    assert native_config(config(full_access=True))["permission"]["*"] == "allow"
    assert native_config(config())["permission"]["question"] == "deny"
    assert native_config(config(), {HostCapability.USER_INPUT})["permission"]["question"] == "allow"
    assert native_config(config())["provider"]["openjiuwen"]["options"]["apiKey"] == "test-secret"
    assert provider.card.supports(HarnessCapability.MCP_TOOLS)
    assert not provider.card.supports(HarnessCapability.NATIVE_TOOLS)


def _product_mcp(**overrides):
    values = {
        "name": "jiuwenswarm_product_tools",
        "transport": McpTransport.HTTP,
        "url": "http://127.0.0.1:43111/mcp",
        "headers": {"Authorization": "Bearer " + "x" * 43},
    }
    values.update(overrides)
    return McpServerConfig(**values)


def test_managed_product_mcp_is_rendered_without_changing_base_config():
    base = native_config(config(), {HostCapability.MCP_SERVERS})
    managed = native_config(
        config(),
        {HostCapability.MCP_SERVERS},
        (_product_mcp(),),
    )

    assert base["mcp"] == {}
    assert managed["mcp"] == {
        "jiuwenswarm_product_tools": {
            "type": "remote",
            "url": "http://127.0.0.1:43111/mcp",
            "headers": {"Authorization": "Bearer " + "x" * 43},
            "oauth": False,
        }
    }


def test_host_mcp_stdio_and_remote_are_compiled_without_oauth_or_interpolation():
    servers = (
        McpServerConfig(
            name="local_tools",
            transport=McpTransport.STDIO,
            command=("/usr/bin/python3", "server.py"),
            env={"FIXTURE": "value"},
        ),
        McpServerConfig(
            name="remote_tools",
            transport=McpTransport.HTTP,
            url="https://mcp.example/mcp?tenant=fixture",
            headers={"X-Fixture": "value"},
        ),
    )
    rendered = native_config(config(), {HostCapability.MCP_SERVERS}, servers)["mcp"]
    assert rendered == {
        "local_tools": {
            "type": "local",
            "command": ["/usr/bin/python3", "server.py"],
            "environment": {"FIXTURE": "value"},
        },
        "remote_tools": {
            "type": "remote",
            "url": "https://mcp.example/mcp?tenant=fixture",
            "headers": {"X-Fixture": "value"},
            "oauth": False,
        },
    }


def test_stable_mcp_identity_excludes_only_generation_local_product_server():
    manifest_server = McpServerConfig(
        name="manifest",
        transport=McpTransport.HTTP,
        url="http://127.0.0.1:43112/mcp",
    )
    rendered = native_config(
        config(),
        {HostCapability.MCP_SERVERS},
        (_product_mcp(), manifest_server),
        include_product_mcp=False,
    )
    assert set(rendered["mcp"]) == {"manifest"}


def test_explicit_skill_path_is_the_only_enabled_skill_source():
    skill_path = Path("/work/.openjiuwen/harness-skills/opencode/config")
    rendered = native_config(config(skills=("/source",)), skill_path=skill_path)
    assert rendered["skills"] == {"paths": [str(skill_path)], "urls": []}
    assert "skills" not in native_config(config())
    with pytest.raises(OpenCodeError, match="explicit_skill_path_required"):
        native_config(config(skills=("/source",)))


@pytest.mark.parametrize(
    "server",
    [
        McpServerConfig(
            name="plain_remote",
            transport=McpTransport.HTTP,
            url="http://mcp.example/mcp",
        ),
        McpServerConfig(
            name="interpolated_command",
            transport=McpTransport.STDIO,
            command=("{env:UNTRUSTED}",),
        ),
        McpServerConfig(
            name="interpolated_header",
            transport=McpTransport.HTTP,
            url="https://mcp.example/mcp",
            headers={"Authorization": "{file:/tmp/secret}"},
        ),
        McpServerConfig(
            name="in_process",
            transport=McpTransport.IN_PROCESS,
            instance=object(),
        ),
    ],
)
def test_uncontrolled_host_mcp_sources_are_rejected(server):
    with pytest.raises(OpenCodeError):
        native_config(config(), {HostCapability.MCP_SERVERS}, (server,))


def test_duplicate_product_mcp_names_are_rejected_even_in_stable_identity():
    with pytest.raises(OpenCodeError, match="invalid_mcp_server_name"):
        native_config(
            config(),
            {HostCapability.MCP_SERVERS},
            (_product_mcp(), _product_mcp()),
            include_product_mcp=False,
        )


@pytest.mark.parametrize(
    "server",
    [
        McpServerConfig(
            name="jiuwenswarm_product_tools",
            transport=McpTransport.STDIO,
            command=("mcp",),
        ),
        _product_mcp(url="https://mcp.example/mcp"),
        _product_mcp(url="http://127.0.0.1:invalid/mcp"),
        _product_mcp(headers={}),
        _product_mcp(name="invalid.name"),
    ],
)
def test_unmanaged_product_mcp_is_rejected(server):
    with pytest.raises(OpenCodeError):
        native_config(config(), {HostCapability.MCP_SERVERS}, (server,))


def test_mcp_context_requires_declared_host_capability():
    harness = OpenCodeHarness()
    with pytest.raises(HarnessProtocolError, match="MCP_SERVERS"):
        harness._validate_context(context(mcp_servers=(_product_mcp(),)))

    harness._validate_context(
        context(
            mcp_servers=(_product_mcp(),),
            host_capabilities=frozenset({HostCapability.MCP_SERVERS}),
        )
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"env": {"HOME": "/bad"}},
        {"tools": object()},
        {"hooks": object()},
    ],
)
@pytest.mark.asyncio
async def test_unsupported_context_before_start(kwargs):
    harness = OpenCodeHarness()
    with pytest.raises(UnsupportedHarnessCapabilityError):
        await harness.start(context(**kwargs))
    assert harness._server is None


@pytest.mark.asyncio
async def test_require_resume_needs_checkpoint_before_start():
    harness = OpenCodeHarness()
    with pytest.raises(HarnessProtocolError, match="without a checkpoint"):
        await harness.start(context(resume_policy=ResumePolicy.REQUIRE_RESUME))


def test_source_environment_and_effective_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"*":"allow"}')
    monkeypatch.setenv("NODE_OPTIONS", "untrusted")
    expected = native_config(config())
    env = environment(tmp_path, expected, "private-password")
    assert "NODE_OPTIONS" not in env and "OPENCODE_PERMISSION" not in env
    assert env["HOME"] == str(tmp_path / "home")
    persistent = tmp_path / "persistent"
    assert environment(tmp_path, expected, "pw", persistent_root=persistent)["XDG_DATA_HOME"] == str(
        persistent / "data"
    )
    actual = {**expected, "agent": {"title": {"disable": True, "permission": {}, "options": {}}}}
    validate_readback(actual, expected)
    with pytest.raises(OpenCodeError):
        validate_readback({**actual, "plugin": ["bad"]}, expected)
    with pytest.raises(OpenCodeError):
        validate_readback({**actual, "permission": {"*": "allow"}}, expected)


def test_snapshot_rejects_drift_links_and_writes(tmp_path):
    root = tmp_path / "sealed"
    root.mkdir(mode=0o700)
    item = root / "config"
    item.write_text("{}")
    item.chmod(0o400)
    root.chmod(0o500)
    baseline = sealed_snapshot(root)
    item.chmod(0o600)
    with pytest.raises(OpenCodeError):
        sealed_snapshot(root)
    item.write_text("changed")
    item.chmod(0o400)
    assert sealed_snapshot(root) != baseline
    root.chmod(0o700)
    item.unlink()
    item.symlink_to(tmp_path / "missing")
    root.chmod(0o500)
    with pytest.raises(OpenCodeError):
        sealed_snapshot(root)
    root.chmod(0o700)


def test_private_storage_and_exclusive_lease(tmp_path):
    tmp_path.chmod(0o700)
    private_directory(tmp_path)
    fd = lease(tmp_path / "owner.lock")
    try:
        with pytest.raises(BlockingIOError):
            lease(tmp_path / "owner.lock")
    finally:
        os.close(fd)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OpenCodeError):
        private_directory(alias)


@pytest.mark.asyncio
async def test_unknown_owner_never_stopped(tmp_path):
    server = ManagedServer(config(), context())
    server.scope = tmp_path
    write_private(tmp_path / "owner.json", {"unit": "someone-else.service", "generation": "x", "description": "x"})
    with pytest.raises(OpenCodeError, match="invalid_owner_descriptor"):
        server.read_owner()
    calls = []

    async def control(*args):
        calls.append(args)
        return 0, "LoadState=loaded\nDescription=other owner\nActiveState=active\n"

    server.control = control
    with pytest.raises(OpenCodeError, match="service_identity_mismatch"):
        await server.reap({"unit": "fixture.service", "description": "expected"})
    assert all(call[0] != "stop" for call in calls)


def test_delayed_launcher_rejects_replaced_generation(tmp_path, monkeypatch):
    generation = tmp_path / "old"
    generation.mkdir()
    write_private(tmp_path / "owner.json", {"generation": "new"})
    write_private(generation / "launch.json", {"owner": {"generation": "old"}})

    def forbidden(*args, **kwargs):
        pytest.fail("stale generation launched a process")

    monkeypatch.setattr(_launcher.subprocess, "call", forbidden)
    assert _launcher.main(generation / "launch.json") == 73


@pytest.mark.asyncio
async def test_cleanup_failure_retains_exact_lease_and_retries(tmp_path):
    server = ManagedServer(config(), context())
    server.scope = tmp_path
    nonce = "a" * 32
    server.owner = {
        "generation": nonce,
        "unit": f"ojw-opencode-{tmp_path.name}-{nonce}.service",
        "description": f"OpenJiuwen OpenCode {tmp_path.name} {nonce}",
    }
    write_private(tmp_path / "owner.json", server.owner)
    server.lock = lease(tmp_path / "host.lock")

    async def reject(_):
        raise OpenCodeError("supervisor_unavailable")

    server.reap = reject
    with pytest.raises(OpenCodeError):
        await server.stop()
    assert server.lock is not None and server.owner is not None
    assert json.loads((tmp_path / "owner.json").read_text()) == server.owner
    with pytest.raises(BlockingIOError):
        lease(tmp_path / "host.lock")

    async def confirm(_):
        pass

    server.reap = confirm
    await server.stop()
    assert server.owner is None and server.lock is None
    assert not (tmp_path / "owner.json").exists()


@pytest.mark.asyncio
async def test_sse_queue_backpressure_and_close_unblocks(monkeypatch):
    class Response:
        status, content_type = 200, "text/event-stream"
        content = Chunks(
            [b'data: {"type":"server.connected","properties":{}}\n\n']
            + [b'data: {"type":"test","properties":{"n":' + str(n).encode() + b"}}\n\n" for n in range(4)]
        )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def get(self, *args, **kwargs):
            return Response()

        async def close(self):
            pass

    monkeypatch.setattr("openjiuwen.harness_providers.opencode.transport.aiohttp.ClientSession", Client)
    transport = Transport(SimpleNamespace(url="http://localhost:1", password="secret"), config(transport_capacity=1))
    await transport.connect()
    assert transport.queue.qsize() == 1
    assert not transport.closed.is_set()
    assert [(await transport.next_event())["properties"]["n"] for _ in range(4)] == list(range(4))
    with pytest.raises(OpenCodeError, match="event_stream_closed"):
        await transport.next_event()
    await transport.close()


@pytest.mark.parametrize(
    "status,expected", [(401, "auth_required"), (429, "rate_limited"), (503, "server_unavailable")]
)
@pytest.mark.asyncio
async def test_http_errors_discard_native_body(monkeypatch, status, expected):
    class Response:
        async def __aenter__(self):
            return SimpleNamespace(status=status)

        async def __aexit__(self, *args):
            pass

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, *args, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response()

        async def close(self):
            pass

    monkeypatch.setattr("openjiuwen.harness_providers.opencode.transport.aiohttp.ClientSession", Client)
    transport = Transport(SimpleNamespace(url="http://localhost:1", password="secret"), config())
    with pytest.raises(OpenCodeError) as caught:
        await transport.request("POST", "/session", {"secret": "canary"})
    assert caught.value.category == expected
    assert "canary" not in repr(caught.value.turn_error())
    await transport.close()


class Chunks:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_sse_fragments_multiline_and_eof():
    raw = b': comment\r\ndata: {"type":"e",\r\ndata: "properties":{"text":"\xe4\xb8\xad"}}\r\n\r\n'
    parser = sse_events(Chunks([raw[i : i + 1] for i in range(len(raw))]), 1024)
    assert (await anext(parser))["properties"]["text"] == "中"
    with pytest.raises(OpenCodeError, match="event_stream_closed"):
        await anext(parser)


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b"data: " + b"x" * 100, "sse_frame_limit"),
        (b"data: []\n\n", "invalid_sse_event"),
        (b'data: {"type":"e","properties":{"x":NaN}}\n\n', "invalid_native_json"),
    ],
)
@pytest.mark.asyncio
async def test_sse_invalid_and_bounded(raw, reason):
    with pytest.raises(OpenCodeError, match=reason):
        await anext(sse_events(Chunks([raw]), 64))


def test_mapping_scopes_snapshots_deltas_usage_and_final():
    acc = Accumulator("ses_s", "msg_u", 10000)
    assert not acc.consume(event("message.updated", info=info(parentID="msg_old")))
    acc.consume(event("message.updated", info=info()))
    first = acc.consume(event("message.part.updated", part=textpart("")))
    assert first[0][0].operation is OutputOperation.SNAPSHOT
    for _ in range(2):
        delta = acc.consume(event("message.part.delta", messageID="msg_a", partID="prt_t", field="text", delta="a"))
        assert delta[0][0].operation is OutputOperation.DELTA
    acc.consume(event("message.part.updated", part=textpart("aa", time={"end": 1})))
    completed = info(time={"completed": 1}, finish="stop", tokens={"input": 4, "output": 2, "total": 6})
    for _ in range(2):
        acc.consume(event("message.updated", info=completed))
    assert acc.usage().input_tokens == 4
    assert acc.usage().cached_input_tokens is None
    assert acc.is_idle(event("session.idle"))
    acc.reconcile({"info": completed, "parts": [textpart("aa", time={"end": 1})]})
    result = acc.result(TurnTiming())
    assert result.final_output == "aa" and result.status is TurnStatus.COMPLETED
    assert result.messages[0].content[0].content == "aa"
    with pytest.raises(OpenCodeError, match="terminal_reconciliation_failed"):
        acc.reconcile({"info": {**completed, "parentID": "msg_old"}, "parts": []})


def test_mapping_tool_lifecycle_is_once_and_preserves_blocks():
    acc = Accumulator("ses_s", "msg_u", 10000)
    acc.consume(event("message.updated", info=info()))
    part = textpart(
        type="tool", tool="bash", callID="call1", state={"status": "running", "input": {"command": "echo x"}}
    )
    events = acc.consume(event("message.part.updated", part=part))
    part = {**part, "state": {**part["state"], "status": "completed", "output": "x"}}
    events += acc.consume(event("message.part.updated", part=part))
    events += acc.consume(event("message.part.updated", part=part))
    lifecycle = [p.kind for p, _ in events if isinstance(p, ItemLifecycleEvent)]
    assert lifecycle.count(ItemEventKind.STARTED) == lifecycle.count(ItemEventKind.COMPLETED) == 1
    assert [b.kind for b in acc.result(TurnTiming()).messages[0].content] == ["tool_call", "tool_result"]


@pytest.mark.parametrize("kind", ["permission.asked", "question.asked", "session.error"])
def test_mapping_errors_are_nonsecret(kind):
    acc = Accumulator("ses_s", "msg_u", 10000)
    with pytest.raises(OpenCodeError) as failure:
        acc.consume(event(kind, error={"name": "APIError", "data": {"message": "test-secret", "statusCode": 401}}))
    assert "test-secret" not in repr(failure.value.turn_error())


def test_interaction_ledger_rejects_cross_turn_and_conflicting_duplicates():
    acc = Accumulator("ses_s", "msg_u", 10000)
    acc.consume(event("message.updated", info=info()))
    asked = event(
        "permission.asked",
        id="per_one",
        permission="bash",
        metadata={},
        tool={"messageID": "msg_a", "callID": "call_1"},
    )
    assert acc.claim_interaction(asked) == ("per_one", "call_1")
    assert acc.claim_interaction(asked) is None
    with pytest.raises(OpenCodeError, match="interaction_identity_conflict"):
        acc.claim_interaction({**asked, "properties": {**asked["properties"], "permission": "write"}})
    wrong_session = {**asked, "properties": {**asked["properties"], "sessionID": "ses_other", "id": "per_two"}}
    assert acc.claim_interaction(wrong_session) is None
    wrong_message = {
        **asked,
        "properties": {
            **asked["properties"],
            "id": "per_three",
            "tool": {"messageID": "msg_other", "callID": "call_2"},
        },
    }
    with pytest.raises(OpenCodeError, match="interaction_scope_mismatch"):
        acc.claim_interaction(wrong_message)


class FakeTransport:
    def __init__(self, mode):
        self.mode = mode
        self.queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.failure = None
        self.requests = []
        self.history = []

    async def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        if method == "POST" and path == "/session":
            return {"id": "ses_s"}
        if method == "GET" and path == "/session/ses_s":
            return {"id": "ses_s"}
        if method == "GET" and path == "/session/status":
            return {}
        if method == "GET" and path in {"/permission", "/question"}:
            return []
        if method == "GET" and path == "/session/ses_s/message":
            return self.history
        if method == "GET":
            return {"info": self.completed, "parts": [textpart("done")]}
        self.completed = info(parentID=body["messageID"], finish="stop", time={"completed": 1})
        self.history = [{"info": self.completed, "parts": [textpart("done")]}]
        if self.mode != "idle":
            await self.queue.put(event("message.updated", info=self.completed))
        if self.mode != "eof":
            await self.queue.put(event("session.idle"))
        else:
            await self.queue.put(None)

    async def next_event(self):
        value = await self.queue.get()
        if value is None:
            raise OpenCodeError("event_stream_closed")
        return value

    async def close(self):
        self.closed.set()
        await self.queue.put(None)


class FakeHarness(OpenCodeHarness):
    def __init__(self, mode):
        super().__init__(config(turn_timeout_s=0.2))
        self.mode = mode

    async def _open_session(self, ctx):
        self._transport = FakeTransport(self.mode)
        session_id, resumed = await self._activate_session(ctx)
        self._session_id = session_id
        await self._publish_session_checkpoint(
            reason=CheckpointReason.SESSION_ACTIVATED,
            resumable=True,
            state="idle",
            resumed=resumed,
        )
        return session_id

    async def _verify(self):
        pass


@pytest.mark.parametrize(
    "mode,terminal", [("normal", TurnEventKind.FINISHED), ("idle", TurnEventKind.FAILED), ("eof", TurnEventKind.FAILED)]
)
@pytest.mark.asyncio
async def test_shared_lifecycle_one_terminal_no_idle_or_eof_success(mode, terminal):
    harness = FakeHarness(mode)
    await harness.start(context())
    try:
        receipt = await harness.send(HarnessInput("test"))
        events = await collect_turn(harness, receipt.turn_id)
        assert_turn_invariants(events, receipt.turn_id)
        assert terminal_of(events).kind is terminal
        if mode != "normal":
            receipt = await harness.send(HarnessInput("must not replay"))
            events = await collect_turn(harness, receipt.turn_id)
            assert terminal_of(events).result.error.code == "session_requires_restart"
    finally:
        await harness.stop()


class AnsweringHandler:
    def __init__(self, response=None, *, blocked=False):
        self.response = response
        self.requests = []
        self.cancelled = []
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def handle(self, request):
        self.requests.append(request)
        await self.release.wait()
        return self.response(request) if callable(self.response) else self.response

    async def cancel(self, request_id, *, reason=InteractionCancelReason.PROVIDER_WITHDREW):
        self.cancelled.append((request_id, reason))
        self.release.set()


class InteractiveTransport(FakeTransport):
    def __init__(self, mode):
        super().__init__(mode)
        self.user_id = None
        self.native_replies = []

    async def request(self, method, path, body=None):
        if method == "POST" and path.endswith("/prompt_async"):
            self.requests.append((method, path, body))
            self.user_id = body["messageID"]
            self.completed = info(parentID=self.user_id)
            await self.queue.put(event("message.updated", info=self.completed))
            if self.mode in {"approval", "abort", "disconnect"}:
                await self.queue.put(
                    event(
                        "permission.asked",
                        id="per_req",
                        permission="bash",
                        patterns=["echo ok"],
                        always=["echo *"],
                        metadata={"command": "echo ok"},
                        tool={"messageID": "msg_a", "callID": "call_1"},
                    )
                )
            else:
                await self.queue.put(
                    event(
                        "question.asked",
                        id="que_req",
                        questions=[
                            {
                                "header": "Choice",
                                "question": "Continue?",
                                "options": [{"label": "Yes", "description": "Proceed"}],
                            }
                        ],
                        tool={"messageID": "msg_a", "callID": "call_1"},
                    )
                )
            return None
        if method == "POST" and ("/permissions/" in path or path.startswith("/question/")):
            self.requests.append((method, path, body))
            self.native_replies.append((path, body))
            if self.mode == "abort":
                return None
            allowed = body is not None and (body.get("response") in {"once", "always"} or "answers" in body)
            if allowed:
                self.completed = info(parentID=self.user_id, finish="stop", time={"completed": 1})
                self.history = [{"info": self.completed, "parts": [textpart("done")]}]
                await self.queue.put(event("message.updated", info=self.completed))
                await self.queue.put(event("session.idle"))
            else:
                self.completed = info(parentID=self.user_id, finish="tool-calls", time={"completed": 1})
                denied = textpart(
                    type="tool",
                    tool="bash",
                    callID="call_1",
                    state={"status": "error", "input": {"command": "echo ok"}, "error": "rejected"},
                )
                self.history = [{"info": self.completed, "parts": [denied]}]
                await self.queue.put(event("message.updated", info=self.completed))
                await self.queue.put(event("message.part.updated", part=denied))
                await self.queue.put(event("session.idle"))
            return None
        if method == "POST" and path.endswith("/abort"):
            self.requests.append((method, path, body))
            self.completed = info(
                parentID=self.user_id,
                time={"completed": 1},
                error={"name": "MessageAbortedError", "data": {"message": "Aborted"}},
            )
            await self.queue.put(event("message.updated", info=self.completed))
            await self.queue.put(event("session.idle"))
            return True
        if method == "GET" and "/message/" in path:
            return self.history[-1]
        return await super().request(method, path, body)


class InteractiveHarness(FakeHarness):
    async def _open_session(self, ctx):
        self._transport = InteractiveTransport(self.mode)
        session_id, resumed = await self._activate_session(ctx)
        self._session_id = session_id
        await self._publish_session_checkpoint(
            reason=CheckpointReason.SESSION_ACTIVATED,
            resumable=True,
            state="idle",
            resumed=resumed,
        )
        return session_id


@pytest.mark.asyncio
async def test_permission_allow_and_deny_are_scoped_and_terminal():
    allow = AnsweringHandler(lambda request: ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW))
    harness = InteractiveHarness("approval")
    await harness.start(
        context(
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=allow,
        )
    )
    receipt = await harness.send(HarnessInput("allow"))
    events = await collect_turn(harness, receipt.turn_id)
    assert terminal_of(events).kind is TurnEventKind.FINISHED
    assert allow.requests[0].call_id == "call_1"
    assert harness._transport.native_replies == [("/session/ses_s/permissions/per_req", {"response": "once"})]
    await harness.stop()

    deny = AnsweringHandler(lambda request: ToolApprovalResponse(request.request_id, ToolApprovalDecision.DENY))
    harness = InteractiveHarness("approval")
    await harness.start(
        context(
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=deny,
        )
    )
    receipt = await harness.send(HarnessInput("deny"))
    events = await collect_turn(harness, receipt.turn_id)
    terminal = terminal_of(events)
    assert terminal.kind is TurnEventKind.FAILED
    assert terminal.result.error.code == "interaction_declined"
    await harness.stop()


@pytest.mark.asyncio
async def test_question_round_trip_uses_native_positional_answers():
    handler = AnsweringHandler(
        lambda request: UserInputResponse(
            request.request_id,
            InteractionResponseStatus.COMPLETED,
            {"answers": {"Continue?": "Yes"}},
        )
    )
    harness = InteractiveHarness("question")
    await harness.start(
        context(
            host_capabilities=frozenset({HostCapability.USER_INPUT}),
            interactions=handler,
        )
    )
    receipt = await harness.send(HarnessInput("question"))
    events = await collect_turn(harness, receipt.turn_id)
    assert terminal_of(events).kind is TurnEventKind.FINISHED
    assert handler.requests[0].choices == ("Yes",)
    assert harness._transport.native_replies == [("/question/que_req/reply", {"answers": [["Yes"]]})]
    await harness.stop()


@pytest.mark.asyncio
async def test_abort_cancels_pending_interaction_and_is_not_success():
    handler = AnsweringHandler(
        lambda request: ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW),
        blocked=True,
    )
    harness = InteractiveHarness("abort")
    await harness.start(
        context(
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=handler,
        )
    )
    receipt = await harness.send(HarnessInput("abort"))
    for _ in range(100):
        if handler.requests:
            break
        await asyncio.sleep(0.001)
    await harness.abort(mode=AbortMode.GRACEFUL)
    events = await collect_turn(harness, receipt.turn_id)
    terminal = terminal_of(events)
    assert terminal.kind is TurnEventKind.ABORTED
    assert terminal.result.status is TurnStatus.INTERRUPTED
    assert handler.cancelled == [("opencode-approval:per_req", InteractionCancelReason.TURN_ABORTED)]
    await harness.stop()


@pytest.mark.asyncio
async def test_disconnect_while_waiting_marks_result_unknown_and_cancels_host_request():
    handler = AnsweringHandler(
        lambda request: ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW),
        blocked=True,
    )
    harness = InteractiveHarness("disconnect")
    await harness.start(
        context(
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=handler,
        )
    )
    receipt = await harness.send(HarnessInput("disconnect"))
    for _ in range(100):
        if handler.requests:
            break
        await asyncio.sleep(0.001)
    harness._transport.failure = OpenCodeError("event_stream_closed", category="server_unavailable")
    harness._transport.closed.set()
    events = await collect_turn(harness, receipt.turn_id)
    terminal = terminal_of(events)
    assert terminal.kind is TurnEventKind.FAILED
    assert terminal.result.error.code == "event_stream_closed"
    assert terminal.result.error.retryable is True
    assert handler.cancelled == [("opencode-approval:per_req", InteractionCancelReason.PROVIDER_WITHDREW)]
    await harness.stop()


class RecordingSink:
    def __init__(self):
        self.saved = []

    async def save(self, checkpoint, *, reason, expected_storage_revision=None):
        self.saved.append((checkpoint, reason, expected_storage_revision))
        return CheckpointSaveReceipt(checkpoint.checkpoint_id, checkpoint.sequence, f"rev-{len(self.saved)}")


@pytest.mark.asyncio
async def test_completed_session_checkpoint_resumes_without_creating_session():
    sink = RecordingSink()
    harness = FakeHarness("normal")
    await harness.start(context(checkpoint_sink=sink))
    receipt = await harness.send(HarnessInput("one"))
    await collect_turn(harness, receipt.turn_id)
    checkpoint = await harness.export_checkpoint()
    assert checkpoint.data["resumable"] is True
    assert [reason for _, reason, _ in sink.saved] == [
        CheckpointReason.SESSION_ACTIVATED,
        CheckpointReason.STATE_CHANGED,
        CheckpointReason.TURN_COMPLETED,
    ]
    await harness.stop()

    resumed = FakeHarness("normal")
    await resumed.start(context(checkpoint=checkpoint, resume_policy=ResumePolicy.REQUIRE_RESUME))
    assert not any(method == "POST" and path == "/session" for method, path, _ in resumed._transport.requests)
    assert (await resumed.export_checkpoint()).data["resumed"] is True
    await resumed.stop()

    unsafe = replace(
        checkpoint,
        checkpoint_id="unsafe",
        data={**dict(checkpoint.data), "resumable": False, "state": "turn_active"},
    )
    rejected = FakeHarness("normal")
    with pytest.raises(HarnessProtocolError, match="confirmed idle session"):
        await rejected.start(context(checkpoint=unsafe, resume_policy=ResumePolicy.REQUIRE_RESUME))
