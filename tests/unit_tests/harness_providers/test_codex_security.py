# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Codex permission negotiation and per-client process boundary regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.harness_protocol import HarnessProtocolError, HostCapability, ResumePolicy
from openjiuwen.harness_providers.base import ProviderStartupError
from openjiuwen.harness_providers.codex import CodexHarness, CodexHarnessConfig
from openjiuwen.harness_providers.codex.sdk_compat import connect_with_host_approvals, isolate_process_environment

from tests.unit_tests.harness_providers.test_codex import _context, _install_fake_sdk


@pytest.mark.parametrize("field", ["inherit_process_env", "bypass_approvals_and_sandbox"])
@pytest.mark.parametrize("value", ["false", 0, None])
def test_permission_switches_reject_non_booleans(field, value):
    with pytest.raises(TypeError, match="boolean"):
        CodexHarnessConfig(**{field: value})


def _client(effective=None, config=None):
    if effective is None:
        effective = {
            "approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": {"type": "readOnly"},
        }
    async def request(method, params, *, response_model):
        if method == "config/read":
            return response_model.model_validate({"config": config or {}})
        return response_model.model_validate({"thread": {"id": "thread-1"}, "model": "test-model", **effective})

    client = SimpleNamespace(
        _ensure_initialized=AsyncMock(), _client=SimpleNamespace(request=AsyncMock(side_effect=request)),
        thread_start=AsyncMock(), thread_resume=AsyncMock(),
    )
    sdk = SimpleNamespace(
        AsyncThread=lambda owner, thread_id: SimpleNamespace(id=thread_id),
        generated=SimpleNamespace(v2_all=SimpleNamespace(ThreadStartResponse=object, ThreadResumeResponse=object)),
    )
    return client, sdk


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [None, "thread-1"])
@pytest.mark.parametrize("raw_events", [False, True])
async def test_host_policy_overrides_ambient_config_for_start_and_resume(resume, raw_events):
    client, sdk = _client()
    # Conflicting ambient config cannot override the explicit request fields.
    options = {
        "config": {"approval_policy": "never", "approvals_reviewer": "auto_review"},
        "cwd": "/work", "model": "model-a", "model_provider": "provider-a", "developer_instructions": "rules",
    }
    thread, model, fingerprint = await connect_with_host_approvals(
        client=client, sdk=sdk, options=options, resume_thread_id=resume, raw_events=raw_events,
    )
    method, request = client._client.request.call_args.args
    assert method == ("thread/resume" if resume else "thread/start")
    assert (request["approvalPolicy"], request["approvalsReviewer"], request["sandbox"]) == (
        "untrusted", "user", "read-only",
    )
    assert request["modelProvider"] == "provider-a"
    assert request["developerInstructions"] == "rules"
    assert ("experimentalRawEvents" in request) is (resume is None)
    if resume is None:
        assert request["experimentalRawEvents"] is raw_events
    assert thread.id == "thread-1" and model == "test-model"
    assert len(fingerprint) == 64
    client.thread_start.assert_not_called()
    client.thread_resume.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("effective", [
    {},
    {"approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": {"type": "readOnly"}},
    {"approvalPolicy": "untrusted", "approvalsReviewer": "auto_review", "sandbox": {"type": "readOnly"}},
    {"approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": {"type": "dangerFullAccess"}},
])
async def test_unconfirmed_policy_never_falls_back_to_sdk_defaults(effective):
    client, sdk = _client(effective)
    with pytest.raises(HarnessProtocolError, match="did not confirm"):
        await connect_with_host_approvals(client=client, sdk=sdk, options={}, resume_thread_id=None, raw_events=True)
    client.thread_start.assert_not_called()


@pytest.mark.asyncio
async def test_resume_identity_and_rpc_failures_do_not_recreate_thread():
    client, sdk = _client()
    with pytest.raises(HarnessProtocolError, match="different thread"):
        await connect_with_host_approvals(
            client=client, sdk=sdk, options={}, resume_thread_id="another-thread", raw_events=True,
        )
    client._client.request.side_effect = RuntimeError("connection failed")
    with pytest.raises(RuntimeError, match="connection failed"):
        await connect_with_host_approvals(
            client=client, sdk=sdk, options={}, resume_thread_id="thread-1", raw_events=True,
        )
    client.thread_start.assert_not_called()
    client.thread_resume.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [{"sandbox_mode": "danger-full-access"}, {"sandbox_mode": "unknown"}])
