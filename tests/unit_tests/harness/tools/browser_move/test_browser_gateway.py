#!/usr/bin/env python
# coding: utf-8
"""Tests for the Provider-neutral Browser tool gateway."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserBackend,
    BrowserExecutionFileRoots,
    BrowserExecutionIdentity,
    BrowserExecutionToolGateway,
    BrowserInstanceIdentity,
    BrowserProfileIdentity,
    BrowserTaskIdentity,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    browser_tool_allowlist_fingerprint,
)
from openjiuwen.harness_protocol import ToolInvocation


def _identity(tmp_path) -> BrowserExecutionIdentity:
    profile = BrowserProfileIdentity(
        profile_id="profile-main",
        owner_subject_id="subject-1",
        backend=BrowserBackend.MANAGED,
        policy_revision="policy-1",
    )
    instance = BrowserInstanceIdentity(
        instance_id="instance-1",
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        parent_session_id="parent-1",
        subagent_id="parent-1_sub_browser-1",
        workspace=str(tmp_path),
    )
    task = BrowserTaskIdentity(
        task_id="task-1",
        instance_id=instance.instance_id,
        instance_generation=instance.generation,
        child_turn_id="turn-1",
        request_id="request-1",
        capability_fingerprint=browser_tool_allowlist_fingerprint(
            ("browser_navigate", "browser_snapshot")
        ),
    )
    return BrowserExecutionIdentity(profile=profile, instance=instance, task=task)


class _FakeTool:
    def __init__(self, result=None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0

    async def invoke(self, arguments):
        self.calls.append(dict(arguments))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            self.active -= 1


class _FakeClient:
    def __init__(self, cards) -> None:
        self.cards = list(cards)
        self.list_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        return list(self.cards)


class _FakeRuntime:
    def __init__(self, identity, roots, tools) -> None:
        self.service = SimpleNamespace(
            execution_identity=identity,
            file_roots=roots,
            allowed_tool_names=("browser_navigate", "browser_snapshot"),
            mcp_cfg=SimpleNamespace(server_id="browser-instance-1"),
        )
        self.tools = dict(tools)
        self.ready_calls = 0
        self.release_calls = 0
        self.recorded: list[tuple[str, dict, object]] = []
        self.refs: list[tuple[str, ...]] = []
        self.batch_calls: list[dict] = []

    async def ensure_runtime_ready(self) -> None:
        self.ready_calls += 1

    async def _get_playwright_mcp_tool(self, name):
        return self.tools[name]

    @staticmethod
    def classify_tool_result(value):
        failed = isinstance(value, dict) and value.get("ok") is False
        return {"success": not failed, "error": "failed" if failed else ""}

    def validate_reference_values(self, values) -> None:
        values = tuple(values)
        self.refs.append(values)
        if "stale-ref" in values:
            raise ValueError("stale ref")

    def record_tool_reference_state(self, *, tool_name, tool_args, tool_result):
        self.recorded.append((tool_name, dict(tool_args), tool_result))

    @staticmethod
    def export_page_state():
        return {"generation_id": "g1", "url": "https://example.test/"}

    async def batch_interact(self, **kwargs):
        self.batch_calls.append(dict(kwargs))
        return {"ok": True, "status": "completed"}

    async def probe_interactives(self, **kwargs):
        return {"ok": True, "items": [], **kwargs}

    async def probe_cards(self, **kwargs):
        return {"ok": True, "cards": [], **kwargs}

    async def release_task_resources(self) -> None:
        self.release_calls += 1


def _roots(tmp_path) -> BrowserExecutionFileRoots:
    return BrowserExecutionFileRoots(
        workspace=str(tmp_path),
        uploads_root=str(tmp_path / "uploads"),
        outputs_root=str(tmp_path / "outputs"),
        audit_root=str(tmp_path / "audit"),
    )


def _cards():
    return [
        SimpleNamespace(
            name="browser_navigate",
            description="Navigate",
            input_params={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
        SimpleNamespace(
            name="browser_snapshot",
            description="Snapshot",
            input_params={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": [],
            },
        ),
        SimpleNamespace(
            name="browser_cookie_list",
            description="Not selected",
            input_params={"type": "object", "properties": {}},
        ),
    ]


def _gateway(tmp_path, monkeypatch, *, tools=None, cards=None, admit=None):
    identity = _identity(tmp_path)
    roots = _roots(tmp_path)
    runtime = _FakeRuntime(
        identity,
        roots,
        tools
        or {
            "browser_navigate": _FakeTool({"ok": True, "url": "https://example.test/"}),
            "browser_snapshot": _FakeTool({"ok": True, "snapshot": "- button ref=s1"}),
        },
    )
    client = _FakeClient(_cards() if cards is None else cards)
    monkeypatch.setattr(
        "openjiuwen.harness.tools.browser_move.playwright_runtime.browser_gateway.get_registered_client",
        lambda server_id: client if server_id == "browser-instance-1" else None,
    )
    return BrowserExecutionToolGateway(runtime, admit=admit), runtime, client


def test_gateway_requires_product_identity_and_file_roots(tmp_path) -> None:
    runtime = _FakeRuntime(_identity(tmp_path), _roots(tmp_path), {})
    runtime.service.execution_identity = None
    with pytest.raises(ValueError, match="identity-bound"):
        BrowserExecutionToolGateway(runtime)

    runtime.service.execution_identity = _identity(tmp_path)
    runtime.service.file_roots = None
    with pytest.raises(ValueError, match="file roots"):
        BrowserExecutionToolGateway(runtime)


def test_definitions_expose_only_allowlist_plus_deterministic_helpers(
    tmp_path,
    monkeypatch,
) -> None:
    gateway, runtime, client = _gateway(tmp_path, monkeypatch)

    definitions = asyncio.run(gateway.definitions())
    second = asyncio.run(gateway.definitions())

    assert [definition.name for definition in definitions] == [
        "browser_navigate",
        "browser_snapshot",
        "browser_probe_interactives",
        "browser_probe_cards",
        "browser_batch_interact",
    ]
    assert second == definitions
    assert "browser_cookie_list" not in {item.name for item in definitions}
    assert runtime.ready_calls == 1
    assert client.list_calls == 1


def test_missing_allowlisted_mcp_tool_fails_closed(tmp_path, monkeypatch) -> None:
    gateway, _, _ = _gateway(tmp_path, monkeypatch, cards=_cards()[:1])

    with pytest.raises(RuntimeError, match="browser_snapshot"):
        asyncio.run(gateway.definitions())


def test_unknown_and_denied_tools_never_reach_browser(tmp_path, monkeypatch) -> None:
    navigate = _FakeTool({"ok": True})

    async def deny(_identity, _invocation):
        return False

    gateway, _, _ = _gateway(
        tmp_path,
        monkeypatch,
        tools={"browser_navigate": navigate, "browser_snapshot": _FakeTool({})},
        admit=deny,
    )

    unknown = asyncio.run(
        gateway.invoke(ToolInvocation(call_id="1", name="browser_cookie_list", arguments={}))
    )
    denied = asyncio.run(
        gateway.invoke(
            ToolInvocation(
                call_id="2",
                name="browser_navigate",
                arguments={"url": "https://example.test"},
            )
        )
    )

    assert unknown.is_error is True
    assert denied.is_error is True
    assert navigate.calls == []


def test_admission_failure_is_redacted_and_never_reaches_browser(
    tmp_path,
    monkeypatch,
) -> None:
    navigate = _FakeTool({"ok": True})

    def fail_admission(_identity, _invocation):
        raise RuntimeError("Authorization: policy-secret")

    gateway, _, _ = _gateway(
        tmp_path,
        monkeypatch,
        tools={"browser_navigate": navigate, "browser_snapshot": _FakeTool({})},
        admit=fail_admission,
    )

    result = asyncio.run(
        gateway.invoke(
            ToolInvocation(
                call_id="1",
                name="browser_navigate",
                arguments={"url": "https://example.test"},
            )
        )
    )

    assert result.is_error is True
    assert "policy-secret" not in str(result.content)
    assert navigate.calls == []


def test_raw_tool_result_includes_bound_task_and_page_state(tmp_path, monkeypatch) -> None:
    gateway, runtime, _ = _gateway(tmp_path, monkeypatch)

    result = asyncio.run(
        gateway.invoke(
            ToolInvocation(
                call_id="1",
                name="browser_navigate",
                arguments={"url": "https://example.test"},
            )
        )
    )

    assert result.is_error is False
    assert result.content["task_id"] == "task-1"
    assert result.content["request_id"] == "request-1"
    assert result.content["page_state"]["generation_id"] == "g1"
    assert runtime.recorded[0][0] == "browser_navigate"


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("browser_navigate", {}),
        ("browser_snapshot", {"ref": "stale-ref"}),
        ("browser_snapshot", {"target_id": "t_g1_1"}),
    ],
)
def test_invalid_schema_reference_or_raw_target_id_is_rejected(
    tmp_path,
    monkeypatch,
    name,
    arguments,
) -> None:
    gateway, runtime, _ = _gateway(tmp_path, monkeypatch)

    result = asyncio.run(
        gateway.invoke(ToolInvocation(call_id="1", name=name, arguments=arguments))
    )

    assert result.is_error is True
    assert all(tool.calls == [] for tool in runtime.tools.values())


def test_helper_receives_identity_owned_session_and_request_ids(
    tmp_path,
    monkeypatch,
) -> None:
    gateway, runtime, _ = _gateway(tmp_path, monkeypatch)

    result = asyncio.run(
        gateway.invoke(
            ToolInvocation(
                call_id="1",
                name="browser_batch_interact",
                arguments={"steps": [{"op": "wait_for_stable"}], "generation_id": "g1"},
            )
        )
    )

    assert result.is_error is False
    assert runtime.batch_calls[0]["session_id"] == "task-1"
    assert runtime.batch_calls[0]["request_id"] == "request-1"


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "steps": [{"op": "wait_for_stable"}],
            "generation_id": "g1",
            "session_id": "another-task",
        },
        {
            "steps": [{"op": "wait_for_stable"}],
            "generation_id": "g1",
            "request_id": "another-request",
        },
    ],
)
def test_helper_rejects_mismatched_task_identity(
    tmp_path,
    monkeypatch,
    arguments,
) -> None:
    gateway, runtime, _ = _gateway(tmp_path, monkeypatch)

    result = asyncio.run(
        gateway.invoke(
            ToolInvocation(
                call_id="1",
                name="browser_batch_interact",
                arguments=arguments,
            )
        )
    )

    assert result.is_error is True
    assert runtime.batch_calls == []


def test_gateway_serializes_concurrent_browser_calls(tmp_path, monkeypatch) -> None:
    navigate = _FakeTool({"ok": True})
    gateway, _, _ = _gateway(
        tmp_path,
        monkeypatch,
        tools={"browser_navigate": navigate, "browser_snapshot": _FakeTool({})},
    )

    async def invoke_twice():
        return await asyncio.gather(
            gateway.invoke(
                ToolInvocation(
                    call_id="1",
                    name="browser_navigate",
                    arguments={"url": "https://example.test/one"},
                )
            ),
            gateway.invoke(
                ToolInvocation(
                    call_id="2",
                    name="browser_navigate",
                    arguments={"url": "https://example.test/two"},
                )
            ),
        )

    results = asyncio.run(invoke_twice())

    assert all(result.is_error is False for result in results)
    assert navigate.max_active == 1


def test_transport_errors_are_redacted_and_close_is_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    secret_error = RuntimeError("Authorization: bearer-secret")
    gateway, runtime, _ = _gateway(
        tmp_path,
        monkeypatch,
        tools={
            "browser_navigate": _FakeTool(error=secret_error),
            "browser_snapshot": _FakeTool({}),
        },
    )

    result = asyncio.run(
        gateway.invoke(
            ToolInvocation(
                call_id="1",
                name="browser_navigate",
                arguments={"url": "https://example.test"},
            )
        )
    )
    asyncio.run(gateway.close())
    asyncio.run(gateway.close())
    after_close = asyncio.run(
        gateway.invoke(
            ToolInvocation(
                call_id="2",
                name="browser_navigate",
                arguments={"url": "https://example.test"},
            )
        )
    )

    assert result.is_error is True
    assert "bearer-secret" not in str(result.content)
    assert runtime.release_calls == 1
    assert gateway.closed is True
    assert after_close.is_error is True
