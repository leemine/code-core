# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Provider factory for the Codex harness."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping

from openjiuwen.harness_protocol import ExecutionAuthorization, HarnessCard, JsonObject
from openjiuwen.harness_protocol.models import freeze_json_object
from openjiuwen.harness_providers.codex.config import CodexHarnessConfig
from openjiuwen.harness_providers.codex.harness import CodexHarness


def _permission_config(config: JsonObject, full_access: bool) -> JsonObject:
    return freeze_json_object({
        **dict(config),
        "bypass_approvals_and_sandbox": full_access,
        "mcp_default_tools_approval_mode": "auto" if full_access else "prompt",
    })


def legacy_full_access_config(config: JsonObject) -> JsonObject:
    """Exact historical facade projection; keep old JSON/fingerprints unchanged."""
    return _permission_config(config, True)


class CodexHarnessProvider:
    """Validate provider configuration and create an unstarted Codex harness."""

    @property
    def card(self) -> HarnessCard:
        return CodexHarness.card

    @staticmethod
    def create(config: JsonObject) -> CodexHarness:
        return CodexHarness(CodexHarnessConfig.from_mapping(config))

    @staticmethod
    def compile_authorization(config: JsonObject, authorization: ExecutionAuthorization) -> JsonObject:
        """Translate host intent without mutating or default-filling its snapshot.

        Normal access keeps Codex's runtime permission verification; it is not
        an assertion that arbitrary ambient CLI policy is a filesystem sandbox.
        """
        parsed = CodexHarnessConfig.from_mapping(config)
        reserved = {
            "approval_policy", "approvals_reviewer", "sandbox_mode", "sandbox_workspace_write",
            "permissions", "default_permissions", "default_tools_approval_mode", "profiles",
        }

        def conflicts(value: object) -> bool:
            if isinstance(value, Mapping):
                return bool(reserved.intersection(value)) or any(conflicts(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return any(conflicts(item) for item in value)
            return False

        overrides = [parsed.thread_config]
        for override in parsed.config_overrides:
            try:
                overrides.append(tomllib.loads(override))
            except tomllib.TOMLDecodeError as exc:
                raise ValueError("explicit authorization requires valid Codex config overrides") from exc
        if any(conflicts(value) for value in overrides):
            raise ValueError("Codex permission overrides conflict with explicit execution authorization")
        if authorization.full_access and parsed.startup_source_roots is not None:
            raise ValueError("Codex restricted startup sources conflict with full access")
        return _permission_config(config, authorization.full_access)

    @staticmethod
    def legacy_authorization(config: JsonObject) -> ExecutionAuthorization:
        """Retain the old explicitly configured bypass, never infer from model."""
        return ExecutionAuthorization(full_access=config.get("bypass_approvals_and_sandbox") is True)


__all__ = ["CodexHarnessProvider"]
