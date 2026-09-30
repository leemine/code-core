"""Configured Provider capability inventory value objects."""

from dataclasses import FrozenInstanceError

import pytest

from openjiuwen.harness_protocol import (
    ProviderCapability,
    ProviderCapabilityInventory,
    ProviderCapabilityKind,
    RuntimeSurface,
)


def test_inventory_is_frozen_sorted_surface_filtered_and_stable() -> None:
    tool = ProviderCapability(
        "review_tool",
        ProviderCapabilityKind.TOOL,
        frozenset({RuntimeSurface.CODE}),
        "plugin@example",
    )
    category = ProviderCapability("filesystem", ProviderCapabilityKind.CATEGORY)
    inventory = ProviderCapabilityInventory("codex", (tool, category))

    assert inventory.capabilities == (category, tool)
    assert inventory.for_surface(RuntimeSurface.WORK) == (category,)
    assert inventory.for_surface(RuntimeSurface.CODE) == (category, tool)
    assert inventory.fingerprint == ProviderCapabilityInventory(
        "codex", (category, tool)
    ).fingerprint
    with pytest.raises(FrozenInstanceError):
        inventory.provider_id = "opencode"


def test_inventory_invalid_values_fail_closed() -> None:
    with pytest.raises(ValueError, match="name"):
        ProviderCapability("bad name", ProviderCapabilityKind.TOOL)
    with pytest.raises(ValueError, match="duplicate"):
        ProviderCapabilityInventory(
            "codex",
            (
                ProviderCapability("same", ProviderCapabilityKind.TOOL),
                ProviderCapability("SAME", ProviderCapabilityKind.TOOL),
            ),
        )
    with pytest.raises(ValueError, match="schema"):
        ProviderCapabilityInventory("codex", schema_version=2)
