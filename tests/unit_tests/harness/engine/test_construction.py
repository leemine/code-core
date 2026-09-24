# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Config selection, compatibility and isolated construction contracts."""
import os
import subprocess
import sys

import pytest

from openjiuwen.harness import ExecutionBinding, create_harness_engine, resolve_execution_spec
from openjiuwen.harness_protocol import AgentExecutionSpec, UnsupportedHarnessCapabilityError


def spec(provider="native", revision="r1", config=None, mode=None):
    return AgentExecutionSpec(provider, revision, mode, config or {})


def binding(value, subject="alice", session="s1", workspace="/tmp/work"):
    return ExecutionBinding.create(value, subject_id=subject, host_session_id=session, workspace=workspace)


def test_snapshot_frozen_and_secrets_not_in_repr():
    config = {"model": {"api_key": "test-secret"}, "paths": ["one"]}
    value = spec(config=config)
    config["paths"].append("two")
    assert value.provider_config["paths"] == ("one",)
    assert "test-secret" not in repr(value)
    with pytest.raises(TypeError):
        value.provider_config["new"] = True


def test_precedence_no_merging_or_native_fallback():
    explicit, project, default = spec("codex"), spec("claudecode"), spec()
    assert resolve_execution_spec(explicit=explicit, project=project, default=default) is explicit
    assert resolve_execution_spec(project=project, default=default) is project
    assert resolve_execution_spec(default=default) is default
    with pytest.raises(ValueError, match="no execution"):
        resolve_execution_spec()


def test_binding_isolates_subject_session_workspace_provider_and_content():
    base = spec()
    keys = {binding(base).cache_key, binding(base, subject="bob").cache_key,
            binding(base, session="s2").cache_key, binding(base, workspace="/tmp/other").cache_key,
            binding(spec("codex")).cache_key, binding(spec(revision="r2")).cache_key,
            binding(spec(config={"language": "en"})).cache_key}
    assert len(keys) == 7
    with pytest.raises(ValueError, match="does not match"):
        create_harness_engine(spec(config={"language": "en"}), binding=binding(base))


@pytest.mark.parametrize("provider", ["native", "claudecode", "codex", "dsh"])
def test_existing_providers_construct_without_start(provider):
    value = spec(provider)
    engine = create_harness_engine(value, binding=binding(value))
    assert engine.harness.provider_session_id is None
    assert engine.binding.provider_id == provider


def test_unknown_and_unadapted_mode_do_not_fallback():
    value = spec("not-registered")
    with pytest.raises(ValueError, match="unknown harness provider"):
        create_harness_engine(value, binding=binding(value))
    value = spec(mode="plan")
    with pytest.raises(UnsupportedHarnessCapabilityError):
        create_harness_engine(value, binding=binding(value))


def test_external_construction_does_not_import_native_or_vendor_sdks():
    code = '''
import importlib.abc
import sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("openjiuwen.harness.deep_agent", "openjiuwen.harness.resources",
                                "claude_agent_sdk", "codex", "openai_codex", "deepseek_harness")):
            raise AssertionError("unexpected dependency: " + fullname)
sys.meta_path.insert(0, Block())
from openjiuwen.harness import ExecutionBinding, create_harness_engine
from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization
value = AgentExecutionSpec("claudecode", "r1")
binding = ExecutionBinding.create(value, subject_id="alice", host_session_id="s1", workspace="/tmp/work")
assert create_harness_engine(value, binding=binding).binding is binding
value = AgentExecutionSpec("codex", "r1", authorization=ExecutionAuthorization(True))
binding = ExecutionBinding.create(value, subject_id="alice", host_session_id="s2", workspace="/tmp/work")
assert create_harness_engine(value, binding=binding).binding is binding
'''
    subprocess.run([sys.executable, "-c", code], check=True, env=os.environ.copy(), timeout=30)


@pytest.mark.parametrize("values", [{"provider_id": ""}, {"config_revision": " "},
                                     {"provider_config": {"bad": float("nan")}},
                                     {"requested_mode": ""}])
def test_invalid_spec_rejected(values):
    with pytest.raises((ValueError, TypeError)):
        AgentExecutionSpec(**({"provider_id": "native", "config_revision": "r1"} | values))


def test_relative_workspace_is_not_bound_to_ambient_cwd():
    with pytest.raises(ValueError, match="absolute"):
        binding(spec(), workspace="relative")
