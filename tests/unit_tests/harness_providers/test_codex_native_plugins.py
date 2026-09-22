"""Fail-closed Codex native plugin snapshot tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from openjiuwen.harness_protocol import (
    HarnessContext,
    HarnessProtocolError,
    HostCapability,
)
from openjiuwen.harness_providers.codex import (
    CodexHarnessConfig,
    CodexNativePluginConfig,
    native_plugin_content_digest,
)
from openjiuwen.harness_providers.codex.native_plugins import (
    native_plugin_overrides,
    validate_native_plugin_inventory,
    validate_native_plugin_mcp_runtime,
    validate_native_plugin_packages,
)
from openjiuwen.harness_providers.codex.source_policy import (
    restricted_startup_overrides,
    validate_source_config,
    validate_startup_sources,
)


def _package(tmp_path: Path, *, hooks: bool = False) -> tuple[Path, Path]:
    codex_home = tmp_path / "codex"
    market = tmp_path / "market"
    root = codex_home / "plugins/cache/local-market/fixed/1.2.3"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "skills/marker").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "fixed",
                "version": "1.2.3",
                "skills": "./skills",
                "mcpServers": "./.mcp.json",
                "hooks": ["unsafe"] if hooks else [],
            }
        ),
        encoding="utf-8",
    )
    (root / "skills/marker/SKILL.md").write_text("---\nname: marker\n---\nUse marker.\n", encoding="utf-8")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"fixed_marker": {"command": "/prepared/mcp"}}}),
        encoding="utf-8",
    )
    market.mkdir()
    return root, market


def _snapshot(root: Path, market: Path, **overrides: Any) -> CodexNativePluginConfig:
    values: dict[str, Any] = {
        "plugin_id": "fixed@local-market",
        "source_type": "local",
        "source_locator": str(market),
        "version": "1.2.3",
        "content_sha256": native_plugin_content_digest(root),
        "mcp_server_names": ("fixed_marker",),
    }
    values.update(overrides)
    return CodexNativePluginConfig(**values)


def test_fixed_package_snapshot_and_mcp_namespace(tmp_path: Path) -> None:
    root, market = _package(tmp_path)
    plugin = _snapshot(root, market)
    env = {"CODEX_HOME": str(tmp_path / "codex")}
    fingerprint = validate_native_plugin_packages((plugin,), env=env)
    assert fingerprint is not None and len(fingerprint) == 64
    assert native_plugin_overrides((plugin,)) == ("features.plugins=true", "features.remote_plugin=false")
    with pytest.raises(HarnessProtocolError, match="host MCP"):
        validate_native_plugin_packages((plugin,), env=env, protocol_mcp_names=("fixed-marker",))
    (root / "skills/marker/SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(HarnessProtocolError, match="digest changed"):
        validate_native_plugin_packages((plugin,), env=env)


def test_c1_rejects_hooks_and_ambient_or_override_control(tmp_path: Path) -> None:
    root, market = _package(tmp_path, hooks=True)
    plugin = _snapshot(root, market)
    with pytest.raises(HarnessProtocolError, match="unsupported C2"):
        validate_native_plugin_packages((plugin,), env={"CODEX_HOME": str(tmp_path / "codex")})
    with pytest.raises(ValueError, match="inherit_process_env"):
        CodexHarnessConfig(native_plugins=(plugin,))
    with pytest.raises(ValueError, match="config_overrides"):
        CodexHarnessConfig(
            inherit_process_env=False,
            native_plugins=(plugin,),
            config_overrides=("features.plugins=false",),
        )
    assert native_plugin_overrides(()) == ("features.plugins=false", "features.remote_plugin=false")


def test_manifest_component_paths_cannot_escape_the_fixed_package(tmp_path: Path) -> None:
    root, market = _package(tmp_path)
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside", encoding="utf-8")
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    manifest["skills"] = "../outside"
    (root / ".codex-plugin/plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    plugin = _snapshot(root, market)
    with pytest.raises(HarnessProtocolError, match="escapes"):
        validate_native_plugin_packages((plugin,), env={"CODEX_HOME": str(tmp_path / "codex")})


def test_native_plugin_config_parses_as_provider_owned_snapshot(tmp_path: Path) -> None:
    root, market = _package(tmp_path)
    plugin = _snapshot(root, market)
    config = CodexHarnessConfig.from_mapping(
        {
            "inherit_process_env": False,
            "env": {"CODEX_HOME": str(tmp_path / "codex")},
            "native_plugins": [
                {
                    "plugin_id": plugin.plugin_id,
                    "source_type": plugin.source_type,
                    "source_locator": plugin.source_locator,
                    "version": plugin.version,
                    "content_sha256": plugin.content_sha256,
                    "enabled": plugin.enabled,
                    "required_components": list(plugin.required_components),
                    "mcp_server_names": list(plugin.mcp_server_names),
                }
            ],
        }
    )
    assert config.native_plugins == (plugin,)
    assert plugin.content_sha256 not in repr(config)  # provider config repr does not expose plugin snapshots


def test_restricted_startup_admits_only_the_managed_plugin_tree(tmp_path: Path) -> None:
    root, market = _package(tmp_path)
    plugin = _snapshot(root, market)
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    config = CodexHarnessConfig(
        inherit_process_env=False,
        env={"HOME": str(home), "CODEX_HOME": str(tmp_path / "codex")},
        startup_source_roots=(str(work), str(market), str(tmp_path / "codex/plugins")),
        native_plugins=(plugin,),
    )
    context = HarnessContext(
        agent_name="codex",
        agent_id="codex",
        host_session_id="session",
        cwd=str(work),
        system_prompt="fixture",
        host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}),
        interactions=object(),
    )
    assert validate_startup_sources(config, context)
    outside_source = CodexHarnessConfig(
        inherit_process_env=False,
        env={"HOME": str(home), "CODEX_HOME": str(tmp_path / "codex")},
        startup_source_roots=(str(work), str(tmp_path / "codex/plugins")),
        native_plugins=(plugin,),
    )
    with pytest.raises(HarnessProtocolError, match="outside authorized roots"):
        validate_startup_sources(outside_source, context)
    generated = restricted_startup_overrides(str(work), allow_native_plugins=True)
    assert "features.plugins=false" not in generated
    effective = {
        "default_permissions": "fixture",
        "plugins": {plugin.plugin_id: {"enabled": True}},
        **tomllib.loads("\n".join(generated)),
    }
    effective["features"]["plugins"] = True
    validate_source_config(effective, effective=True, cwd=str(work), allow_native_plugins=True)


class _Client:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self._client = self

    async def _ensure_initialized(self) -> None:
        self.calls.append("initialize")

    async def request(self, method: str, params: dict[str, Any], *, response_model: Any) -> Any:
        _ = params
        self.calls.append(method)
        return response_model.model_validate(self.responses[method])


@pytest.mark.asyncio
async def test_native_loader_inventory_and_required_mcp_are_confirmed(tmp_path: Path) -> None:
    root, market = _package(tmp_path)
    plugin = _snapshot(root, market)
    client = _Client(
        {
            "plugin/list": {
                "marketplaces": [
                    {
                        "path": str(market / ".agents/plugins/marketplace.json"),
                        "plugins": [
                            {
                                "id": plugin.plugin_id,
                                "installed": True,
                                "enabled": True,
                                "localVersion": plugin.version,
                                "source": {"type": "local", "path": str(market)},
                            }
                        ],
                    }
                ]
            },
            "plugin/read": {
                "plugin": {"hooks": [], "skills": [{"name": "fixed:marker"}], "mcpServers": ["fixed_marker"]}
            },
            "mcpServerStatus/list": {"data": [{"name": "fixed_marker", "tools": {"marker": {}}}]},
        }
    )
    await validate_native_plugin_inventory(client, (plugin,), cwd=str(tmp_path))
    await validate_native_plugin_mcp_runtime(client, (plugin,), thread_id="thread")
    assert client.calls == ["initialize", "plugin/list", "plugin/read", "mcpServerStatus/list"]


@pytest.mark.asyncio
async def test_native_loader_rejects_unapproved_enabled_plugin(tmp_path: Path) -> None:
    root, market = _package(tmp_path)
    plugin = _snapshot(root, market)
    client = _Client(
        {
            "plugin/list": {
                "marketplaces": [
                    {
                        "path": str(market / ".agents/plugins/marketplace.json"),
                        "plugins": [
                            {
                                "id": plugin.plugin_id,
                                "installed": True,
                                "enabled": True,
                                "localVersion": plugin.version,
                                "source": {"type": "local", "path": str(market)},
                            },
                            {"id": "ambient@other", "installed": True, "enabled": True},
                        ],
                    }
                ]
            }
        }
    )
    with pytest.raises(HarnessProtocolError, match="unapproved"):
        await validate_native_plugin_inventory(client, (plugin,), cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_disabled_native_plugin_may_be_omitted_by_the_loader(tmp_path: Path) -> None:
    root, market = _package(tmp_path)
    plugin = _snapshot(root, market, enabled=False)
    client = _Client({"plugin/list": {"marketplaces": []}})
    await validate_native_plugin_inventory(client, (plugin,), cwd=str(tmp_path))
    assert client.calls == ["initialize", "plugin/list"]
