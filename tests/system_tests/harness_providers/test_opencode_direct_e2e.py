# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real direct CLI normal lifecycle; crash descendant recovery is not promised."""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from openjiuwen.harness_protocol import HarnessInput, ResumePolicy, TurnEventKind
from openjiuwen.harness_providers.base import ProviderStartupError
from openjiuwen.harness_providers.opencode import OpenCodeHarness, OpenCodeHarnessConfig

from . import test_opencode_e2e as managed
from ._contract import assert_turn_invariants, collect_turn, make_context, terminal_of

runtime = managed.runtime

pytestmark = pytest.mark.skipif(os.environ.get("RUN_OPENCODE_DIRECT") != "1", reason="real direct CLI opt-in")


@pytest.mark.asyncio
async def test_direct_text_tool_stop_and_resume(runtime):
    config, model, work, create = runtime
    cfg = replace(config, server_mode=OpenCodeHarnessConfig().server_mode)
    h = create(cfg)
    context = make_context(cwd=str(work))
    await h.start(context)
    actions = [{"tool": "bash", "args": {"command": "printf direct-ok > direct.txt", "description": "fixture"}}, {}]
    for action in actions:
        model.actions = [action] if action else []
        receipt = await h.send(HarnessInput("Run fixture"))
        events = await collect_turn(h, receipt.turn_id)
        assert_turn_invariants(events, receipt.turn_id)
        assert terminal_of(events).kind is TurnEventKind.FINISHED
    assert (work / "direct.txt").read_text() == "direct-ok"
    checkpoint = await h.export_checkpoint()
    server, child = h._server, h._server.process
    await h.stop()
    assert child.returncode is not None
    assert not (server.scope / "owner.json").exists()
    restored = create(cfg)
    await restored.start(replace(context, checkpoint=checkpoint, resume_policy=ResumePolicy.REQUIRE_RESUME))
    receipt = await restored.send(HarnessInput("Followup"))
    events = await collect_turn(restored, receipt.turn_id)
    assert terminal_of(events).kind is TurnEventKind.FINISHED


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "stop"])
async def test_direct_normal_running_tool_control(runtime, control):
    config, model, work, create = runtime
    h = create(replace(config, server_mode=OpenCodeHarnessConfig().server_mode))
    await h.start(make_context(cwd=str(work)))
    marker = work / "pid"
    model.actions = [
        {
            "tool": "bash",
            "args": {
                "command": f"echo $$ > {marker}; exec sleep 40",
                "description": "owned ordinary tool",
            },
        }
    ]
    receipt = await h.send(HarnessInput("Run fixture"))
    stream = asyncio.create_task(collect_turn(h, receipt.turn_id))
    async with asyncio.timeout(15):
        while not marker.exists():  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    pid = int(marker.read_text())
    await getattr(h, control)()
    events = await stream
    assert_turn_invariants(events, receipt.turn_id)
    assert terminal_of(events).kind is TurnEventKind.ABORTED
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"  # noqa: ASYNC240
    if control == "abort":
        receipt = await h.send(HarnessInput("Followup"))
        assert terminal_of(await collect_turn(h, receipt.turn_id)).kind is TurnEventKind.FINISHED


@pytest.mark.asyncio
async def test_direct_stale_owner_is_not_adopted(runtime):
    config, _, work, create = runtime
    cfg = replace(config, server_mode=OpenCodeHarnessConfig().server_mode)
    context = make_context(cwd=str(work))
    h = create(cfg)
    await h.start(context)
    server, owner = h._server, dict(h._server.owner)
    await h.stop()
    # Simulate only a stale descriptor; no unrelated process is created/killed.
    descriptor = server.scope / "owner.json"
    descriptor.write_text(json.dumps(owner))
    descriptor.chmod(0o600)
    other = OpenCodeHarness(cfg)
    with pytest.raises(ProviderStartupError) as error:
        await other.start(context)
    assert error.value.error.code == "direct_owner_recovery_required"
    assert json.loads(descriptor.read_text()) == owner
    await other.stop()
    descriptor.unlink()
