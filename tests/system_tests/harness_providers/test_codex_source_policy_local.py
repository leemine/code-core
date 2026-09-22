# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Restricted source admission using the locked CLI and local Responses."""

import asyncio
import json
from dataclasses import replace

import pytest
from openjiuwen.harness_protocol import HarnessInput, ResumePolicy, TurnEventKind
from openjiuwen.harness_providers.codex import CodexHarness

from tests.system_tests.harness_providers._codex_response_fixture import ResponsesFixture
from tests.system_tests.harness_providers.test_codex_read_roots_local import _config, _context
from tests.system_tests.harness_providers.test_codex_read_roots_local import scope as scope

pytest.importorskip("openai_codex", reason="optional locked SDK/CLI required")


def _restricted_config(scope, responses=None):
    cache = scope / "codex/skills"
    cache.mkdir(exist_ok=True)
    return replace(_config(scope, responses), startup_source_roots=(str(scope / "work"), str(cache)))


@pytest.mark.asyncio
async def test_restricted_start_and_resume_disable_ambient_agents(scope):
    (scope / "outside/AGENTS.md").write_text("A0-FORBIDDEN-AGENTS")
    (scope / "work/AGENTS.md").symlink_to(scope / "outside/AGENTS.md")
    with ResponsesFixture() as responses:
        config = _restricted_config(scope, responses)
        checkpoint = None
        for resume in (False, True):
            harness = CodexHarness(config)
            proc = None
            try:
                context = _context(scope)
                if resume:
                    context = replace(context, checkpoint=checkpoint, resume_policy=ResumePolicy.REQUIRE_RESUME)
                await asyncio.wait_for(harness.start(context), 20)
                proc = harness._client._client._sync._proc
                receipt = await harness.send(HarnessInput(content="Return fixed marker."))
                events = [event async for event in harness.turn_events(receipt.turn_id)]
                assert events[-1].event.kind is TurnEventKind.FINISHED, events[-1].event.result.error
                checkpoint = await harness.export_checkpoint()
                assert "A0-FORBIDDEN-AGENTS" not in json.dumps(responses.requests)
                print(json.dumps({"restricted_resume": resume, "agents_body_suppressed": True}))
            finally:
                await harness.stop()
            assert proc is not None and proc.poll() is not None


@pytest.mark.asyncio
async def test_admitted_skill_is_copied_and_visible_with_restricted_startup(scope):
    from openjiuwen.harness_providers.skills import SkillSource

    source = scope / "reference/skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: admitted\ndescription: A0-ADMITTED-DESCRIPTION\n---\nA0-ADMITTED-BODY\n",
    )
    with ResponsesFixture() as responses:
        base = _restricted_config(scope, responses)
        config = replace(base, skills=(SkillSource(str(source)),),
                         startup_source_roots=(*base.startup_source_roots, str(scope / "reference")))
        harness = CodexHarness(config)
        try:
            await harness.start(_context(scope))
            receipt = await harness.send(HarnessInput(content="Return fixed marker."))
            events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert events[-1].event.kind is TurnEventKind.FINISHED, events[-1].event.result.error
            assert "A0-ADMITTED-DESCRIPTION" in json.dumps(responses.requests[0])
            copied = scope / "work/.agents/skills/admitted/SKILL.md"
            assert copied.read_text() == (source / "SKILL.md").read_text()
            print(json.dumps({"admitted_skill_copied": True, "metadata_visible": True}))
        finally:
            await harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source", "discovery", "mcp_config", "plugin", "hook", "override"])
async def test_unadmitted_startup_never_constructs_cli(scope, monkeypatch, kind):
    import openai_codex
    from openjiuwen.harness_protocol import HarnessProtocolError
    from openjiuwen.harness_providers.skills import SkillSource

    config = _restricted_config(scope)
    if kind == "source":
        (scope / "outside/SKILL.md").write_text("---\nname: forbidden\n---\nA0-FORBIDDEN\n")
        config = replace(config, skills=(SkillSource(str(scope / "outside")),))
    elif kind == "discovery":
        (scope / "work/.agents/skills").mkdir(parents=True)
        (scope / "work/.agents/skills/escape").symlink_to(scope / "outside")
    elif kind == "mcp_config":
        path = scope / "codex/config.toml"
        path.write_text(path.read_text() + '\n[mcp_servers.blocked]\ncommand="/never/run"\n')
    elif kind == "plugin":
        (scope / "codex/plugins").mkdir()
    elif kind == "hook":
        (scope / "codex/hooks.json").write_text("{}")
    else:
        config = replace(config, config_overrides=('mcp_servers.blocked.command="/never/run"',))
    calls = []

    def no_client(**kwargs):
        calls.append(True)
        raise AssertionError("Rejected sources must not construct any CLI client")

    monkeypatch.setattr(openai_codex, "AsyncCodex", no_client)
    with pytest.raises(HarnessProtocolError):
        await CodexHarness(config).start(_context(scope))
    assert not calls
    assert not list((scope / "codex").glob("sessions/**/*"))
    print(json.dumps({"blocked_source": kind, "cli_constructed": False}))


