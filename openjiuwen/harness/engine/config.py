# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Immutable host bindings; no provider lifecycle or queue lives here."""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from openjiuwen.harness_protocol.construction import AgentExecutionSpec
from openjiuwen.harness_protocol.models import json_value_to_builtin


def config_fingerprint(spec: AgentExecutionSpec) -> str:
    """Detect payload changes even when a caller reuses a revision label."""
    payload = [spec.provider_id, spec.config_revision, spec.requested_mode,
               json_value_to_builtin(spec.provider_config)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    """A caller-authorized execution scope and exact configuration identity.

    The host owns persistence and authorization. The digest is not a token or
    permission grant. Binding objects contain no provider configuration.
    """

    subject_id: str
    host_session_id: str
    workspace: str
    provider_id: str
    config_revision: str
    fingerprint: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.subject_id, self.host_session_id, self.workspace,
                      self.provider_id, self.config_revision):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("binding identity fields must be non-empty strings")
        if not Path(self.workspace).is_absolute():
            raise ValueError("workspace must be an absolute path")
        object.__setattr__(self, "workspace", str(Path(self.workspace).resolve()))
        if (not isinstance(self.fingerprint, str) or len(self.fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in self.fingerprint)):
            raise ValueError("binding fingerprint must be a SHA-256 hex digest")

    @classmethod
    def create(cls, spec: AgentExecutionSpec, *, subject_id: str,
               host_session_id: str, workspace: str) -> "ExecutionBinding":
        for value in (subject_id, host_session_id, workspace):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("subject, session and workspace must be non-empty strings")
        path = Path(workspace)
        if not path.is_absolute():
            raise ValueError("workspace must be an absolute path")
        return cls(subject_id, host_session_id, str(path.resolve()), spec.provider_id,
                   spec.config_revision, config_fingerprint(spec))

    @property
    def cache_key(self) -> tuple[str, ...]:
        """Complete scope key, including provider and configuration content."""
        return (self.subject_id, self.host_session_id, self.workspace,
                self.provider_id, self.config_revision, self.fingerprint)

    def validate_spec(self, spec: AgentExecutionSpec) -> None:
        if (self.provider_id, self.config_revision, self.fingerprint) != (
                spec.provider_id, spec.config_revision, config_fingerprint(spec)):
            raise ValueError("execution configuration does not match its binding")
