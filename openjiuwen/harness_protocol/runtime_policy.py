# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Immutable provider-neutral requirements for one harness process cycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


class RuntimeSurface(str, Enum):
    """Product work environment selected when the host Session is created."""

    WORK = "work"
    CODE = "code"


class RuntimeExecutionState(str, Enum):
    """Execution state within a fixed product Surface."""

    NORMAL = "normal"
    PLAN = "plan"


class WorkspaceAccess(str, Enum):
    """Maximum filesystem side-effect policy required for this process cycle."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class SourceDiscovery(str, Enum):
    """How provider-native context, skill and plugin sources may be discovered."""

    EXPLICIT_ONLY = "explicit_only"


def _normalized_names(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"runtime policy {field_name} must be an array of strings")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"runtime policy {field_name} values must be normalized non-empty strings")
        if value in result:
            raise ValueError(f"runtime policy {field_name} values must be unique")
        result.append(value)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class HarnessRuntimePolicy:
    """Effective host requirements frozen for one provider process cycle.

    This value describes requirements, not a claim that tools or product panels
    are available. Providers translate the workspace/source fields into their
    private startup configuration and verify the effective result. Capability
    and artifact names remain host vocabulary for later catalog/projection
    layers.
    """

    revision: str
    surface: RuntimeSurface
    execution_state: RuntimeExecutionState
    workspace_access: WorkspaceAccess
    context_sources: tuple[str, ...] = ()
    memory_sources: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()
    source_discovery: SourceDiscovery = SourceDiscovery.EXPLICIT_ONLY
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision.strip() or self.revision != self.revision.strip():
            raise ValueError("runtime policy revision must be a normalized non-empty string")
        if self.schema_version != 1:
            raise ValueError("unsupported runtime policy schema version")
        for name, enum_type in (
            ("surface", RuntimeSurface),
            ("execution_state", RuntimeExecutionState),
            ("workspace_access", WorkspaceAccess),
            ("source_discovery", SourceDiscovery),
        ):
            try:
                value = enum_type(getattr(self, name))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"runtime policy {name} is invalid") from exc
            object.__setattr__(self, name, value)
        for name in ("context_sources", "memory_sources", "required_capabilities", "artifact_kinds"):
            object.__setattr__(self, name, _normalized_names(getattr(self, name), field_name=name))
        if (
            self.execution_state is RuntimeExecutionState.PLAN
            and self.workspace_access is not WorkspaceAccess.READ_ONLY
        ):
            raise ValueError("plan runtime policy must be read-only")

    def record(self) -> dict[str, object]:
        """Return the non-secret stable audit representation."""

        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "surface": self.surface.value,
            "execution_state": self.execution_state.value,
            "workspace_access": self.workspace_access.value,
            "source_discovery": self.source_discovery.value,
            "context_sources": list(self.context_sources),
            "memory_sources": list(self.memory_sources),
            "required_capabilities": list(self.required_capabilities),
            "artifact_kinds": list(self.artifact_kinds),
        }

    @property
    def fingerprint(self) -> str:
        """Return an audit digest; it is not a binding or authorization token."""

        encoded = json.dumps(self.record(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "HarnessRuntimePolicy",
    "RuntimeExecutionState",
    "RuntimeSurface",
    "SourceDiscovery",
    "WorkspaceAccess",
]
