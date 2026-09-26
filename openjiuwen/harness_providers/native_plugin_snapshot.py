# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral pure helpers for fixed local native-plugin trees.

This is deliberately not a plugin ABI.  Providers retain their own config,
manifest, loader and error vocabulary; only deterministic byte/path checks are
shared.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class NativePluginSnapshotError(ValueError):
    """A local native-plugin tree is not a deterministic regular-file tree."""


def native_plugin_tree_digest(root: Path) -> str:
    """Hash relative paths and file bytes, rejecting links and special files."""
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise NativePluginSnapshotError(f"native plugin root is not an absolute regular directory: {root}")
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise NativePluginSnapshotError(f"native plugin tree contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise NativePluginSnapshotError(f"native plugin tree contains a non-regular file: {path}")
        files.append(path)
    if not files:
        raise NativePluginSnapshotError(f"native plugin tree is empty: {root}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def native_plugin_relative_file(root: Path, value: str, *, suffixes: frozenset[str]) -> Path:
    """Resolve one relative regular file without permitting a link escape."""
    if not isinstance(value, str) or not value or Path(value).is_absolute() or "\x00" in value:
        raise NativePluginSnapshotError("native plugin entrypoint must be a non-empty relative path")
    candidate = root / value
    if candidate.is_symlink():
        raise NativePluginSnapshotError(f"native plugin entrypoint is a symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise NativePluginSnapshotError(f"native plugin entrypoint is unavailable: {candidate}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise NativePluginSnapshotError(f"native plugin entrypoint escapes its package: {candidate}")
    if resolved.suffix not in suffixes:
        raise NativePluginSnapshotError(f"native plugin entrypoint has an unsupported suffix: {candidate}")
    return resolved


__all__ = [
    "NativePluginSnapshotError",
    "native_plugin_relative_file",
    "native_plugin_tree_digest",
]
