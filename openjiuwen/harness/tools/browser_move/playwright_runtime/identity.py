# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Non-sensitive identities for one authorized browser execution."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_CAPABILITY_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_ID_LENGTH = 256
_MAX_WORKSPACE_LENGTH = 4096


class BrowserBackend(str, Enum):
    """Browser host selected by the trusted product composition root."""

    MANAGED = "managed"
    REMOTE = "remote"
    EXTENSION = "extension"
    ELECTRON = "electron"


def _normalized_text(
    value: object,
    *,
    field_name: str,
    allow_empty: bool = False,
    forbid_path_separator: bool = False,
    max_length: int = _MAX_ID_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    if _CONTROL_CHARACTERS.search(normalized):
        raise ValueError(f"{field_name} contains control characters")
    if forbid_path_separator and ("/" in normalized or "\\" in normalized):
        raise ValueError(f"{field_name} must not be a path")
    return normalized


def _positive_generation(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _normalized_workspace(value: object) -> str:
    workspace = _normalized_text(
        value,
        field_name="workspace",
        max_length=_MAX_WORKSPACE_LENGTH,
    )
    if not os.path.isabs(workspace):
        raise ValueError("workspace must be an absolute path")
    return os.path.realpath(os.path.normpath(workspace))


def _strict_fields(
    value: object,
    *,
    type_name: str,
    fields: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{type_name} must be a mapping")
    actual = frozenset(value)
    missing = fields - actual
    unknown = actual - fields
    if missing:
        raise ValueError(
            f"{type_name} is missing fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ValueError(
            f"{type_name} has unknown fields: {', '.join(sorted(unknown))}"
        )
    return value


@dataclass(frozen=True, slots=True)
class BrowserProfileIdentity:
    """Authorized persistent login container without connection secrets."""

    profile_id: str
    owner_subject_id: str
    backend: BrowserBackend
    policy_revision: str
    generation: int = 1
    owner_tenant_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_id",
            _normalized_text(
                self.profile_id,
                field_name="profile_id",
                forbid_path_separator=True,
            ),
        )
        object.__setattr__(
            self,
            "owner_subject_id",
            _normalized_text(
                self.owner_subject_id,
                field_name="owner_subject_id",
            ),
        )
        object.__setattr__(
            self,
            "owner_tenant_id",
            _normalized_text(
                self.owner_tenant_id,
                field_name="owner_tenant_id",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "policy_revision",
            _normalized_text(
                self.policy_revision,
                field_name="policy_revision",
            ),
        )
        try:
            backend = BrowserBackend(self.backend)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown browser backend: {self.backend!r}") from exc
        object.__setattr__(self, "backend", backend)
        object.__setattr__(
            self,
            "generation",
            _positive_generation(self.generation, field_name="profile generation"),
        )

    def to_dict(self) -> dict[str, str | int]:
        return {
            "profile_id": self.profile_id,
            "owner_subject_id": self.owner_subject_id,
            "owner_tenant_id": self.owner_tenant_id,
            "backend": self.backend.value,
            "policy_revision": self.policy_revision,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BrowserProfileIdentity":
        raw = _strict_fields(
            value,
            type_name=cls.__name__,
            fields=frozenset(
                {
                    "profile_id",
                    "owner_subject_id",
                    "owner_tenant_id",
                    "backend",
                    "policy_revision",
                    "generation",
                }
            ),
        )
        return cls(
            profile_id=raw["profile_id"],  # type: ignore[arg-type]
            owner_subject_id=raw["owner_subject_id"],  # type: ignore[arg-type]
            owner_tenant_id=raw["owner_tenant_id"],  # type: ignore[arg-type]
            backend=raw["backend"],  # type: ignore[arg-type]
            policy_revision=raw["policy_revision"],  # type: ignore[arg-type]
            generation=raw["generation"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BrowserInstanceIdentity:
    """One browser process/target generation bound to a product child."""

    instance_id: str
    profile_id: str
    profile_generation: int
    parent_session_id: str
    subagent_id: str
    workspace: str
    generation: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "instance_id",
            "parent_session_id",
            "subagent_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalized_text(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "profile_id",
            _normalized_text(
                self.profile_id,
                field_name="profile_id",
                forbid_path_separator=True,
            ),
        )
        object.__setattr__(self, "workspace", _normalized_workspace(self.workspace))
        object.__setattr__(
            self,
            "profile_generation",
            _positive_generation(
                self.profile_generation,
                field_name="profile_generation",
            ),
        )
        object.__setattr__(
            self,
            "generation",
            _positive_generation(self.generation, field_name="instance generation"),
        )

    def to_dict(self) -> dict[str, str | int]:
        return {
            "instance_id": self.instance_id,
            "profile_id": self.profile_id,
            "profile_generation": self.profile_generation,
            "parent_session_id": self.parent_session_id,
            "subagent_id": self.subagent_id,
            "workspace": self.workspace,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BrowserInstanceIdentity":
        raw = _strict_fields(
            value,
            type_name=cls.__name__,
            fields=frozenset(
                {
                    "instance_id",
                    "profile_id",
                    "profile_generation",
                    "parent_session_id",
                    "subagent_id",
                    "workspace",
                    "generation",
                }
            ),
        )
        return cls(
            instance_id=raw["instance_id"],  # type: ignore[arg-type]
            profile_id=raw["profile_id"],  # type: ignore[arg-type]
            profile_generation=raw["profile_generation"],  # type: ignore[arg-type]
            parent_session_id=raw["parent_session_id"],  # type: ignore[arg-type]
            subagent_id=raw["subagent_id"],  # type: ignore[arg-type]
            workspace=raw["workspace"],  # type: ignore[arg-type]
            generation=raw["generation"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BrowserTaskIdentity:
    """One delegated browser task within an admitted instance generation."""

    task_id: str
    instance_id: str
    instance_generation: int
    child_turn_id: str
    request_id: str
    capability_fingerprint: str

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "instance_id",
            "child_turn_id",
            "request_id",
            "capability_fingerprint",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalized_text(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "instance_generation",
            _positive_generation(
                self.instance_generation,
                field_name="instance_generation",
            ),
        )
        if not _CAPABILITY_FINGERPRINT.fullmatch(self.capability_fingerprint):
            raise ValueError(
                "capability_fingerprint must be a sha256:<64 lowercase hex> digest"
            )

    def to_dict(self) -> dict[str, str | int]:
        return {
            "task_id": self.task_id,
            "instance_id": self.instance_id,
            "instance_generation": self.instance_generation,
            "child_turn_id": self.child_turn_id,
            "request_id": self.request_id,
            "capability_fingerprint": self.capability_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BrowserTaskIdentity":
        raw = _strict_fields(
            value,
            type_name=cls.__name__,
            fields=frozenset(
                {
                    "task_id",
                    "instance_id",
                    "instance_generation",
                    "child_turn_id",
                    "request_id",
                    "capability_fingerprint",
                }
            ),
        )
        return cls(
            task_id=raw["task_id"],  # type: ignore[arg-type]
            instance_id=raw["instance_id"],  # type: ignore[arg-type]
            instance_generation=raw["instance_generation"],  # type: ignore[arg-type]
            child_turn_id=raw["child_turn_id"],  # type: ignore[arg-type]
            request_id=raw["request_id"],  # type: ignore[arg-type]
            capability_fingerprint=raw["capability_fingerprint"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BrowserExecutionFileRoots:
    """Authorized task file roots, separated by data purpose."""

    workspace: str
    uploads_root: str
    outputs_root: str
    audit_root: str

    def __post_init__(self) -> None:
        workspace = _normalized_workspace(self.workspace)
        object.__setattr__(self, "workspace", workspace)
        roots: dict[str, str] = {}
        workspace_path = os.path.normcase(workspace)
        for field_name in ("uploads_root", "outputs_root", "audit_root"):
            root = _normalized_workspace(getattr(self, field_name))
            try:
                common = os.path.commonpath(
                    (workspace_path, os.path.normcase(root))
                )
            except ValueError as exc:
                raise ValueError(f"{field_name} must be within workspace") from exc
            if common != workspace_path or root == workspace:
                raise ValueError(f"{field_name} must be within workspace")
            roots[field_name] = root
            object.__setattr__(self, field_name, root)

        items = tuple(roots.items())
        for index, (left_name, left_root) in enumerate(items):
            for right_name, right_root in items[index + 1 :]:
                common = os.path.commonpath(
                    (os.path.normcase(left_root), os.path.normcase(right_root))
                )
                if common in {
                    os.path.normcase(left_root),
                    os.path.normcase(right_root),
                }:
                    raise ValueError(
                        f"{left_name} and {right_name} must not overlap"
                    )

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace": self.workspace,
            "uploads_root": self.uploads_root,
            "outputs_root": self.outputs_root,
            "audit_root": self.audit_root,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BrowserExecutionFileRoots":
        raw = _strict_fields(
            value,
            type_name=cls.__name__,
            fields=frozenset(
                {"workspace", "uploads_root", "outputs_root", "audit_root"}
            ),
        )
        return cls(
            workspace=raw["workspace"],  # type: ignore[arg-type]
            uploads_root=raw["uploads_root"],  # type: ignore[arg-type]
            outputs_root=raw["outputs_root"],  # type: ignore[arg-type]
            audit_root=raw["audit_root"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class BrowserExecutionIdentity:
    """Validated Profile -> Instance -> Task identity chain."""

    profile: BrowserProfileIdentity
    instance: BrowserInstanceIdentity
    task: BrowserTaskIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.profile, BrowserProfileIdentity):
            raise ValueError("profile must be a BrowserProfileIdentity")
        if not isinstance(self.instance, BrowserInstanceIdentity):
            raise ValueError("instance must be a BrowserInstanceIdentity")
        if not isinstance(self.task, BrowserTaskIdentity):
            raise ValueError("task must be a BrowserTaskIdentity")
        if self.instance.profile_id != self.profile.profile_id:
            raise ValueError("browser instance profile_id does not match profile")
        if self.instance.profile_generation != self.profile.generation:
            raise ValueError(
                "browser instance profile_generation does not match profile"
            )
        if self.task.instance_id != self.instance.instance_id:
            raise ValueError("browser task instance_id does not match instance")
        if self.task.instance_generation != self.instance.generation:
            raise ValueError(
                "browser task instance_generation does not match instance"
            )

    def validate_scope(
        self,
        *,
        owner_subject_id: str,
        parent_session_id: str,
        subagent_id: str,
        workspace: str,
        owner_tenant_id: str = "",
    ) -> None:
        expected = {
            "owner_subject_id": _normalized_text(
                owner_subject_id,
                field_name="owner_subject_id",
            ),
            "owner_tenant_id": _normalized_text(
                owner_tenant_id,
                field_name="owner_tenant_id",
                allow_empty=True,
            ),
            "parent_session_id": _normalized_text(
                parent_session_id,
                field_name="parent_session_id",
            ),
            "subagent_id": _normalized_text(
                subagent_id,
                field_name="subagent_id",
            ),
            "workspace": _normalized_workspace(workspace),
        }
        actual = {
            "owner_subject_id": self.profile.owner_subject_id,
            "owner_tenant_id": self.profile.owner_tenant_id,
            "parent_session_id": self.instance.parent_session_id,
            "subagent_id": self.instance.subagent_id,
            "workspace": self.instance.workspace,
        }
        mismatches = [name for name, value in expected.items() if actual[name] != value]
        if mismatches:
            raise ValueError(
                "browser execution scope mismatch: " + ", ".join(mismatches)
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.to_dict(),
            "instance": self.instance.to_dict(),
            "task": self.task.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BrowserExecutionIdentity":
        raw = _strict_fields(
            value,
            type_name=cls.__name__,
            fields=frozenset({"profile", "instance", "task"}),
        )
        return cls(
            profile=BrowserProfileIdentity.from_dict(raw["profile"]),  # type: ignore[arg-type]
            instance=BrowserInstanceIdentity.from_dict(raw["instance"]),  # type: ignore[arg-type]
            task=BrowserTaskIdentity.from_dict(raw["task"]),  # type: ignore[arg-type]
        )


__all__ = [
    "BrowserBackend",
    "BrowserExecutionFileRoots",
    "BrowserExecutionIdentity",
    "BrowserInstanceIdentity",
    "BrowserProfileIdentity",
    "BrowserTaskIdentity",
]
