# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cheap construction and explicit authorization compilation."""

from openjiuwen.harness_protocol import ExecutionAuthorization, HarnessCard, JsonObject
from openjiuwen.harness_protocol.models import freeze_json_object

from .config import OpenCodeHarnessConfig
from .harness import OpenCodeHarness


class OpenCodeHarnessProvider:
    @property
    def card(self) -> HarnessCard:
        return OpenCodeHarness.card

    @staticmethod
    def create(config: JsonObject) -> OpenCodeHarness:
        return OpenCodeHarness(OpenCodeHarnessConfig.from_mapping(config))

    @staticmethod
    def compile_authorization(config: JsonObject, authorization: ExecutionAuthorization) -> JsonObject:
        OpenCodeHarnessConfig.from_mapping(config)
        return freeze_json_object({**dict(config), "full_access": authorization.full_access})

    @staticmethod
    def legacy_authorization(config: JsonObject) -> ExecutionAuthorization:
        parsed = OpenCodeHarnessConfig.from_mapping(config)
        return ExecutionAuthorization(full_access=parsed.full_access)
