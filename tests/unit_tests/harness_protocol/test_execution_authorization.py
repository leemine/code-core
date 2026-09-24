# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Public authorization is immutable, explicit, and strictly typed."""
from dataclasses import FrozenInstanceError

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization


@pytest.mark.parametrize("value", [0, 1, "false", None, {}, []])
def test_authorization_does_not_coerce_untrusted_values(value):
    with pytest.raises(TypeError, match="boolean"):
        ExecutionAuthorization(value)


def test_authorization_is_frozen_and_optional_for_old_positional_specs():
    value = ExecutionAuthorization(True)
    with pytest.raises(FrozenInstanceError):
        value.full_access = False
    assert AgentExecutionSpec("codex", "r1", None, {}).authorization is None
    with pytest.raises(TypeError, match="ExecutionAuthorization"):
        AgentExecutionSpec("codex", "r1", authorization={"full_access": True})
