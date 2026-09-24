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
    ResumePolicy,
    ToolApprovalDecision,
    ToolApprovalRequest,
    ToolApprovalResponse,
    TurnEventKind,
    UserInputResponse,
)
from openjiuwen.harness_providers.opencode import OpenCodeHarness, OpenCodeHarnessConfig, OpenCodeModelConfig
from openjiuwen.harness_providers.opencode.server import ManagedServer, write_private

from ._contract import assert_turn_invariants, collect_turn, make_context, terminal_of, tool_items

pytestmark = pytest.mark.skipif(os.environ.get("RUN_OPENCODE_OC1") != "1", reason="real managed CLI opt-in")


class ModelFixture:
    def __init__(self):
        self.actions, self.requests = [], []
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


@pytest_asyncio.fixture
async def runtime(tmp_path):
    model = ModelFixture()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", model.respond)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
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