@pytest.mark.asyncio
async def test_new_unadmitted_discovery_fails_next_turn_and_closes_client(scope):
    with ResponsesFixture() as responses:
        harness = CodexHarness(_restricted_config(scope, responses))
        proc = None
        try:
            await harness.start(_context(scope))
            proc = harness._client._client._sync._proc
            (scope / "work/.agents/skills").mkdir(parents=True)
            (scope / "work/.agents/skills/escape").symlink_to(scope / "outside")
            receipt = await harness.send(HarnessInput(content="Return fixed marker."))
            events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert events[-1].event.kind is TurnEventKind.FAILED
            assert not responses.requests
            assert proc.poll() is not None
            print(json.dumps({"new_source_rejected_before_model": True, "client_closed": True}))
        finally:
            await harness.stop()


@pytest.mark.asyncio
async def test_restricted_commands_keep_named_read_boundary(scope):
    import shlex

    with ResponsesFixture() as responses:
        for index, location in enumerate(("work", "outside")):
            target = scope / location / "marker.txt"
            responses.items.append({
                "type": "function_call", "name": "exec_command", "id": f"fc_{index}", "call_id": f"read_{index}",
                "arguments": json.dumps({"cmd": f"cat {shlex.quote(str(target))}", "login": False}),
            })
        harness = CodexHarness(_restricted_config(scope, responses))
        try:
            await harness.start(_context(scope))
            receipt = await harness.send(HarnessInput(content="Run fixed reads."))
            events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert events[-1].event.kind is TurnEventKind.FINISHED, events[-1].event.result.error
            outputs = [item for request in responses.requests for item in request.get("input", [])
                       if item.get("type") == "function_call_output"]
            assert {item["call_id"] for item in outputs} == {"read_0", "read_1"}
            assert "A0-MARKER-work" in json.dumps(outputs)
            assert "A0-MARKER-outside" not in json.dumps(outputs)
            print(json.dumps({"restricted_command_allowed": True, "outside_command_denied": True}))
        finally:
            await harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ratify", [False, True])
async def test_restricted_fallback_uses_same_source_admission(scope, monkeypatch, ratify):
    from tests.system_tests.harness_providers import test_codex_read_roots_local as probes

    original = probes._config

    def restricted(root, responses=None):
        cache = root / "codex/skills"
        cache.mkdir(exist_ok=True)
        return replace(original(root, responses), startup_source_roots=(str(root / "work"), str(cache)))

    monkeypatch.setattr(probes, "_config", restricted)
    await probes.test_real_auth_fallback_and_declined_restore_preserve_profile(scope, monkeypatch, ratify)


