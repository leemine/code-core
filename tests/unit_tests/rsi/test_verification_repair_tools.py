# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The repair factory must provide executable, role-local file operations."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.core.runner import Runner
from openjiuwen.rsi.harness_rsi.member_optimizer.agents import factory
from openjiuwen.rsi.harness_rsi.member_optimizer.verification import HarnessRepairAgent


@pytest.mark.asyncio
async def test_repair_can_edit_its_package_but_not_another_role(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "role"
    workspace.mkdir()
    outside = tmp_path / "other-role.yaml"
    outside.write_text("private: unchanged\n", encoding="utf-8")
    manifest = workspace / "sections.yaml"
    manifest.write_text("sections: invalid\n", encoding="utf-8")
    (workspace / "escape.yaml").symlink_to(outside)
    monkeypatch.setattr(factory, "load_member_optimizer_model", lambda _: object())
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: kwargs)
    await Runner.start()
    config = None
    try:
        config = factory.create_verification_repair_agent(model_config_ref="test-only", workspace=workspace)
        tools = {tool.card.name: tool for tool in config["tools"]}
        assert set(tools) == {"read_file", "write_file", "edit_file"}
        assert config["restrict_to_work_dir"] is True
        assert config["auto_create_workspace"] is False
        read = tools["read_file"]
        write = tools["write_file"]
        assert (await read.invoke({"file_path": str(manifest)})).success
        assert (await write.invoke({"file_path": str(manifest), "content": "sections: []\n"})).success
        assert manifest.read_text(encoding="utf-8") == "sections: []\n"
        for path in (outside, workspace / ".." / outside.name, workspace / "escape.yaml"):
            result = await read.invoke({"file_path": str(path)})
            assert not result.success
            assert "private: unchanged" not in str(result)
            result = await write.invoke({"file_path": str(path), "content": "unauthorized"})
            assert not result.success
        assert outside.read_text(encoding="utf-8") == "private: unchanged\n"
    finally:
        if config is not None:
            Runner.resource_mgr.remove_sys_operation(config["sys_operation"].id)
        await Runner.stop()


@pytest.mark.asyncio
async def test_repair_initialization_preserves_existing_package_files(monkeypatch, tmp_path: Path):
    """Use the real lazy initializer: no agent scaffold belongs in a role package."""
    manifest = tmp_path / "harness_config.yaml"
    manifest.write_text("name: synthetic-package\n", encoding="utf-8")
    model = factory.Model(
        model_client_config=factory.ModelClientConfig(
            model_name="synthetic",
            client_provider="OpenAI",
            api_base="http://127.0.0.1:1/v1",
            api_key="test-only",
        )
    )
    monkeypatch.setattr(factory, "load_member_optimizer_model", lambda _: model)
    await Runner.start()
    agent = None
    try:
        agent = factory.create_verification_repair_agent(model_config_ref="test-only", workspace=tmp_path)
        await agent.ensure_initialized()
        names = await asyncio.to_thread(lambda: sorted(path.name for path in tmp_path.iterdir()))
        assert names == [
            "harness_config.yaml",
        ]
        assert manifest.read_text(encoding="utf-8") == "name: synthetic-package\n"
    finally:
        if agent is not None:
            Runner.resource_mgr.remove_sys_operation(agent.deep_config.sys_operation.id)
        await Runner.stop()


@pytest.mark.asyncio
async def test_failed_repair_assembly_releases_its_file_operation(monkeypatch, tmp_path: Path):
    original = factory._build_file_tools_for_workspace
    operation_ids = []

    def record_operation(**kwargs):
        tools, operation = original(**kwargs)
        operation_ids.append(operation.id)
        return tools, operation

    def fail_model(_):
        raise ValueError("invalid model configuration")

    monkeypatch.setattr(factory, "_build_file_tools_for_workspace", record_operation)
    monkeypatch.setattr(factory, "load_member_optimizer_model", fail_model)
    await Runner.start()
    try:
        with pytest.raises(ValueError, match="invalid model configuration"):
            factory.create_verification_repair_agent(model_config_ref="invalid", workspace=tmp_path)
        assert len(operation_ids) == 1
        assert Runner.resource_mgr.get_sys_operation(operation_ids[0]) is None
    finally:
        await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_repair_invocation_releases_only_its_operation(monkeypatch, tmp_path: Path, outcome):
    operation_ids = []

    def make_agent(**kwargs):
        operation = kwargs["sys_operation"]
        operation_ids.append(operation.id)

        async def invoke(**_):
            assert Runner.resource_mgr.get_sys_operation(operation.id) is operation
            if outcome == "failure":
                raise RuntimeError("repair failed")
            if outcome == "cancelled":
                raise asyncio.CancelledError
            return {"output": "repair attempted"}

        return SimpleNamespace(card=kwargs["card"], deep_config=SimpleNamespace(sys_operation=operation), invoke=invoke)

    monkeypatch.setattr(factory, "load_member_optimizer_model", lambda _: object())
    monkeypatch.setattr(factory, "create_deep_agent", make_agent)
    await Runner.start()
    other = None
    try:
        _, other = factory._build_file_tools_for_workspace(workspace=tmp_path / "other", agent_name="other")
        repair = HarnessRepairAgent("test-only")
        call = repair.repair_role(tmp_path, "role", [{"name": "prompt_section_ref:role:0", "error": "invalid"}])
        if outcome == "success":
            assert (await call)["status"] == "attempted"
        else:
            with pytest.raises(RuntimeError if outcome == "failure" else asyncio.CancelledError):
                await call
        assert len(operation_ids) == 1
        assert Runner.resource_mgr.get_sys_operation(operation_ids[0]) is None
        assert Runner.resource_mgr.get_sys_operation(other.id) is other
    finally:
        if other is not None:
            Runner.resource_mgr.remove_sys_operation(other.id)
        await Runner.stop()
