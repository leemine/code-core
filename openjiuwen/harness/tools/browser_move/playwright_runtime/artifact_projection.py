# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project controlled browser outputs into the existing AgentResult contract."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from enum import Enum
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

from openjiuwen.core.single_agent.schema.agent_result import Artifact, Part

from .identity import BrowserExecutionFileRoots, BrowserExecutionIdentity

_HASH_CHUNK_SIZE = 1024 * 1024
_MAX_METADATA_TEXT_LENGTH = 1024


class BrowserArtifactProjectionError(ValueError):
    """Raised when an output cannot safely enter the product Artifact contract."""


class BrowserArtifactKind(str, Enum):
    """User-visible browser output kinds supported by the projection bridge."""

    DOWNLOAD = "download"
    SCREENSHOT = "screenshot"
    PDF = "pdf"
    TRACE = "trace"
    VIDEO = "video"
    EXPORT = "export"


def _required_metadata_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise BrowserArtifactProjectionError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise BrowserArtifactProjectionError(f"{field_name} is required")
    if len(normalized) > _MAX_METADATA_TEXT_LENGTH:
        raise BrowserArtifactProjectionError(
            f"{field_name} exceeds {_MAX_METADATA_TEXT_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise BrowserArtifactProjectionError(
            f"{field_name} contains control characters"
        )
    return normalized


def _sanitized_source_url(value: object) -> str:
    source_url = _required_metadata_text(value, field_name="source_url")
    if source_url == "about:blank":
        return source_url
    parsed = urlsplit(source_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise BrowserArtifactProjectionError(
            "source_url must be an http(s) URL or about:blank"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserArtifactProjectionError("source_url has an invalid port") from exc
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme.lower(),
            netloc=netloc,
            path=parsed.path or "/",
            query="",
            fragment="",
        )
    )


def _resolve_controlled_output(
    file_path: str | os.PathLike[str],
    *,
    file_roots: BrowserExecutionFileRoots,
) -> tuple[Path, str]:
    if not isinstance(file_roots, BrowserExecutionFileRoots):
        raise BrowserArtifactProjectionError(
            "file_roots must be a BrowserExecutionFileRoots"
        )
    raw_path = Path(file_path).expanduser()
    output_root = Path(file_roots.outputs_root)
    lexical_path = raw_path if raw_path.is_absolute() else output_root / raw_path
    lexical_path = Path(os.path.abspath(os.path.normpath(str(lexical_path))))
    resolved_path = Path(os.path.realpath(lexical_path))
    if os.path.normcase(str(lexical_path)) != os.path.normcase(str(resolved_path)):
        raise BrowserArtifactProjectionError(
            "browser output path must not contain symbolic links"
        )
    try:
        relative_path = resolved_path.relative_to(output_root)
    except ValueError as exc:
        raise BrowserArtifactProjectionError(
            "browser output must be within outputs_root"
        ) from exc
    if not relative_path.parts:
        raise BrowserArtifactProjectionError("browser output must be a regular file")
    workspace_relative = resolved_path.relative_to(Path(file_roots.workspace)).as_posix()
    return resolved_path, workspace_relative


def _hash_regular_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, IsADirectoryError, OSError) as exc:
        raise BrowserArtifactProjectionError(
            "browser output must be an existing regular file"
        ) from exc
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise BrowserArtifactProjectionError(
                "browser output must be an existing regular file"
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _HASH_CHUNK_SIZE):
            digest.update(chunk)
        opened_after = os.fstat(descriptor)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise BrowserArtifactProjectionError(
                "browser output changed during projection"
            ) from exc
        opened_identity = (
            opened_before.st_dev,
            opened_before.st_ino,
            opened_before.st_size,
            opened_before.st_mtime_ns,
        )
        final_identity = (
            opened_after.st_dev,
            opened_after.st_ino,
            opened_after.st_size,
            opened_after.st_mtime_ns,
        )
        path_identity = (
            path_after.st_dev,
            path_after.st_ino,
            path_after.st_size,
            path_after.st_mtime_ns,
        )
        if opened_identity != final_identity or final_identity != path_identity:
            raise BrowserArtifactProjectionError(
                "browser output changed during projection"
            )
        return digest.hexdigest(), opened_after.st_size
    finally:
        os.close(descriptor)


def project_browser_output_artifact(
    file_path: str | os.PathLike[str],
    *,
    execution_identity: BrowserExecutionIdentity,
    file_roots: BrowserExecutionFileRoots,
    kind: BrowserArtifactKind | str,
    source_url: str,
    tool_name: str,
    permission_decision_id: str,
) -> Artifact:
    """Validate one controlled output and map it to the existing Artifact model.

    The file stays in the caller-owned outputs root.  This function neither copies
    it nor embeds its bytes; a product file service can consume the returned
    workspace-relative URL using the already-authorized workspace binding.
    """

    if not isinstance(execution_identity, BrowserExecutionIdentity):
        raise BrowserArtifactProjectionError(
            "execution_identity must be a BrowserExecutionIdentity"
        )
    if execution_identity.instance.workspace != file_roots.workspace:
        raise BrowserArtifactProjectionError(
            "browser Artifact roots do not match execution workspace"
        )
    try:
        artifact_kind = BrowserArtifactKind(kind)
    except (TypeError, ValueError) as exc:
        raise BrowserArtifactProjectionError(
            f"unsupported browser Artifact kind: {kind!r}"
        ) from exc
    producer_tool = _required_metadata_text(tool_name, field_name="tool_name")
    permission_id = _required_metadata_text(
        permission_decision_id,
        field_name="permission_decision_id",
    )
    safe_source_url = _sanitized_source_url(source_url)
    resolved_path, workspace_relative_path = _resolve_controlled_output(
        file_path,
        file_roots=file_roots,
    )
    sha256, size_bytes = _hash_regular_file(resolved_path)
    mime_type = mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
    identity_seed = "\0".join(
        (
            execution_identity.task.task_id,
            workspace_relative_path,
            sha256,
        )
    )
    artifact_id = hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()
    metadata: dict[str, str | int] = {
        "kind": artifact_kind.value,
        "source_url": safe_source_url,
        "producer_tool": producer_tool,
        "task_id": execution_identity.task.task_id,
        "child_turn_id": execution_identity.task.child_turn_id,
        "request_id": execution_identity.task.request_id,
        "permission_decision_id": permission_id,
        "workspace_relative_path": workspace_relative_path,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }
    return Artifact(
        artifactId=f"browser-output-{artifact_id}",
        name=resolved_path.name,
        description=f"Browser {artifact_kind.value} output",
        parts=[
            Part(
                url=workspace_relative_path,
                filename=resolved_path.name,
                media_type=mime_type,
                metadata={
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                },
            )
        ],
        metadata=metadata,
    )


__all__ = [
    "BrowserArtifactKind",
    "BrowserArtifactProjectionError",
    "project_browser_output_artifact",
]