@pytest.mark.asyncio
async def test_writable_profile_stays_untrusted_and_resumes_without_persisting_trust(scope):
    from openjiuwen.harness_protocol import ToolApprovalDecision, ToolApprovalResponse
    from pydantic import RootModel

    class Approve:
        async def handle(self, request):
            return ToolApprovalResponse(request_id=request.request_id, decision=ToolApprovalDecision.ALLOW)

        async def cancel(self, request_id, *, reason=None):
            pass

    path = scope / "codex/config.toml"
    original = path.read_text().replace(f'{json.dumps(str(scope / "work"))} = "read"',
                                        f'{json.dumps(str(scope / "work"))} = "write"')
    path.write_text(original)
    checkpoint = None
    with ResponsesFixture() as responses:
        for index in range(2):
            responses.items.extend([
                {"type": "function_call", "name": "exec_command", "id": f"write_{index}",
                 "call_id": f"write_{index}", "arguments": json.dumps({
                     "cmd": f"printf WRITE-{index} > result.txt", "login": False, "workdir": str(scope / "work"),
                 })},
                {"type": "function_call", "name": "exec_command", "id": f"outside_{index}",
                 "call_id": f"outside_{index}", "arguments": json.dumps({
                     "cmd": f"cat {scope / 'outside/marker.txt'}", "login": False,
                 })},
            ])
            harness = CodexHarness(_restricted_config(scope, responses))
            proc = None
            try:
                context = replace(_context(scope), interactions=Approve())
                if checkpoint:
                    context = replace(context, checkpoint=checkpoint, resume_policy=ResumePolicy.REQUIRE_RESUME)
                await harness.start(context)
                proc = harness._client._client._sync._proc
                effective = await harness._client._client.request(
                    "config/read", {"cwd": str(scope / "work"), "includeLayers": False},
                    response_model=RootModel[dict],
                )
                values = effective.root["config"]
                assert values["projects"] == {str(scope / "work"): {"trust_level": "untrusted"}}
                assert values["features"]["plugins"] is False
                receipt = await harness.send(HarnessInput(content="Run the fixed writes and boundary check."))
                events = [event async for event in harness.turn_events(receipt.turn_id)]
                assert events[-1].event.kind is TurnEventKind.FINISHED, events[-1].event.result.error
                assert (scope / "work/result.txt").read_text() == f"WRITE-{index}"
                checkpoint = await harness.export_checkpoint()
                assert path.read_text() == original
                assert "A0-MARKER-outside" not in json.dumps(responses.requests)
            finally:
                await harness.stop()
            assert proc is not None and proc.poll() is not None
    print(json.dumps({"writable_start_resume": True, "trust_persisted": False, "outside_read_blocked": True}))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "restricted", "restricted_failure"])
async def test_plugin_startup_git_outlives_legacy_cli_but_is_disabled_when_restricted(scope, mode):
    import sys
    import time
    from pathlib import Path

    from tests.system_tests.harness_providers.test_codex_read_roots_local import _config

    restricted = mode != "legacy"

    def wait_for_file(path):
        deadline = time.monotonic() + 5
        while not path.exists():
            assert time.monotonic() < deadline, str(path)
            time.sleep(0.02)

    started, release, finished = (scope / name for name in ("git-started", "git-release", "git-finished"))
    bin_dir = scope / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        "import os,sys,time\nfrom pathlib import Path\n"
        "if sys.argv[1:] != ['ls-remote','https://github.com/openai/plugins.git','HEAD']: sys.exit(1)\n"
        f"Path({str(started)!r}).write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+12\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(0.02)\n"
        f"Path({str(finished)!r}).write_text('finished after CLI stop')\n",
    )
    git.chmod(0o755)
    config = _restricted_config(scope) if restricted else _config(scope)
    config = replace(config, env={**config.env, "PATH": f"{bin_dir}:/usr/bin:/bin",
                                  "HTTPS_PROXY": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"},
                     config_overrides=("features.remote_plugin=false",))
    harness = CodexHarness(config)
    proc = None
    try:
        await harness.start(_context(scope))
        proc = harness._client._client._sync._proc
        if not restricted:
            await asyncio.to_thread(wait_for_file, started)
        if mode == "restricted_failure":
            (scope / "work/.agents/skills").mkdir(parents=True)
            (scope / "work/.agents/skills/escape").symlink_to(scope / "outside")
            receipt = await harness.send(HarnessInput(content="Must fail before reaching a model."))
            events = [event async for event in harness.turn_events(receipt.turn_id)]
            assert events[-1].event.kind is TurnEventKind.FAILED
        await harness.stop()
        assert proc.poll() is not None
        if restricted:
            assert not started.exists()
            assert not (scope / "codex/plugins").exists()
            assert not list((scope / "codex/.tmp").glob("plugins-clone-*"))
        else:
            pid = int(started.read_text())
            assert await asyncio.to_thread(Path(f"/proc/{pid}").exists), "Legacy git must outlive app-server"
            assert not finished.exists()
    finally:
        await harness.stop()
        release.touch()
        if started.exists():
            await asyncio.to_thread(wait_for_file, finished)
    print(json.dumps({"mode": mode, "startup_git_spawned": started.exists(),
                      "fixture_git_finished": finished.exists(), "cli_exited": proc.poll() is not None}))
