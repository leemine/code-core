# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real full-access Codex CLI stop must retire its tool tree, using loopback only."""

import asyncio
import hashlib
import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

import pytest

from openjiuwen.harness_protocol import HarnessContext, HarnessInput
from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig, CodexModelConfig
from tests.system_tests.harness_providers._codex_response_fixture import ResponsesFixture

pytest.importorskip("openai_codex", reason="optional real bundled Codex CLI is required")


def _process(pid):
    path = Path("/proc") / str(pid)
    try:
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        command = (path / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return {
        "pid": int(pid),
        "start": fields[19],
        "state": fields[0],
        "ppid": int(fields[1]),
        "pgid": int(fields[2]),
        "sid": int(fields[3]),
        "command_sha256": hashlib.sha256(command).hexdigest(),
    }


def _native_cli_children(parent_pid):
    children = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        candidate = _process(int(proc.name))
        if candidate is not None and candidate["ppid"] == parent_pid:
            try:
                argv = (proc / "cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            if b"app-server" in argv and Path(argv[0].decode()).name == "codex":
                children.append({**candidate, "kind": "codex-app-server"})
    return children


def _observe(identities):
    result = []
    for old in identities:
        current = _process(old["pid"])
        same = current is not None and current["start"] == old["start"]
        result.append(
            {
                "original": old,
                "current": current,
                "live": same and current["state"] not in {"Z", "X"},
            }
        )
    return result


async def _cleanup_exact(identities):
    """Only the identities created and recorded by this test can be signalled."""
    signalled = []
    for row in _observe(identities):
        if row["live"]:
            os.kill(row["original"]["pid"], signal.SIGTERM)
            signalled.append(row["original"])
    deadline = time.monotonic() + 3
    while any(row["live"] for row in _observe(identities)) and time.monotonic() < deadline:  # noqa: ASYNC110 -- external process identities have no asyncio event
        await asyncio.sleep(0.05)
    for row in _observe(identities):
        if row["live"]:
            os.kill(row["original"]["pid"], signal.SIGKILL)
    return {"signalled": signalled, "remaining": _observe(identities)}


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_latency_s", [0, 0.12])
@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process identity proof")
async def test_full_access_stop_confirms_python_and_sleep_tree_exit(tmp_path, replacement_latency_s):
    home, codex_home, work = (tmp_path / name for name in ("home", "codex", "work"))
    for path in (home, codex_home, work):
        path.mkdir()
    marker = work / "owned.json"
    parent = work / "slow_parent.py"
    parent.write_text(
        "import json,os,subprocess,time\nfrom pathlib import Path\n"
        "child=subprocess.Popen(['sleep','120'])\n"
        "def identity(pid):\n"
        " fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()\n"
        " return {'pid':pid,'start':fields[19],'state':fields[0]}\n"
        f"Path({str(marker)!r}).write_text(json.dumps([identity(os.getpid()),identity(child.pid)]))\n"
        "child.wait()\n"
        f"Path({str(work / 'goal-finished.txt')!r}).write_text('TOOL-FINISHED')\n"
        "print('TOOL-FINISHED')\n"
    )
    unrelated = await asyncio.to_thread(subprocess.Popen, ["sleep", "120"], start_new_session=True)
    unrelated_identity = _process(unrelated.pid)
    harness = None
    consumer = None
    owned = []
    try:
        with ResponsesFixture() as responses:
            responses.items.append(
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "id": "fc_stop_tree",
                    "call_id": "call_stop_tree",
                    "arguments": json.dumps(
                        {
                            "cmd": "python3 " + shlex.quote(str(parent)),
                            "yield_time_ms": 30000,
                        }
                    ),
                }
            )
            config = CodexHarnessConfig(
                inherit_process_env=False,
                bypass_approvals_and_sandbox=True,
                env={"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                model=CodexModelConfig(
                    model="glm-5.2",
                    provider="stop_tree_fixture",
                    api_base=responses.base_url,
                    api_key="local-only",
                ),
            )
            notifications = []

            def observe(notification):
                payload = getattr(notification, "payload", None)
                native_turn = getattr(payload, "turn", None)
                notifications.append(
                    {
                        "at": time.monotonic(),
                        "method": notification.method,
                        "native_turn_id": getattr(native_turn, "id", None),
                        "owned_processes": _observe(owned) if notification.method == "turn/completed" else None,
                    }
                )

            harness = CodexHarness(config, notification_observer=observe)
            await asyncio.wait_for(
                harness.start(
                    HarnessContext(
                        agent_name="stop-tree",
                        agent_id="stop-tree",
                        host_session_id="stop-tree",
                        cwd=str(work),
                        system_prompt="Execute the prescribed local shell tool once.",
                    )
                ),
                25,
            )
            provider = harness._client._client._sync._proc
            owned.append({**_process(provider.pid), "kind": "sdk-owned-process"})
            owned.extend(await asyncio.to_thread(_native_cli_children, provider.pid))
            assert sum(row.get("kind") == "codex-app-server" for row in owned) == 1, owned
            receipt = await harness.send(HarnessInput(content="Run the prescribed slow local tool."))

            async def drain():
                return [envelope async for envelope in harness.turn_events(receipt.turn_id)]

            consumer = asyncio.create_task(drain())
            deadline = time.monotonic() + 20
            while not marker.exists():
                assert time.monotonic() < deadline, "real Codex tool did not create its process marker"
                await asyncio.sleep(0.02)
            owned.extend(json.loads(marker.read_text()))
            before = _observe(owned)
            assert all(row["live"] for row in before), before
            assert harness._active_handle is not None, "must stop while the native tool turn is active"
            # The real Gateway counterexample submitted replacement 116 ms after
            # the tool marker; exercise that observed window as well as direct stop.
            await asyncio.sleep(replacement_latency_s)
            began = time.monotonic()
            await asyncio.wait_for(harness.stop(), 15)
            after = _observe(owned)
            unrelated_after = _process(unrelated.pid)
            evidence = {
                "bypass_approvals_and_sandbox": True,
                "provider_session_id": harness.provider_session_id,
                "turn_id": receipt.turn_id,
                "replacement_latency_s": replacement_latency_s,
                "stop_seconds": time.monotonic() - began,
                "stop_requested_at": began,
                "native_notifications": notifications,
                "before": before,
                "after_stop": after,
                "unrelated_before": unrelated_identity,
                "unrelated_after": unrelated_after,
                "provider_returncode": provider.poll(),
                "model_requests": len(responses.requests),
            }
            (tmp_path / "stop-evidence.json").write_text(json.dumps(evidence, indent=2))
            assert unrelated.poll() is None and unrelated_after["start"] == unrelated_identity["start"]
            assert provider.poll() is not None
            assert not any(row["live"] for row in after), evidence
            await asyncio.sleep(0.25)
            assert not any(row["live"] for row in _observe(owned))
            await asyncio.wait_for(consumer, 3)
    finally:
        try:
            if harness is not None:
                try:
                    await asyncio.wait_for(harness.stop(), 15)
                finally:
                    cleanup = await _cleanup_exact(owned)
                    (tmp_path / "owned-cleanup.json").write_text(json.dumps(cleanup, indent=2))
        finally:
            unrelated.terminate()
            await asyncio.to_thread(unrelated.wait, 3)
            if consumer is not None and not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
