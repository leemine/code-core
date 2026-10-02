# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral description of one configured native capability set."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from openjiuwen.harness_protocol.runtime_policy import RuntimeSurface

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}")


class ProviderCapabilityKind(str, Enum):
    """Namespace used by a Provider's own loader."""

    CATEGORY = "category"
    TOOL = "tool"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    PLUGIN = "plugin"


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    """One configured Provider-native capability.

    This is an inventory declaration, not authorization and not proof of a
    successful startup. The owning Provider must still validate its native
    loader and effective configuration before it accepts a Turn.
    """

    name: str
    kind: ProviderCapabilityKind
    surfaces: frozenset[RuntimeSurface] = frozenset(RuntimeSurface)
    source: str = "builtin"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise ValueError("provider capability name must be normalized and path-safe")
        if not isinstance(self.kind, ProviderCapabilityKind):
            raise TypeError("provider capability kind must be ProviderCapabilityKind")
        surfaces = frozenset(self.surfaces)
        if not surfaces or any(not isinstance(item, RuntimeSurface) for item in surfaces):
            raise ValueError("provider capability surfaces must contain RuntimeSurface values")
        if not isinstance(self.source, str) or not self.source.strip() or self.source != self.source.strip():
            raise ValueError("provider capability source must be a normalized string")
        object.__setattr__(self, "surfaces", surfaces)

    def record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "surfaces": sorted(surface.value for surface in self.surfaces),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilityInventory:
    """Exact configured inventory exposed by a Provider-private compiler."""

    provider_id: str
    capabilities: tuple[ProviderCapability, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_id, str)
            or not self.provider_id.strip()
            or self.provider_id != self.provider_id.strip()
        ):
            raise ValueError("provider capability inventory requires a normalized provider_id")
        if self.schema_version != 1:
            raise ValueError("unsupported provider capability inventory schema")
        values = tuple(self.capabilities)
        if any(not isinstance(item, ProviderCapability) for item in values):
            raise TypeError("provider capability inventory contains an invalid entry")
        keys: set[tuple[ProviderCapabilityKind, str]] = set()
        for item in values:
            key = (item.kind, item.name.casefold())
            if key in keys:
                raise ValueError(
                    f"duplicate provider capability: {item.kind.value}:{item.name}"
                )
            keys.add(key)
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted(values, key=lambda item: (item.kind.value, item.name.casefold()))),
        )

    def for_surface(self, surface: RuntimeSurface) -> tuple[ProviderCapability, ...]:
        if not isinstance(surface, RuntimeSurface):
            raise TypeError("surface must be RuntimeSurface")
        return tuple(item for item in self.capabilities if surface in item.surfaces)

    def record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "capabilities": [item.record() for item in self.capabilities],
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.record(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        return hashlib.sha256(payload).hexdigest()


__all__ = [
    "ProviderCapability",
    "ProviderCapabilityInventory",
    "ProviderCapabilityKind",
]
