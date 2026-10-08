# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runtime permission control remains optional for existing Providers."""

from openjiuwen.harness_protocol import HarnessProtocol, HarnessRuntimeAuthorization
from tests.unit_tests.harness_protocol.test_protocol import _Harness


def test_legacy_provider_remains_valid_without_runtime_authorization():
    legacy = _Harness()
    assert isinstance(legacy, HarnessProtocol)
    assert not isinstance(legacy, HarnessRuntimeAuthorization)
