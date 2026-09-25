# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in OC6 qualification against a real remote OpenCode model.

RUN_OPENCODE_OC6=1 OPENCODE_OC6_API_KEY=... pytest \
    tests/system_tests/harness_providers/test_opencode_remote_e2e.py

The credential is supplied explicitly and is never copied from ambient OpenCode
state.  The managed Provider still uses its pinned CLI and isolated native
configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openjiuwen.harness_protocol import HarnessInput, TurnEventKind
from openjiuwen.harness_providers.opencode import (
    OpenCodeHarness,
    OpenCodeHarnessConfig,
    OpenCodeModelConfig,
)

from ._contract import assert_turn_invariants, collect_turn, make_context, terminal_of, tool_items


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_OPENCODE_OC6") != "1",
    reason="real remote OpenCode model qualification is opt-in",
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    assert value, f"{name} is required when RUN_OPENCODE_OC6=1"
    return value


@pytest.mark.asyncio
async def test_remote_model_tool_usage_and_owned_cleanup(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir()
    config = OpenCodeHarnessConfig(
        cli_path=os.environ.get(
            "OPENCODE_OC6_CLI",
            os.path.expanduser("~/.opencode/bin/opencode"),
        ),
        runtime_root=str(runtime_root),
        model=OpenCodeModelConfig(
            model=os.environ.get("OPENCODE_OC6_MODEL", "glm-5.2"),
            api_base=_required_environment("OPENCODE_OC6_API_BASE"),
            api_key=_required_environment("OPENCODE_OC6_API_KEY"),
            provider="oc6_volcano",
        ),
        full_access=True,
        turn_timeout_s=180,
    )
    harness = OpenCodeHarness(config)
    server = None
    owner = None
    try:
        await harness.start(
            make_context(cwd=str(work), host_session_id="oc6-remote-core")
        )
        server = harness._server
        owner = dict(server.owner)
        receipt = await harness.send(
            HarnessInput(
                "Use the bash tool exactly once to run "
                "`printf OC6-REMOTE-TOOL > oc6-remote.txt`. "
                "After the tool succeeds, reply with exactly OC6-REMOTE-FINAL."
            )
        )
        events = await collect_turn(harness, receipt.turn_id)
        assert_turn_invariants(events, receipt.turn_id)
        terminal = terminal_of(events)
        assert terminal.kind is TurnEventKind.FINISHED, terminal.result
        assert "OC6-REMOTE-FINAL" in terminal.result.final_output
        assert (work / "oc6-remote.txt").read_text() == "OC6-REMOTE-TOOL"
        assert tool_items(events)
        assert terminal.result.usage.total_tokens is not None
        assert terminal.result.usage.total_tokens > 0
    finally:
        await harness.stop()

    assert server is not None and owner is not None
    assert (await server.properties(owner)).get("ActiveState") != "active"
    assert not (server.scope / "owner.json").exists()
