# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Immutable, provider-neutral construction input."""
from dataclasses import dataclass, field

from openjiuwen.harness_protocol.models import JsonObject, freeze_json_object


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """Trusted host authorization, independent of model and vendor settings.

    False retains runtime tool policy/approval enforcement. True authorizes
    the provider's full-access policy; it is not an OS isolation guarantee.
    """

    full_access: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.full_access, bool):
            raise TypeError("execution authorization full_access must be a boolean")


@dataclass(frozen=True, slots=True)
class AgentExecutionSpec:
    """Select an execution provider independently of model configuration.

    Configuration is copied and frozen, and deliberately excluded from repr.
    A revision identifies a host-owned configuration snapshot, not a provider
    session. Runtime identity and tools remain in HarnessContext.
    """

    provider_id: str
    config_revision: str
    requested_mode: str | None = None
    provider_config: JsonObject = field(default_factory=dict, repr=False)
    authorization: ExecutionAuthorization | None = None

    def __post_init__(self) -> None:
        for value in (self.provider_id, self.config_revision):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError("provider_id and config_revision must be non-empty normalized strings")
        if self.requested_mode is not None:
            if not isinstance(self.requested_mode, str) or not self.requested_mode.strip():
                raise ValueError("requested_mode must be a non-empty string or None")
        object.__setattr__(self, "provider_config", freeze_json_object(self.provider_config))
        if self.authorization is not None and not isinstance(self.authorization, ExecutionAuthorization):
            raise TypeError("authorization must be ExecutionAuthorization or None")
