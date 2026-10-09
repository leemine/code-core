# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reject malformed prompt manifests within the original author retry loop."""

import json
from pathlib import Path

import pytest
import yaml

from openjiuwen.rsi.harness_rsi.member_optimizer import action_executor
from openjiuwen.rsi.harness_rsi.member_optimizer.schema import MemberOptimizationAction
from openjiuwen.rsi.harness_rsi.member_optimizer.verification import (
    _check_prompt_sections_manifest,
    _repair_failed_file_context,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "modify"])
async def test_invalid_manifest_retries_without_erasing_other_sections(monkeypatch, tmp_path: Path, operation):
    target = "prompt_sections/files/readback.md"
    manifest = "prompt_sections/sections.yaml"
    (tmp_path / target).parent.mkdir(parents=True)
    (tmp_path / target).write_text("Original instruction")
    other = {"name": "other", "content": {"en": "Preserve other instruction"}, "priority": 77}
    good = {"name": "readback", "file": target, "priority": 30}
    (tmp_path / manifest).write_text(yaml.safe_dump({"sections": [other, good]}))
    action = MemberOptimizationAction(
        action_id="repair_manifest",
        role="solver",
        action_group="prompt",
        operation=operation,
        action_type="prompt_improvement",
        target_path=target,
        description="Improve readback",
        declared_write_paths=[target, manifest],
        constraints={"section_name": "readback", "priority": 30},
    )
    calls = []

    class Agent:
        async def invoke(self, inputs, session=None):
            calls.append(inputs["query"])
            if len(calls) == 2:
                assert "prompt_section_ref" in inputs["query"]
                assert "phase" in inputs["query"]
            entry = {"id": "readback", "file": "readback.md", "phase": "task_start"} if len(calls) == 1 else good
            return {
                "text": json.dumps(
                    {
                        "status": "succeeded",
                        "file_writes": [
                            {
                                "path": target,
                                "content": "Read the actual output and compare it with the task contract.",
                            },
                            {"path": manifest, "content": yaml.safe_dump({"sections": [other, entry]})},
                        ],
                    }
                )
            }

    monkeypatch.setattr(action_executor, "create_action_execution_agent", lambda **kwargs: Agent())
    result = await action_executor.MemberActionExecutorAgent("unused").execute_action(
        tmp_path,
        action,
        "synthetic",
        [],
        [],
    )
    assert result["status"] == "succeeded", result
    assert len(calls) == 2
    assert yaml.safe_load((tmp_path / manifest).read_text())["sections"] == [other, good]
    assert all(c.status == "passed" for c in _check_prompt_sections_manifest("solver", tmp_path))


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "section", "file": "test.md", "phase": "task_start"},
        {"name": "section", "file": "files/test.md"},
        {"name": "section", "file": "../outside.md"},
        {"name": "section", "file": "link.md"},
    ],
)
def test_invalid_manifest_keeps_diagnostics_and_does_not_rewrite(tmp_path: Path, entry):
    files = tmp_path / "prompt_sections/files"
    files.mkdir(parents=True)
    (files / "test.md").write_text("synthetic instruction")
    outside = tmp_path.parent / (tmp_path.name + "-outside.md")
    outside.write_text("OTHER_ROLE_SECRET")
    (files / "link.md").symlink_to(outside)
    manifest = tmp_path / "prompt_sections/sections.yaml"
    manifest.write_text(yaml.safe_dump({"sections": [entry]}))
    before = manifest.read_bytes()
    checks = _check_prompt_sections_manifest("solver", tmp_path)
    assert any(c.status == "failed" for c in checks)
    context = _repair_failed_file_context(tmp_path, [{"name": c.name} for c in checks])
    assert "prompt_sections/sections.yaml" in context
    assert "OTHER_ROLE_SECRET" not in context
    assert manifest.read_bytes() == before
