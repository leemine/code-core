# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fail-closed admission for fixed OpenCode JS/TS native plugins.

OpenCode remains the only module loader and hook dispatcher.  The generated
wrapper merely verifies the hook inventory returned by one pinned local module
and writes a generation-private startup handshake for host readback.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from openjiuwen.harness_providers.native_plugin_snapshot import (
    NativePluginSnapshotError,
    native_plugin_relative_file,
    native_plugin_tree_digest,
)

from .errors import OpenCodeError

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SUPPORTED_HOOKS = frozenset(
    {
        "chat.message",
        "chat.params",
        "event",
        "experimental.chat.system.transform",
        "tool.execute.before",
        "tool.execute.after",
    }
)
_ENTRY_SUFFIXES = frozenset({".js", ".ts"})


@dataclass(frozen=True, slots=True)
class OpenCodeNativePluginConfig:
    """One host-authorized local OpenCode module and exact hook inventory."""

    plugin_id: str
    source_type: str
    source_locator: str
    version: str
    content_sha256: str
    entrypoint: str
    export_name: str
    required_hooks: tuple[str, ...]
    required_tools: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("plugin_id", "source_type", "source_locator", "version", "entrypoint", "export_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"OpenCode native plugin {name} must be a non-empty normalized string")
        if not _IDENTITY_RE.fullmatch(self.plugin_id) or not _IDENTITY_RE.fullmatch(self.version):
            raise ValueError("OpenCode native plugin identity and version must be path-safe")
        if self.source_type != "local" or not Path(self.source_locator).is_absolute():
            raise ValueError("OpenCode native plugins require a prepared absolute local source")
        if not _DIGEST_RE.fullmatch(self.content_sha256):
            raise ValueError("OpenCode native plugin content_sha256 must be a lowercase SHA-256 digest")
        if not _IDENTIFIER_RE.fullmatch(self.export_name):
            raise ValueError("OpenCode native plugin export_name must be a JavaScript identifier")
        if not isinstance(self.required_hooks, (list, tuple)) or not self.required_hooks:
            raise TypeError("OpenCode native plugin required_hooks must be a non-empty array")
        if any(not isinstance(item, str) or not item for item in self.required_hooks):
            raise TypeError("OpenCode native plugin required_hooks must contain strings")
        if len(set(self.required_hooks)) != len(self.required_hooks):
            raise ValueError("OpenCode native plugin required_hooks must not contain duplicates")
        unsupported = set(self.required_hooks) - _SUPPORTED_HOOKS
        if unsupported:
            raise ValueError(f"unsupported OpenCode native plugin hooks: {', '.join(sorted(unsupported))}")
        if not isinstance(self.required_tools, (list, tuple)):
            raise TypeError("OpenCode native plugin required_tools must be an array")
        if any(not isinstance(item, str) or not _IDENTITY_RE.fullmatch(item) for item in self.required_tools):
            raise ValueError("OpenCode native plugin required_tools must contain path-safe identifiers")
        if len(set(self.required_tools)) != len(self.required_tools):
            raise ValueError("OpenCode native plugin required_tools must not contain duplicates")
        if not isinstance(self.enabled, bool):
            raise TypeError("OpenCode native plugin enabled must be a boolean")
        object.__setattr__(self, "required_hooks", tuple(self.required_hooks))
        object.__setattr__(self, "required_tools", tuple(self.required_tools))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "OpenCodeNativePluginConfig":
        if not isinstance(value, Mapping):
            raise TypeError("OpenCode native plugin configuration must be an object")
        known = {
            "plugin_id",
            "source_type",
            "source_locator",
            "version",
            "content_sha256",
            "entrypoint",
            "export_name",
            "required_hooks",
            "required_tools",
            "enabled",
        }
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown OpenCode native plugin fields: {', '.join(unknown)}")
        return cls(**dict(value))  # type: ignore[arg-type]


def _plugin_error(reason: str) -> OpenCodeError:
    return OpenCodeError(reason, category="process_start_failed")


def opencode_plugin_content_digest(root: str | Path) -> str:
    path = Path(root)
    try:
        return native_plugin_tree_digest(path)
    except NativePluginSnapshotError as exc:
        raise ValueError(f"invalid OpenCode native plugin package: {exc}") from exc


def _validated(plugin: OpenCodeNativePluginConfig) -> tuple[Path, Path, str]:
    try:
        declared = Path(plugin.source_locator)
        if declared.is_symlink():
            raise NativePluginSnapshotError(f"native plugin root is a symlink: {declared}")
        root = declared.resolve(strict=True)
        actual = native_plugin_tree_digest(root)
        entrypoint = native_plugin_relative_file(root, plugin.entrypoint, suffixes=_ENTRY_SUFFIXES)
    except (OSError, NativePluginSnapshotError) as exc:
        raise _plugin_error("native_plugin_source_invalid") from exc
    if actual != plugin.content_sha256:
        raise _plugin_error("native_plugin_source_drift")
    return root, entrypoint, actual


def validate_native_plugin_packages(
    plugins: tuple[OpenCodeNativePluginConfig, ...] | None,
) -> str | None:
    """Validate source bytes and return the Binding/checkpoint fingerprint."""
    if plugins is None:
        return None
    if len({plugin.plugin_id for plugin in plugins}) != len(plugins):
        raise _plugin_error("native_plugin_id_conflict")
    fingerprint = hashlib.sha256()
    for plugin in sorted(plugins, key=lambda item: item.plugin_id):
        root, entrypoint, actual = _validated(plugin)
        snapshot = {
            "plugin_id": plugin.plugin_id,
            "source_type": plugin.source_type,
            "source_locator": str(root),
            "version": plugin.version,
            "content_sha256": actual,
            "entrypoint": entrypoint.relative_to(root).as_posix(),
            "export_name": plugin.export_name,
            "required_hooks": sorted(plugin.required_hooks),
            "required_tools": sorted(plugin.required_tools),
            "enabled": plugin.enabled,
        }
        fingerprint.update(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode())
    return fingerprint.hexdigest()


@dataclass(slots=True)
class NativePluginStage:
    fingerprint: str | None
    specs: tuple[str, ...]
    packages: tuple[tuple[Path, str], ...]
    inventories: tuple[tuple[Path, dict[str, object]], ...]
    wrappers: tuple[tuple[Path, str], ...]

    def verify_files(self) -> None:
        for root, expected in self.packages:
            try:
                actual = native_plugin_tree_digest(root)
            except NativePluginSnapshotError as exc:
                raise _plugin_error("native_plugin_stage_drift") from exc
            if actual != expected:
                raise _plugin_error("native_plugin_stage_drift")
        for path, expected in self.wrappers:
            if path.is_symlink() or not path.is_file():
                raise _plugin_error("native_plugin_wrapper_drift")
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise _plugin_error("native_plugin_wrapper_drift")

    def inventory_ready(self) -> bool:
        return all(path.is_file() and not path.is_symlink() for path, _ in self.inventories)

    def verify_inventory(self) -> None:
        for path, expected in self.inventories:
            try:
                if path.is_symlink() or path.stat().st_size > 8192:
                    raise ValueError
                actual = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise _plugin_error("native_plugin_inventory_invalid") from exc
            if actual != expected:
                raise _plugin_error("native_plugin_inventory_mismatch")


def _wrapper_source(*, entrypoint: Path, export_name: str, inventory: Path, expected: dict[str, object]) -> str:
    return f'''import * as module from {json.dumps(entrypoint.as_uri())}
import {{ writeFile }} from "node:fs/promises"

const implementation = module[{json.dumps(export_name)}]
if (typeof implementation !== "function") throw new Error("managed plugin export is not a function")
export const ManagedOpenCodePlugin = async (input, options) => {{
  const hooks = await implementation(input, options)
  if (!hooks || typeof hooks !== "object" || Array.isArray(hooks)) {{
    throw new Error("managed plugin did not return a hook object")
  }}
  const actual = Object.keys(hooks).sort()
  const expectedHooks = {json.dumps(sorted(expected["hooks"]))}
  const expectedTools = {json.dumps(sorted(expected["tools"]))}
  const expected = [...expectedHooks, ...(expectedTools.length ? ["tool"] : [])].sort()
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {{
    throw new Error("managed plugin hook inventory mismatch: " + JSON.stringify(actual))
  }}
  const invalid = expectedHooks.filter((name) => typeof hooks[name] !== "function")
  if (invalid.length) {{
    throw new Error("managed plugin hook is not callable: " + JSON.stringify(invalid))
  }}
  const actualTools = hooks.tool && typeof hooks.tool === "object" && !Array.isArray(hooks.tool)
    ? Object.keys(hooks.tool).sort()
    : []
  if (JSON.stringify(actualTools) !== JSON.stringify(expectedTools)) {{
    throw new Error("managed plugin tool inventory mismatch: " + JSON.stringify(actualTools))
  }}
  const invalidTools = actualTools.filter((name) =>
    !hooks.tool[name] || typeof hooks.tool[name] !== "object" || typeof hooks.tool[name].execute !== "function"
  )
  if (invalidTools.length) {{
    throw new Error("managed plugin tool is invalid: " + JSON.stringify(invalidTools))
  }}
  const admitted = {{ ...hooks }}
  if (actualTools.length) {{
    admitted.tool = Object.fromEntries(actualTools.map((name) => [name, {{
      ...hooks.tool[name],
      execute: async (args, context) => {{
        if (!context || typeof context.ask !== "function") {{
          throw new Error("managed plugin tool cannot reach the native permission channel")
        }}
        await context.ask({{
          permission: name,
          patterns: ["*"],
          always: [],
          metadata: {{ managedPlugin: {json.dumps(expected["plugin_id"])} }},
        }})
        return hooks.tool[name].execute(args, context)
      }},
    }}]))
  }}
  await writeFile(
    {json.dumps(str(inventory))},
    JSON.stringify({json.dumps(expected, sort_keys=True)}),
    {{ flag: "wx", mode: 0o600 }},
  )
  return admitted
}}
'''


def stage_native_plugins(
    root: Path,
    plugins: tuple[OpenCodeNativePluginConfig, ...] | None,
    *,
    fingerprint: str | None,
) -> NativePluginStage:
    """Copy exact packages and create native-loader inventory wrappers."""
    if plugins is None:
        return NativePluginStage(fingerprint, (), (), (), ())
    expected = validate_native_plugin_packages(plugins)
    if expected != fingerprint:
        raise _plugin_error("native_plugin_source_drift")
    base = root / "managed-plugins"
    packages_dir, wrappers_dir = base / "packages", base / "wrappers"
    packages_dir.mkdir(parents=True, mode=0o700)
    wrappers_dir.mkdir(mode=0o700)
    specs: list[str] = []
    packages: list[tuple[Path, str]] = []
    inventories: list[tuple[Path, dict[str, object]]] = []
    wrappers: list[tuple[Path, str]] = []
    for plugin in sorted(plugins, key=lambda item: item.plugin_id):
        if not plugin.enabled:
            continue
        source, source_entrypoint, actual = _validated(plugin)
        destination = packages_dir / plugin.plugin_id / plugin.version
        destination.parent.mkdir(mode=0o700)
        shutil.copytree(source, destination, symlinks=True)
        if native_plugin_tree_digest(destination) != actual or native_plugin_tree_digest(source) != actual:
            raise _plugin_error("native_plugin_copy_race")
        staged_entrypoint = destination / source_entrypoint.relative_to(source)
        inventory = root / "tmp" / f"native-plugin-{plugin.plugin_id}.json"
        expected_inventory: dict[str, object] = {
            "plugin_id": plugin.plugin_id,
            "version": plugin.version,
            "content_sha256": actual,
            "hooks": sorted(plugin.required_hooks),
            "tools": sorted(plugin.required_tools),
        }
        wrapper = wrappers_dir / f"{plugin.plugin_id}.js"
        wrapper.write_text(
            _wrapper_source(
                entrypoint=staged_entrypoint,
                export_name=plugin.export_name,
                inventory=inventory,
                expected=expected_inventory,
            ),
            encoding="utf-8",
        )
        for path in destination.rglob("*"):
            path.chmod(0o500 if path.is_dir() else 0o400)
        destination.chmod(0o500)
        wrapper.chmod(0o400)
        with wrapper.open("rb") as stream:
            wrapper_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        specs.append(wrapper.as_uri())
        packages.append((destination, actual))
        inventories.append((inventory, expected_inventory))
        wrappers.append((wrapper, wrapper_digest))
    wrappers_dir.chmod(0o500)
    packages_dir.chmod(0o500)
    base.chmod(0o500)
    return NativePluginStage(
        fingerprint,
        tuple(specs),
        tuple(packages),
        tuple(inventories),
        tuple(wrappers),
    )


__all__ = [
    "NativePluginStage",
    "OpenCodeNativePluginConfig",
    "opencode_plugin_content_digest",
    "stage_native_plugins",
    "validate_native_plugin_packages",
]
