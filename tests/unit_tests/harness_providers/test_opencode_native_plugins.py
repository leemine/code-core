"""Fixed OpenCode native plugin snapshot and staging contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjiuwen.harness_providers.opencode import (
    OpenCodeHarnessConfig,
    OpenCodeModelConfig,
    OpenCodeNativePluginConfig,
    opencode_plugin_content_digest,
)
from openjiuwen.harness_providers.opencode.errors import OpenCodeError
from openjiuwen.harness_providers.opencode.native_plugins import (
    stage_native_plugins,
    validate_native_plugin_packages,
)
from openjiuwen.harness_providers.opencode.options import environment, native_config, validate_readback


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "plugin.js").write_text(
        "export const Managed = async () => ({event: async () => {}, "
        '"tool.execute.before": async () => {}, "tool.execute.after": async () => {}})\n'
    )
    return root


def _plugin(root: Path, **updates) -> OpenCodeNativePluginConfig:
    values = {
        "plugin_id": "guard",
        "source_type": "local",
        "source_locator": str(root),
        "version": "1.0.0",
        "content_sha256": opencode_plugin_content_digest(root),
        "entrypoint": "plugin.js",
        "export_name": "Managed",
        "required_hooks": ("event", "tool.execute.before", "tool.execute.after"),
        "required_tools": (),
    }
    values.update(updates)
    return OpenCodeNativePluginConfig(**values)


def _config(plugin: OpenCodeNativePluginConfig | None = None) -> OpenCodeHarnessConfig:
    return OpenCodeHarnessConfig(
        model=OpenCodeModelConfig("fixture", "http://127.0.0.1:1/v1"),
        native_plugins=None if plugin is None else (plugin,),
    )


def test_config_parses_provider_private_snapshot_and_rejects_unknown_hooks(tmp_path: Path) -> None:
    plugin = _plugin(_source(tmp_path))
    parsed = OpenCodeHarnessConfig.from_mapping(
        {
            "model": {"model": "fixture", "api_base": "http://127.0.0.1:1/v1"},
            "native_plugins": [
                {
                    "plugin_id": plugin.plugin_id,
                    "source_type": plugin.source_type,
                    "source_locator": plugin.source_locator,
                    "version": plugin.version,
                    "content_sha256": plugin.content_sha256,
                    "entrypoint": plugin.entrypoint,
                    "export_name": plugin.export_name,
                    "required_hooks": list(plugin.required_hooks),
                    "required_tools": list(plugin.required_tools),
                }
            ],
        }
    )
    assert parsed.native_plugins == (plugin,)
    assert plugin.content_sha256 not in repr(parsed)
    with pytest.raises(ValueError, match="unsupported OpenCode native plugin hooks"):
        _plugin(Path(plugin.source_locator), required_hooks=("permission.ask",))
    with pytest.raises(ValueError, match="unknown OpenCode native plugin fields"):
        OpenCodeNativePluginConfig.from_mapping({"unknown": True})
    with pytest.raises(ValueError, match="path-safe identifiers"):
        _plugin(Path(plugin.source_locator), required_tools=("unsafe/tool",))


def test_digest_rejects_links_and_detects_source_drift(tmp_path: Path) -> None:
    root = _source(tmp_path)
    plugin = _plugin(root)
    fingerprint = validate_native_plugin_packages((plugin,))
    assert fingerprint and len(fingerprint) == 64
    (root / "plugin.js").write_text("changed")
    with pytest.raises(OpenCodeError, match="native_plugin_source_drift"):
        validate_native_plugin_packages((plugin,))
    (root / "escape.js").symlink_to(tmp_path / "outside.js")
    with pytest.raises(ValueError, match="symlink"):
        opencode_plugin_content_digest(root)


def test_stage_uses_native_wrapper_and_requires_exact_inventory(tmp_path: Path) -> None:
    source = _source(tmp_path)
    plugin = _plugin(source)
    root = tmp_path / "runtime"
    (root / "tmp").mkdir(parents=True)
    fingerprint = validate_native_plugin_packages((plugin,))
    stage = stage_native_plugins(root, (plugin,), fingerprint=fingerprint)
    assert len(stage.specs) == 1 and stage.specs[0].startswith("file://")
    assert not stage.inventory_ready()
    stage.verify_files()
    inventory, expected = stage.inventories[0]
    inventory.write_text(json.dumps(expected))
    stage.verify_inventory()
    inventory.write_text(json.dumps({**expected, "hooks": ["permission.ask"]}))
    with pytest.raises(OpenCodeError, match="native_plugin_inventory_mismatch"):
        stage.verify_inventory()


def test_stage_wrapper_admits_exact_custom_tool_inventory(tmp_path: Path) -> None:
    source = _source(tmp_path)
    plugin = _plugin(source, required_tools=("workflow", "saveArtifact"))
    root = tmp_path / "runtime"
    (root / "tmp").mkdir(parents=True)
    stage = stage_native_plugins(root, (plugin,), fingerprint=validate_native_plugin_packages((plugin,)))
    wrapper = stage.wrappers[0][0].read_text(encoding="utf-8")
    assert 'const expectedTools = ["saveArtifact", "workflow"]' in wrapper
    assert 'typeof hooks.tool[name].execute !== "function"' in wrapper
    assert 'await context.ask({' in wrapper
    assert 'metadata: { managedPlugin: "guard" }' in wrapper


def test_managed_native_config_is_exact_and_disables_only_pure_gate(tmp_path: Path) -> None:
    plugin = _plugin(_source(tmp_path))
    config = _config(plugin)
    spec = "file:///runtime/guard.js"
    expected = native_config(config, plugin_specs=(spec,))
    env = environment(tmp_path / "runtime", expected, "password", allow_native_plugins=True)
    assert expected["plugin"] == [spec]
    assert "OPENCODE_PURE" not in env
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "true"
    actual = {**expected, "agent": {"title": {"disable": True, "permission": {}, "options": {}}}}
    validate_readback(actual, expected)
    with pytest.raises(OpenCodeError, match="effective_plugin_config_mismatch"):
        validate_readback({**actual, "plugin": ["file:///ambient.js"]}, expected)
    assert environment(tmp_path / "plain", native_config(_config()), "password")["OPENCODE_PURE"] == "true"
