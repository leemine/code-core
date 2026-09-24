# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authorization compilation and legacy Binding identities."""
from dataclasses import replace

import pytest

from openjiuwen.harness.engine.config import config_fingerprint
from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization, UnsupportedHarnessCapabilityError
from openjiuwen.harness_providers import construction


def test_legacy_fingerprints_are_identical_to_pre_authorization_source():
    spec = AgentExecutionSpec("codex", "old-r1", provider_config={"model": {"model": "fixture"}})
    assert config_fingerprint(spec) == "72282159ebaa0f09e27125b00b4a9c87136226711684164b1ce652fdec2ce0e1"
    full = construction.apply_legacy_full_access(spec)
    assert full.authorization is None
    assert config_fingerprint(full) == "6b829962018a9c9becd2a36ccb34d104549f21bc93620a099e38b238573f4688"
    assert construction.compile_execution(spec) is spec.provider_config
    assert len({config_fingerprint(spec), *(config_fingerprint(replace(spec, authorization=ExecutionAuthorization(v)))
                                           for v in (False, True))}) == 3


@pytest.mark.parametrize("full_access", [False, True])
def test_codex_compiles_host_intent_without_mutation(full_access):
    original = {"bypass_approvals_and_sandbox": not full_access, "model": {"model": "fixture"}}
    spec = AgentExecutionSpec("codex", "r1", provider_config=original,
                              authorization=ExecutionAuthorization(full_access))
    compiled = construction.compile_execution(spec)
    assert compiled["bypass_approvals_and_sandbox"] is full_access
    assert compiled["mcp_default_tools_approval_mode"] == ("auto" if full_access else "prompt")
    assert original["bypass_approvals_and_sandbox"] is not full_access
    assert construction.execution_authorization(spec).full_access is full_access
    with pytest.raises(TypeError):
        compiled["changed"] = True


@pytest.mark.parametrize("config", [
    {"thread_config": {"approval_policy": "never"}},
    {"thread_config": {"mcp_servers": {"example": {"default_tools_approval_mode": "auto"}}}},
    {"config_overrides": ['sandbox_mode="danger-full-access"']},
    {"config_overrides": ['profiles.example.approval_policy="never"']},
    {"config_overrides": ['permissions=[{default_permissions="allow"}]']},
])
def test_explicit_authorization_rejects_conflicting_raw_overrides(config):
    spec = AgentExecutionSpec("codex", "r1", provider_config=config, authorization=ExecutionAuthorization())
    with pytest.raises(ValueError, match="conflict"):
        construction.compile_execution(spec)
    assert construction.compile_execution(replace(spec, authorization=None)) == spec.provider_config


def test_model_configuration_never_grants_full_access():
    spec = AgentExecutionSpec("codex", "r1", provider_config={"model": {"provider": "full_access"}})
    assert not construction.execution_authorization(spec).full_access


@pytest.mark.parametrize("provider", ["native", "claudecode", "dsh"])
def test_unadapted_provider_fails_explicit_authorization_without_fallback(provider):
    legacy = AgentExecutionSpec(provider, "r1")
    assert construction.apply_legacy_full_access(legacy) is legacy
    with pytest.raises(UnsupportedHarnessCapabilityError, match="explicit execution authorization"):
        construction.create_execution(replace(legacy, authorization=ExecutionAuthorization(True)))


def test_second_provider_can_compile_without_host_vendor_branches(monkeypatch):
    class AnotherProvider:
        @staticmethod
        def compile_authorization(config, authorization):
            return {"policy": "allow" if authorization.full_access else "ask"}

        @staticmethod
        def legacy_authorization(config):
            return ExecutionAuthorization(False)

    monkeypatch.setattr(construction, "resolve_provider", lambda _: AnotherProvider())
    spec = AgentExecutionSpec("test-provider", "r1", authorization=ExecutionAuthorization(True))
    assert construction.compile_execution(spec) == {"policy": "allow"}
    assert construction.execution_authorization(spec).full_access


def test_full_access_cannot_claim_restricted_source_admission():
    spec = AgentExecutionSpec("codex", "r1", provider_config={"startup_source_roots": ["/tmp/work"]},
                              authorization=ExecutionAuthorization(True))
    with pytest.raises(ValueError, match="restricted startup sources"):
        construction.compile_execution(spec)
