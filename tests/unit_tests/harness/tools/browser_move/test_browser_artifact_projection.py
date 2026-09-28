#!/usr/bin/env python
# coding: utf-8
"""Tests for controlled Browser output to AgentResult Artifact projection."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserArtifactKind,
    BrowserArtifactProjectionError,
    BrowserBackend,
    BrowserExecutionFileRoots,
    BrowserExecutionIdentity,
    BrowserInstanceIdentity,
    BrowserProfileIdentity,
    BrowserTaskIdentity,
    project_browser_output_artifact,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    browser_tool_allowlist_fingerprint,
)


def _identity_and_roots(tmp_path):
    profile = BrowserProfileIdentity(
        profile_id="profile-main",
        owner_subject_id="subject-1",
        owner_tenant_id="tenant-1",
        backend=BrowserBackend.MANAGED,
        policy_revision="browser-policy-v1",
        generation=2,
    )
    instance = BrowserInstanceIdentity(
        instance_id="instance-1",
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        parent_session_id="session-1",
        subagent_id="session-1_sub_browser-1",
        workspace=str(tmp_path),
        generation=3,
    )
    task = BrowserTaskIdentity(
        task_id="task-1",
        instance_id=instance.instance_id,
        instance_generation=instance.generation,
        child_turn_id="turn-1",
        request_id="request-1",
        capability_fingerprint=browser_tool_allowlist_fingerprint(
            ("browser_navigate", "browser_take_screenshot")
        ),
    )
    roots = BrowserExecutionFileRoots(
        workspace=str(tmp_path),
        uploads_root=str(tmp_path / "uploads"),
        outputs_root=str(tmp_path / "outputs"),
        audit_root=str(tmp_path / "audit"),
    )
    return BrowserExecutionIdentity(profile=profile, instance=instance, task=task), roots


def _project(file_path, *, identity, roots, **overrides):
    values = {
        "kind": BrowserArtifactKind.DOWNLOAD,
        "source_url": "https://user:password@Example.COM/report?id=secret#section",
        "tool_name": "browser_download",
        "permission_decision_id": "permission-1",
    }
    values.update(overrides)
    return project_browser_output_artifact(
        file_path,
        execution_identity=identity,
        file_roots=roots,
        **values,
    )


def test_projects_controlled_file_without_copying_or_embedding_content(tmp_path) -> None:
    identity, roots = _identity_and_roots(tmp_path)
    output = tmp_path / "outputs" / "artifacts" / "report.pdf"
    output.parent.mkdir(parents=True)
    content = b"private report bytes"
    output.write_bytes(content)

    artifact = _project(output, identity=identity, roots=roots)

    expected_hash = hashlib.sha256(content).hexdigest()
    assert artifact.artifactId == (
        "browser-output-"
        + hashlib.sha256(
            f"task-1\0outputs/artifacts/report.pdf\0{expected_hash}".encode()
        ).hexdigest()
    )
    assert artifact.name == "report.pdf"
    assert artifact.parts[0].url == "outputs/artifacts/report.pdf"
    assert artifact.parts[0].filename == "report.pdf"
    assert artifact.parts[0].media_type == "application/pdf"
    assert artifact.parts[0].raw is None
    assert artifact.parts[0].data is None
    assert artifact.metadata == {
        "kind": "download",
        "source_url": "https://example.com/report",
        "producer_tool": "browser_download",
        "task_id": "task-1",
        "child_turn_id": "turn-1",
        "request_id": "request-1",
        "permission_decision_id": "permission-1",
        "workspace_relative_path": "outputs/artifacts/report.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(content),
        "sha256": expected_hash,
    }
    serialized = json.dumps(artifact.model_dump(), default=str)
    assert str(tmp_path) not in serialized
    assert "private report bytes" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
    assert output.read_bytes() == content


def test_relative_output_path_is_resolved_from_outputs_root(tmp_path) -> None:
    identity, roots = _identity_and_roots(tmp_path)
    output = tmp_path / "outputs" / "screenshots" / "page.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"image")

    artifact = _project(
        "screenshots/page.png",
        identity=identity,
        roots=roots,
        kind="screenshot",
        source_url="about:blank",
    )

    assert artifact.parts[0].url == "outputs/screenshots/page.png"
    assert artifact.metadata["kind"] == "screenshot"
    assert artifact.metadata["source_url"] == "about:blank"


@pytest.mark.parametrize("candidate", ["outside.txt", "outputs"])
def test_rejects_outside_file_and_directory(tmp_path, candidate) -> None:
    identity, roots = _identity_and_roots(tmp_path)
    (tmp_path / "outputs").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    path = outside if candidate == "outside.txt" else tmp_path / "outputs"

    with pytest.raises(BrowserArtifactProjectionError):
        _project(path, identity=identity, roots=roots)


def test_rejects_missing_file(tmp_path) -> None:
    identity, roots = _identity_and_roots(tmp_path)

    with pytest.raises(BrowserArtifactProjectionError, match="existing regular file"):
        _project("missing.txt", identity=identity, roots=roots)


def test_rejects_symbolic_link_even_when_target_is_inside_outputs(tmp_path) -> None:
    identity, roots = _identity_and_roots(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    target = output_root / "target.txt"
    target.write_text("safe", encoding="utf-8")
    link = output_root / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(BrowserArtifactProjectionError, match="symbolic links"):
        _project(link, identity=identity, roots=roots)


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "audit"},
        {"source_url": "file:///tmp/secret"},
        {"tool_name": ""},
        {"permission_decision_id": ""},
    ],
)
def test_rejects_untrusted_projection_metadata(tmp_path, overrides) -> None:
    identity, roots = _identity_and_roots(tmp_path)
    output = tmp_path / "outputs" / "artifact.bin"
    output.parent.mkdir()
    output.write_bytes(b"value")

    with pytest.raises(BrowserArtifactProjectionError):
        _project(output, identity=identity, roots=roots, **overrides)


def test_rejects_roots_from_a_different_execution_workspace(tmp_path) -> None:
    identity, _ = _identity_and_roots(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    roots = BrowserExecutionFileRoots(
        workspace=str(other),
        uploads_root=str(other / "uploads"),
        outputs_root=str(other / "outputs"),
        audit_root=str(other / "audit"),
    )
    output = other / "outputs" / "artifact.bin"
    output.parent.mkdir()
    output.write_bytes(b"value")

    with pytest.raises(BrowserArtifactProjectionError, match="execution workspace"):
        _project(output, identity=identity, roots=roots)
