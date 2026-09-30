"""Provider-neutral cold-start runtime policy values."""

from dataclasses import FrozenInstanceError, replace

import pytest

from openjiuwen.harness_protocol import (
    HarnessContext,
    HarnessRuntimePolicy,
    RuntimeExecutionState,
    RuntimeSurface,
    WorkspaceAccess,
)


def _policy(**overrides):
    values = {
        "revision": "surface-v1",
        "surface": RuntimeSurface.CODE,
        "execution_state": RuntimeExecutionState.NORMAL,
        "workspace_access": WorkspaceAccess.WORKSPACE_WRITE,
        "context_sources": ("project_rules",),
        "memory_sources": ("project_memory",),
        "required_capabilities": ("filesystem", "terminal"),
        "artifact_kinds": ("diff", "test"),
    }
    values.update(overrides)
    return HarnessRuntimePolicy(**values)


def test_runtime_policy_is_frozen_stable_and_context_typed():
    policy = _policy()
    same = _policy()
    assert policy.record() == same.record()
    assert policy.fingerprint == same.fingerprint
    assert len(policy.fingerprint) == 64
    with pytest.raises(FrozenInstanceError):
        policy.revision = "changed"
    context = HarnessContext("agent", "agent-1", "session-1", "", runtime_policy=policy)
    assert context.runtime_policy is policy
    with pytest.raises(TypeError, match="HarnessRuntimePolicy"):
        replace(context, runtime_policy={"surface": "code"})


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"revision": " "}, "revision"),
        ({"context_sources": ("project", "project")}, "unique"),
        ({"required_capabilities": ("",)}, "normalized"),
        ({"schema_version": 2}, "schema"),
        ({"surface": "unknown"}, "surface"),
        ({"execution_state": "plan"}, "read-only"),
    ],
)
def test_invalid_runtime_policy_fails_closed(overrides, match):
    with pytest.raises((TypeError, ValueError), match=match):
        _policy(**overrides)


def test_plan_policy_requires_read_only_access():
    policy = _policy(
        execution_state=RuntimeExecutionState.PLAN,
        workspace_access=WorkspaceAccess.READ_ONLY,
    )
    assert policy.execution_state is RuntimeExecutionState.PLAN
    assert policy.workspace_access is WorkspaceAccess.READ_ONLY
