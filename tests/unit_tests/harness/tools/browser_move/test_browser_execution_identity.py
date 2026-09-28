#!/usr/bin/env python
# coding: utf-8
"""Tests for non-sensitive Browser Profile/Instance/Task identities."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import FrozenInstanceError

import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserBackend,
    BrowserExecutionFileRoots,
    BrowserExecutionIdentity,
    BrowserInstanceIdentity,
    BrowserProfileIdentity,
    BrowserTaskIdentity,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    browser_tool_allowlist_fingerprint,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserInstanceConfig,
    BrowserRunGuardrails,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.service import (
    BrowserService,
)

_DEFAULT_ROOTS = object()


def _identity(tmp_path) -> BrowserExecutionIdentity:
    profile = BrowserProfileIdentity(
        profile_id="profile-main",
        owner_subject_id="subject-1",
        owner_tenant_id="tenant-1",
        backend=BrowserBackend.MANAGED,
        policy_revision="browser-policy-v1",
        generation=3,
    )
    instance = BrowserInstanceIdentity(
        instance_id="instance-1",
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        parent_session_id="session-1",
        subagent_id="session-1_sub_browser-1",
        workspace=str(tmp_path),
        generation=4,
    )
    task = BrowserTaskIdentity(
        task_id="task-1",
        instance_id=instance.instance_id,
        instance_generation=instance.generation,
        child_turn_id="turn-1",
        request_id="request-1",
        capability_fingerprint=browser_tool_allowlist_fingerprint(
            ("browser_navigate", "browser_click")
        ),
    )
    return BrowserExecutionIdentity(profile=profile, instance=instance, task=task)


def _service(
    tmp_path,
    identity: BrowserExecutionIdentity,
    *,
    driver_mode: str = "managed",
    profile_name: str = "profile-main",
    key: str = "instance-1",
    runtime_cwd: str | None = None,
    allowed_tool_names: tuple[str, ...] = ("browser_navigate", "browser_click"),
    file_roots=_DEFAULT_ROOTS,
) -> BrowserService:
    mcp_cfg = McpServerConfig(
        server_id="playwright-bound-identity",
        server_name="playwright-bound-identity",
        server_path="stdio://playwright",
        client_type="stdio",
        params={"cwd": runtime_cwd or str(tmp_path)},
    )
    if file_roots is _DEFAULT_ROOTS:
        file_roots = BrowserExecutionFileRoots(
            workspace=str(tmp_path),
            uploads_root=str(tmp_path / "uploads"),
            outputs_root=str(tmp_path / "outputs"),
            audit_root=str(tmp_path / "audit"),
        )
    return BrowserService(
        provider="openai",
        api_key="test-key",
        api_base="https://example.invalid/v1",
        model_name="test-model",
        mcp_cfg=mcp_cfg,
        guardrails=BrowserRunGuardrails(),
        instance=BrowserInstanceConfig(
            key=key,
            profile_name=profile_name,
            driver_mode=driver_mode,
        ),
        allowed_tool_names=allowed_tool_names,
        execution_identity=identity,
        file_roots=file_roots,
    )


def test_browser_execution_identity_round_trips_strict_json(tmp_path) -> None:
    identity = _identity(tmp_path)

    encoded = json.loads(json.dumps(identity.to_dict()))
    restored = BrowserExecutionIdentity.from_dict(encoded)

    assert restored == identity
    assert restored.profile.backend is BrowserBackend.MANAGED
    assert set(encoded) == {"profile", "instance", "task"}
    assert "user_data_dir" not in json.dumps(encoded)
    assert "cdp_url" not in json.dumps(encoded)


def test_browser_execution_identity_is_immutable(tmp_path) -> None:
    identity = _identity(tmp_path)

    with pytest.raises(FrozenInstanceError):
        identity.profile.profile_id = "other"  # type: ignore[misc]


def test_browser_service_binds_execution_identity_before_start(tmp_path) -> None:
    identity = _identity(tmp_path)

    service = _service(tmp_path, identity)

    assert service.execution_identity is identity
    assert service.lifecycle_identity.profile_generation == 3
    assert service.lifecycle_identity.instance_generation == 4
    assert service.file_roots is not None
    assert service.artifacts_subdir == "outputs/artifacts"
    assert not (tmp_path / "outputs").exists()


def test_browser_service_projects_output_through_bound_identity(tmp_path) -> None:
    identity = _identity(tmp_path)
    service = _service(tmp_path, identity)
    output = tmp_path / "outputs" / "artifacts" / "result.csv"
    output.parent.mkdir(parents=True)
    output.write_text("name,value\nanswer,42\n", encoding="utf-8")

    artifact = service.project_output_artifact(
        output,
        kind="export",
        source_url="https://example.test/results?token=hidden",
        tool_name="browser_export",
        permission_decision_id="decision-1",
    )

    assert artifact.parts[0].url == "outputs/artifacts/result.csv"
    assert artifact.metadata["task_id"] == identity.task.task_id
    assert artifact.metadata["source_url"] == "https://example.test/results"


def test_identity_bound_screenshot_uses_controlled_outputs_root(tmp_path) -> None:
    service = _service(tmp_path, _identity(tmp_path))
    source = tmp_path / "outside-output.png"
    source.write_bytes(b"png-content")

    normalized = service._normalize_screenshot_value(str(source))

    assert normalized == "data:image/png;base64,cG5nLWNvbnRlbnQ="
    controlled = tmp_path / "outputs" / "screenshots" / source.name
    assert controlled.read_bytes() == b"png-content"


def test_legacy_browser_service_cannot_claim_product_artifact_projection(tmp_path) -> None:
    mcp_cfg = McpServerConfig(
        server_id="playwright-legacy",
        server_name="playwright-legacy",
        server_path="stdio://playwright",
        client_type="stdio",
        params={"cwd": str(tmp_path)},
    )
    service = BrowserService(
        provider="openai",
        api_key="test-key",
        api_base="https://example.invalid/v1",
        model_name="test-model",
        mcp_cfg=mcp_cfg,
        guardrails=BrowserRunGuardrails(),
    )

    with pytest.raises(ValueError, match="requires execution identity"):
        service.project_output_artifact(
            tmp_path / "artifact.txt",
            kind="export",
            source_url="about:blank",
            tool_name="browser_export",
            permission_decision_id="decision-1",
        )


def test_browser_service_rejects_request_identity_drift_before_start(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, _identity(tmp_path))
    started = False

    async def fail_if_started() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(service, "ensure_started", fail_if_started)

    with pytest.raises(ValueError, match="request_id"):
        asyncio.run(
            service.run_task(
                "visit the page",
                request_id="different-request",
            )
        )

    assert started is False


def test_browser_service_uses_bound_request_id_when_caller_omits_it(
    tmp_path,
    monkeypatch,
) -> None:
    identity = _identity(tmp_path)
    service = _service(tmp_path, identity)
    observed_request_ids: list[str] = []

    async def no_start() -> None:
        return None

    async def run_once(task: str, session_id: str, request_id: str):
        observed_request_ids.append(request_id)
        return {
            "ok": True,
            "final": "done",
            "page": {"url": "about:blank", "title": ""},
            "screenshot": None,
            "error": None,
        }

    monkeypatch.setattr(service, "ensure_started", no_start)
    monkeypatch.setattr(service, "run_task_once", run_once)

    result = asyncio.run(service.run_task("visit the page"))

    assert result["ok"] is True
    assert result["request_id"] == identity.task.request_id
    assert observed_request_ids == [identity.task.request_id]


def test_browser_execution_file_roots_round_trip(tmp_path) -> None:
    roots = BrowserExecutionFileRoots(
        workspace=str(tmp_path),
        uploads_root=str(tmp_path / "uploads"),
        outputs_root=str(tmp_path / "outputs"),
        audit_root=str(tmp_path / "audit"),
    )

    assert BrowserExecutionFileRoots.from_dict(roots.to_dict()) == roots


@pytest.mark.parametrize(
    "values",
    [
        {"uploads_root": "relative/uploads"},
        {"outputs_root": "/outside-workspace"},
        {"audit_root": "outputs/audit"},
    ],
)
def test_browser_execution_file_roots_reject_invalid_scope(tmp_path, values) -> None:
    roots = {
        "workspace": str(tmp_path),
        "uploads_root": str(tmp_path / "uploads"),
        "outputs_root": str(tmp_path / "outputs"),
        "audit_root": str(tmp_path / "audit"),
    }
    roots.update(values)
    if values.get("audit_root") == "outputs/audit":
        roots["audit_root"] = str(tmp_path / "outputs" / "audit")

    with pytest.raises(ValueError):
        BrowserExecutionFileRoots(**roots)


def test_browser_service_requires_file_roots_for_product_identity(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires explicit file roots"):
        _service(tmp_path, _identity(tmp_path), file_roots=None)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"driver_mode": "remote"}, "backend"),
        ({"profile_name": "other-profile"}, "profile"),
        ({"key": "other-instance"}, "instance"),
        ({"runtime_cwd": "/different-workspace"}, "workspace"),
        ({"allowed_tool_names": ("browser_navigate",)}, "fingerprint"),
    ],
)
def test_browser_service_rejects_execution_identity_runtime_drift(
    tmp_path,
    override,
    message,
) -> None:
    identity = _identity(tmp_path)

    with pytest.raises(ValueError, match=message):
        _service(tmp_path, identity, **override)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("profile_id", "profiles/main"),
        ("profile_id", "profiles\\main"),
        ("profile_id", "profile\nmain"),
        ("owner_subject_id", ""),
        ("policy_revision", " "),
    ],
)
def test_profile_identity_rejects_invalid_identifiers(field_name, value) -> None:
    values = {
        "profile_id": "profile-main",
        "owner_subject_id": "subject-1",
        "backend": BrowserBackend.MANAGED,
        "policy_revision": "policy-v1",
    }
    values[field_name] = value

    with pytest.raises(ValueError):
        BrowserProfileIdentity(**values)


def test_profile_identity_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown browser backend"):
        BrowserProfileIdentity(
            profile_id="profile-main",
            owner_subject_id="subject-1",
            backend="ambient",  # type: ignore[arg-type]
            policy_revision="policy-v1",
        )


@pytest.mark.parametrize(
    "fingerprint",
    ["", "sha256:short", "SHA256:" + "a" * 64, "sha256:" + "A" * 64],
)
def test_task_identity_rejects_invalid_capability_fingerprint(
    tmp_path,
    fingerprint,
) -> None:
    identity = _identity(tmp_path)

    with pytest.raises(ValueError, match="capability_fingerprint"):
        dataclasses.replace(identity.task, capability_fingerprint=fingerprint)


@pytest.mark.parametrize("generation", [0, -1, True, "1"])
def test_profile_identity_rejects_invalid_generation(generation) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        BrowserProfileIdentity(
            profile_id="profile-main",
            owner_subject_id="subject-1",
            backend=BrowserBackend.MANAGED,
            policy_revision="policy-v1",
            generation=generation,
        )


def test_instance_identity_requires_absolute_workspace(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        BrowserInstanceIdentity(
            instance_id="instance-1",
            profile_id="profile-main",
            profile_generation=1,
            parent_session_id="session-1",
            subagent_id="subagent-1",
            workspace="relative/workspace",
        )

    identity = BrowserInstanceIdentity(
        instance_id="instance-1",
        profile_id="profile-main",
        profile_generation=1,
        parent_session_id="session-1",
        subagent_id="subagent-1",
        workspace=str(tmp_path / "folder" / ".." / "workspace"),
    )
    assert identity.workspace == str(tmp_path / "workspace")


@pytest.mark.parametrize(
    "mutation",
    [
        "profile_id",
        "profile_generation",
        "instance_id",
        "instance_generation",
    ],
)
def test_execution_identity_rejects_cross_layer_drift(tmp_path, mutation) -> None:
    identity = _identity(tmp_path)
    profile = identity.profile
    instance = identity.instance
    task = identity.task
    if mutation == "profile_id":
        instance = dataclasses.replace(instance, profile_id="other-profile")
    elif mutation == "profile_generation":
        instance = dataclasses.replace(instance, profile_generation=profile.generation + 1)
    elif mutation == "instance_id":
        task = dataclasses.replace(task, instance_id="other-instance")
    else:
        task = dataclasses.replace(task, instance_generation=instance.generation + 1)

    with pytest.raises(ValueError):
        BrowserExecutionIdentity(profile=profile, instance=instance, task=task)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("owner_subject_id", "subject-2"),
        ("owner_tenant_id", "tenant-2"),
        ("parent_session_id", "session-2"),
        ("subagent_id", "session-1_sub_browser-2"),
        ("workspace", "/different-workspace"),
    ],
)
def test_validate_scope_rejects_cross_scope_reuse(tmp_path, field_name, value) -> None:
    identity = _identity(tmp_path)
    scope = {
        "owner_subject_id": "subject-1",
        "owner_tenant_id": "tenant-1",
        "parent_session_id": "session-1",
        "subagent_id": "session-1_sub_browser-1",
        "workspace": str(tmp_path),
    }
    scope[field_name] = value

    with pytest.raises(ValueError, match=field_name):
        identity.validate_scope(**scope)


def test_validate_scope_accepts_exact_normalized_scope(tmp_path) -> None:
    identity = _identity(tmp_path)

    identity.validate_scope(
        owner_subject_id=" subject-1 ",
        owner_tenant_id="tenant-1",
        parent_session_id="session-1",
        subagent_id="session-1_sub_browser-1",
        workspace=str(tmp_path / "folder" / ".."),
    )


@pytest.mark.parametrize(
    "type_name",
    ["profile", "instance", "task", "file_roots", "execution"],
)
def test_from_dict_rejects_unknown_and_missing_fields(tmp_path, type_name) -> None:
    identity = _identity(tmp_path)
    file_roots = BrowserExecutionFileRoots(
        workspace=str(tmp_path),
        uploads_root=str(tmp_path / "uploads"),
        outputs_root=str(tmp_path / "outputs"),
        audit_root=str(tmp_path / "audit"),
    )
    values = {
        "profile": (BrowserProfileIdentity, identity.profile.to_dict()),
        "instance": (BrowserInstanceIdentity, identity.instance.to_dict()),
        "task": (BrowserTaskIdentity, identity.task.to_dict()),
        "file_roots": (BrowserExecutionFileRoots, file_roots.to_dict()),
        "execution": (BrowserExecutionIdentity, identity.to_dict()),
    }
    identity_type, payload = values[type_name]

    with pytest.raises(ValueError, match="unknown fields"):
        identity_type.from_dict({**payload, "unexpected": "value"})
    missing = dict(payload)
    missing.pop(next(iter(missing)))
    with pytest.raises(ValueError, match="missing fields"):
        identity_type.from_dict(missing)
