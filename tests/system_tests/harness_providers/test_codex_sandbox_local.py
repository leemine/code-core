# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Linux sandbox boundary checks without a model or personal configuration."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio
from openjiuwen.harness_protocol import HarnessContext, HostCapability
from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig

sdk = pytest.importorskip("openai_codex", reason="optional locked SDK/CLI required")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux sandbox and /proc probes")


@pytest.fixture
def sandbox_root():
    # CLI 0.144.4 refuses to install its sandbox helper under /tmp.
    cache = Path(__file__).resolve().parents[3] / ".pytest_cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="a0-sandbox-", dir=cache) as directory:
        root = Path(directory)
        for name in ("home", "codex", "work", "outside"):
            (root / name).mkdir()
        yield root


@pytest_asyncio.fixture
async def sandbox(sandbox_root):
    class NoApproval:
        async def handle(self, request):
            raise AssertionError("OS sandbox checks must not grant a host escalation")

        async def cancel(self, request_id, *, reason=None):
            pass

    root = sandbox_root
    harness = CodexHarness(CodexHarnessConfig(
        inherit_process_env=False,
        env={"HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"), "PATH": "/usr/bin:/bin"},
    ))
    proc = None
    try:
        await asyncio.wait_for(harness.start(HarnessContext(
            agent_name="a0", agent_id="a0", host_session_id="a0", system_prompt="Local sandbox boundary probe",
            cwd=str(root / "work"), host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
            interactions=NoApproval(),
        )), 15)
        proc = harness._client._client._sync._proc
        yield harness
    finally:
        await asyncio.wait_for(harness.stop(), 10)
        if proc is not None:
            assert proc.poll() is not None
        assert not harness._pending_interactions


def _policy(mode, network=False):
    if mode == "readOnly":
        return {"type": mode, "networkAccess": network}
    return {
        "type": mode, "networkAccess": network, "writableRoots": [],
        "excludeSlashTmp": True, "excludeTmpdirEnvVar": True,
    }


async def _exec(harness, root, command, policy, command_timeout_ms=3000):
    result = await asyncio.wait_for(harness._client._client.request(
        "command/exec", {"command": command, "cwd": str(root / "work"), "sandboxPolicy": policy,
                         "timeoutMs": command_timeout_ms},
        response_model=sdk.generated.v2_all.CommandExecResponse,
    ), 12)
    print(json.dumps({"policy": policy, "result": result.model_dump(by_alias=True)}, ensure_ascii=False))
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readOnly", "workspaceWrite"])
@pytest.mark.parametrize("location", ["work", "outside"])
async def test_legacy_sandbox_read_scope_is_not_limited_to_cwd(sandbox, sandbox_root, mode, location):
    target = sandbox_root / location / "read.txt"
    target.write_text("A0-READ-CONTROL")
    result = await _exec(sandbox, sandbox_root, ["/bin/cat", str(target)], _policy(mode))
    assert result.exit_code == 0
    assert result.stdout.strip() == "A0-READ-CONTROL"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readOnly", "workspaceWrite"])
@pytest.mark.parametrize("location", ["work", "outside", "symlink"])
async def test_sandbox_write_boundary_includes_symlink_targets(sandbox, sandbox_root, mode, location):
    target = sandbox_root / ("outside" if location == "symlink" else location) / "write.txt"
    target.write_text("A0-ORIGINAL")
    path = target
    if location == "symlink":
        path = sandbox_root / "work" / "linked.txt"
        path.symlink_to(target)
    program = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('A0-CHANGED')"
    result = await _exec(sandbox, sandbox_root, [sys.executable, "-c", program, str(path)], _policy(mode))
    allowed = mode == "workspaceWrite" and location == "work"
    assert (result.exit_code == 0) is allowed
    assert target.read_text() == ("A0-CHANGED" if allowed else "A0-ORIGINAL")
    if not allowed:
        assert "PermissionError" in result.stderr or "Read-only file system" in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readOnly", "workspaceWrite"])
async def test_sandbox_blocks_loopback_network_with_live_positive_control(sandbox, sandbox_root, mode):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"A0-NETWORK-CONTROL")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/a0"
    command = [
        sys.executable, "-c",
        "import urllib.request,sys; print(urllib.request.urlopen(sys.argv[1],timeout=1).read().decode())", url,
    ]
    try:
        allowed = await _exec(sandbox, sandbox_root, command, _policy(mode, network=True))
        assert allowed.exit_code == 0 and "A0-NETWORK-CONTROL" in allowed.stdout
        assert hits == ["/a0"]
        denied = await _exec(sandbox, sandbox_root, command, _policy(mode, network=False))
        assert denied.exit_code != 0
        assert "A0-NETWORK-CONTROL" not in denied.stdout
        assert hits == ["/a0"]
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _owned_processes(programs):
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) <= 1:
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if not argv[0] or Path(os.fsdecode(argv[0])).resolve() != Path(sys.executable).resolve():
                continue
            if b"-c" not in argv or argv[argv.index(b"-c") + 1] not in programs:
                continue
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] != "Z":
                found[int(entry.name)] = stat[19]  # Host PID plus process start time.
        except (OSError, IndexError):
            continue
    return found