async def test_host_approval_rejects_unrestricted_or_unknown_sandbox_before_start(config):
    client, sdk = _client()
    with pytest.raises(HarnessProtocolError, match="restricted sandbox"):
        await connect_with_host_approvals(
            client=client, sdk=sdk, options={"config": config}, resume_thread_id=None, raw_events=False,
        )
    client._ensure_initialized.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", [False, True])
async def test_host_approval_requires_handler_and_rejects_explicit_bypass(monkeypatch, bypass):
    _, state = _install_fake_sdk(monkeypatch)
    harness = CodexHarness(CodexHarnessConfig(bypass_approvals_and_sandbox=bypass))
    context = _context(host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}))
    with pytest.raises(HarnessProtocolError):
        await harness.start(context)
    assert not state.clients


@pytest.mark.asyncio
async def test_policy_negotiation_failure_closes_client_before_any_turn(monkeypatch):
    sdk, state = _install_fake_sdk(monkeypatch)
    state.security_config = {"default_permissions": "missing"}
    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    with pytest.raises(ProviderStartupError):
        await harness.start(_context(
            host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}), interactions=object(),
        ))
    assert state.clients[0].closed
    assert not state.thread_calls


def test_isolated_launch_uses_only_selected_environment_and_is_instance_local(monkeypatch):
    monkeypatch.setenv("A0_UNAUTHORIZED_SECRET", "must-not-be-inherited")
    sdk = SimpleNamespace(client=SimpleNamespace(
        _resolve_codex_bin=lambda config: "/bundled/codex",
        _installed_codex_path_dirs=lambda: (),
        _prepend_path_dirs=lambda env, paths: None,
    ))
    transport = SimpleNamespace(
        config=SimpleNamespace(
            launch_args_override=None, codex_bin=None, config_overrides=('model="test"',),
            env={"SELECTED": "allowed"}, cwd="/work",
        ),
        _proc=None, start=Mock(), _start_stderr_drain_thread=Mock(), _start_reader_thread=Mock(),
    )
    original_start = transport.start
    other_transport = SimpleNamespace(start=original_start)
    popen = Mock()
    monkeypatch.setattr("openjiuwen.harness_providers.codex.sdk_compat.subprocess.Popen", popen)
    isolate_process_environment(SimpleNamespace(_client=SimpleNamespace(_sync=transport)), sdk)
    transport.start()
    transport.start()  # Already running; no duplicate process or readers.
    assert popen.call_count == 1
    assert popen.call_args.kwargs["env"] == {"SELECTED": "allowed", "PATH": ""}
    assert popen.call_args.args[0] == ["/bundled/codex", "--config", 'model="test"', "app-server", "--listen", "stdio://"]
    assert other_transport.start is original_start
    original_start.assert_not_called()
    transport._start_reader_thread.assert_called_once()


def test_unknown_transport_cannot_silently_inherit_environment():
    with pytest.raises(HarnessProtocolError, match="isolated process environment"):
        isolate_process_environment(
            SimpleNamespace(_client=SimpleNamespace(_sync=SimpleNamespace())), SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_approval_timeout_cancels_host_wait_and_removes_pending_request(monkeypatch):
    import asyncio

    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr("openjiuwen.harness_providers.codex.harness._APPROVAL_WAIT_TIMEOUT_S", 0.01)
    cancelled = asyncio.Event()

    class Handler:
        async def handle(self, request):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def cancel(self, request_id, *, reason=None):
            pass

    harness = CodexHarness(CodexHarnessConfig(inherit_process_env=False))
    await harness.start(_context(
        host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}), interactions=Handler(),
    ))
    try:
        decision = await asyncio.wait_for(asyncio.to_thread(
            harness._approval_handler, "item/commandExecution/requestApproval", {"itemId": "a0-timeout"},
        ), 2)
        assert decision == {"decision": "decline"}
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not harness._pending_interactions
    finally:
        await harness.stop()


@pytest.mark.parametrize("params", [
    {},
    {"mode": "url", "serverName": "test", "_meta": {"codex_approval_kind": "mcp_tool_call"}},
    {"mode": "form", "serverName": "test", "_meta": {"codex_approval_kind": "other"}},
    {"mode": "form", "serverName": "test", "_meta": None},
    {"mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call"}},
])
def test_unrecognized_mcp_elicitations_are_declined_without_host_dispatch(params):
    harness = CodexHarness()
    assert harness._approval_handler("mcpServer/elicitation/request", params) == {"action": "decline"}


