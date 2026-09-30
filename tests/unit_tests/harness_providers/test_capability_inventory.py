"""Provider-private configuration projected into the neutral inventory."""

from pathlib import Path

import pytest

from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    ProviderCapabilityKind,
    RuntimeSurface,
)
from openjiuwen.harness_providers.construction import configured_provider_capabilities


def _skill(root: Path, name: str) -> Path:
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\ninstructions\n",
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
def test_external_inventory_contains_builtins_and_configured_skill(
    tmp_path: Path, provider_id: str
) -> None:
    skill = _skill(tmp_path / "skill", "surface-helper")
    spec = AgentExecutionSpec(
        provider_id,
        "r1",
        provider_config={"skills": [{"dir": str(skill)}]},
    )

    inventory = configured_provider_capabilities(spec)
    keys = {(entry.kind, entry.name) for entry in inventory.capabilities}

    assert (ProviderCapabilityKind.CATEGORY, "filesystem") in keys
    assert (ProviderCapabilityKind.CATEGORY, "terminal") in keys
    assert (ProviderCapabilityKind.SKILL, "surface-helper") in keys
    assert "terminal" not in {
        entry.name for entry in inventory.for_surface(RuntimeSurface.WORK)
    }
    assert "surface-helper" in {
        entry.name for entry in inventory.for_surface(RuntimeSurface.WORK)
    }


def test_opencode_native_tool_collision_fails_before_runtime_allocation(
    tmp_path: Path,
) -> None:
    plugin = {
        "plugin_id": "one",
        "source_type": "local",
        "source_locator": str(tmp_path),
        "version": "v1",
        "content_sha256": "0" * 64,
        "entrypoint": "index.js",
        "export_name": "Plugin",
        "required_hooks": ["event"],
        "required_tools": ["duplicate"],
    }
    spec = AgentExecutionSpec(
        "opencode",
        "r1",
        provider_config={"native_plugins": [plugin, {**plugin, "plugin_id": "two"}]},
    )

    with pytest.raises(ValueError, match="duplicate provider capability"):
        configured_provider_capabilities(spec)