def _observe_owned(programs, *, present):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = _owned_processes(programs)
        if (present and len(found) == 2) or (not present and not found):
            return found
        time.sleep(0.02)
    raise AssertionError(f"Unexpected owned process state: {found}, expected present={present}")


@pytest.mark.asyncio
async def test_command_timeout_stops_child_and_session_remains_usable(sandbox, sandbox_root):
    # A unique task directory is embedded in both argv programs, never infer a
    # host PID from os.getpid() inside the sandbox PID namespace.
    child = (
        f"marker={str(sandbox_root)!r}; "
        "import time; from pathlib import Path; time.sleep(30); Path('late.txt').write_text('unexpected')"
    )
    program = (
        f"marker={str(sandbox_root)!r}; "
        "import subprocess,sys,os,time,json; "
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
        "print(json.dumps([os.getpid(),p.pid]),flush=True); time.sleep(30)"
    )
    programs = {program.encode(), child.encode()}
    task = asyncio.create_task(_exec(
        sandbox, sandbox_root, [sys.executable, "-u", "-c", program, child], _policy("workspaceWrite"),
        command_timeout_ms=2000,
    ))
    observed = {}
    try:
        observed = await asyncio.to_thread(_observe_owned, programs, present=True)
        result = await task
        assert result.exit_code != 0
        assert len(json.loads(result.stdout.strip())) == 2
        await asyncio.to_thread(_observe_owned, programs, present=False)
        print(json.dumps({"owned_host_pids": list(observed), "remaining": []}))
        assert not (sandbox_root / "work/late.txt").exists()
    finally:
        # Cleanup only matching, same-start-time processes already observed as
        # ours. Never signal namespaced PIDs or unrelated processes.
        for pid, started in _owned_processes(programs).items():
            if observed.get(pid) == started:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
        if not task.done():
            await task
    control = await _exec(sandbox, sandbox_root, ["/bin/echo", "A0-AFTER-TIMEOUT"], _policy("readOnly"))
    assert control.exit_code == 0 and control.stdout.strip() == "A0-AFTER-TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readOnly", "workspaceWrite"])
async def test_network_policy_covers_ipv4_ipv6_tcp_udp_socket_creation(sandbox, sandbox_root, mode):
    program = """import socket,json
results = []
for family in (socket.AF_INET,socket.AF_INET6):
    for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM):
        try:
            with socket.socket(family,kind):
                results.append(0)
        except OSError as exc:
            results.append(exc.errno)
print(json.dumps(results))
"""
    for network in (True, False):
        result = await _exec(sandbox, sandbox_root, [sys.executable, "-c", program], _policy(mode, network))
        assert result.exit_code == 0
        assert json.loads(result.stdout) == ([0] * 4 if network else [1] * 4)


@pytest.mark.asyncio
async def test_real_turn_trusted_outside_read_does_not_consult_host_approval(sandbox_root):
    import shlex

    from openjiuwen.harness_protocol import HarnessInput, TurnEventKind
    from openjiuwen.harness_providers.codex import CodexModelConfig

    from tests.system_tests.harness_providers._codex_response_fixture import ResponsesFixture

    root = sandbox_root
    target = root / "outside/read.txt"
    target.write_text("A0-OUTSIDE-READ-VISIBLE")
    requests = []

    class NoApproval:
        async def handle(self, request):
            requests.append(request)
            raise AssertionError("Trusted read should run within the functional sandbox")

        async def cancel(self, request_id, *, reason=None):
            pass

    with ResponsesFixture() as responses:
        responses.items.append({
            "type": "function_call", "name": "exec_command", "id": "fc_read", "call_id": "call_read",
            "arguments": json.dumps({"cmd": f"cat {shlex.quote(str(target))}", "login": False}),
        })
        harness = CodexHarness(CodexHarnessConfig(
            inherit_process_env=False,
            env={"HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"), "PATH": "/usr/bin:/bin"},
            model=CodexModelConfig(
                model="gpt-5.6-sol", provider="a0_fixture", api_base=responses.base_url, api_key="local-only",
            ),
        ))
        proc = None
        try:
            await harness.start(HarnessContext(
                agent_name="a0", agent_id="a0", host_session_id="a0", system_prompt="Local read boundary probe",
                cwd=str(root / "work"), host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
                interactions=NoApproval(),
            ))
            proc = harness._client._client._sync._proc
            receipt = await harness.send(HarnessInput(content="Read the fixed boundary fixture."))
            events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert events[-1].event.kind is TurnEventKind.FINISHED
            outputs = [x for r in responses.requests for x in r.get("input", [])
                       if x.get("type") == "function_call_output"]
            assert "A0-OUTSIDE-READ-VISIBLE" in json.dumps(outputs)
            assert not requests
            print(json.dumps({"outside_read_visible": True, "host_approval_calls": len(requests)}))
        finally:
            await harness.stop()
        assert proc is not None and proc.poll() is not None