_PROFILE_CONFIG = {
    "default_permissions": "test",
    "permissions": {"test": {"filesystem": {":minimal": "read", "/work": "read"}, "network": {"enabled": False}}},
}
_PROFILE_EFFECTIVE = {
    "approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": {"type": "readOnly"},
    "activePermissionProfile": {"id": "test"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [None, "thread-1"])
async def test_named_profile_preserved_without_legacy_override(resume):
    client, sdk = _client(_PROFILE_EFFECTIVE, _PROFILE_CONFIG)
    _, _, fingerprint = await connect_with_host_approvals(
        client=client, sdk=sdk, options={"cwd": "/work"}, resume_thread_id=resume, raw_events=False,
    )
    params = client._client.request.call_args.args[1]
    assert params["permissionProfile"] == "test" and "sandbox" not in params
    assert len(fingerprint) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", [None, {}, {"id": "other"}])
async def test_named_profile_requires_exact_effective_identity(actual):
    client, sdk = _client({**_PROFILE_EFFECTIVE, "activePermissionProfile": actual}, _PROFILE_CONFIG)
    with pytest.raises(HarnessProtocolError, match="did not confirm"):
        await connect_with_host_approvals(
            client=client, sdk=sdk, options={}, resume_thread_id=None, raw_events=False,
        )
    client.thread_start.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay", [
    {"sandbox_mode": "read-only"}, {"sandbox_workspace_write": {}}, {"default_permissions": None},
    {"default_permissions": "missing"}, {"permissions.test.filesystem": {}},
])
async def test_conflicting_or_missing_profile_fails_before_thread(overlay):
    client, sdk = _client(_PROFILE_EFFECTIVE, _PROFILE_CONFIG)
    with pytest.raises(HarnessProtocolError):
        await connect_with_host_approvals(
            client=client, sdk=sdk, options={"config": overlay}, resume_thread_id=None, raw_events=False,
        )
    assert all(call.args[0] == "config/read" for call in client._client.request.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["roots", "network", "cwd", "parent", "missing", "legacy"])
async def test_changed_security_configuration_fails_before_resume(change):
    from copy import deepcopy

    config = deepcopy(_PROFILE_CONFIG)
    client, sdk = _client(_PROFILE_EFFECTIVE, config)
    options = {"cwd": "/work"}
    _, _, fingerprint = await connect_with_host_approvals(
        client=client, sdk=sdk, options=options, resume_thread_id=None, raw_events=False,
    )
    if change == "roots":
        config["permissions"]["test"]["filesystem"]["/outside"] = "read"
    elif change == "network":
        config["permissions"]["test"]["network"]["enabled"] = True
    elif change == "cwd":
        options["cwd"] = "/other"
    elif change == "parent":
        config["permissions"]["test"]["extends"] = ":workspace"
    elif change == "missing":
        config["permissions"].clear()
    else:
        config["sandbox_mode"] = "read-only"
    client._client.request.reset_mock()
    with pytest.raises(HarnessProtocolError):
        await connect_with_host_approvals(
            client=client, sdk=sdk, options=options, resume_thread_id="thread-1", raw_events=False,
            expected_fingerprint=fingerprint,
        )
    assert [call.args[0] for call in client._client.request.call_args_list] == ["config/read"]


@pytest.mark.asyncio
async def test_permission_bound_checkpoint_cannot_drop_host_approval_capability(monkeypatch):
    _, state = _install_fake_sdk(monkeypatch)
    harness = CodexHarness(CodexHarnessConfig())
    await harness.start(_context(
        host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}), interactions=object(),
    ))
    checkpoint = await harness.export_checkpoint()
    assert len(checkpoint.data["permission_fingerprint"]) == 64
    await harness.stop()
    resumed = CodexHarness(CodexHarnessConfig())
    with pytest.raises(HarnessProtocolError, match="requires host tool approvals"):
        await resumed.start(_context(checkpoint=checkpoint, resume_policy=ResumePolicy.REQUIRE_RESUME))
    assert len(state.clients) == 1
    await resumed.stop()
