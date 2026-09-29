#!/usr/bin/env python
# coding: utf-8
"""Tests for process-level browser service lifecycle ownership."""
# pylint: disable=protected-access

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserInstanceConfig,
    BrowserRunGuardrails,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import (
    _ACTIVE_BROWSER_RUNTIMES,
    BrowserAgentRuntime,
    reset_active_browser_runtimes,
    reset_managed_browser_runtime,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.service import BrowserService
from openjiuwen.harness.tools.browser_move.playwright_runtime.service_registry import (
    BROWSER_SERVICE_REGISTRY,
    BrowserServiceIdentity,
    BrowserServiceRegistry,
)


def _make_service(
    *,
    key: str = "shared",
    profile_name: str = "",
    runtime_cwd: str | None = None,
) -> BrowserService:
    mcp_cfg = McpServerConfig(
        server_id=f"test-playwright-{key}",
        server_name=f"test-playwright-{key}",
        server_path="stdio://playwright",
        client_type="stdio",
        params={"cwd": runtime_cwd or str(Path.cwd())},
    )
    return BrowserService(
        provider="openai",
        api_key="test-key",
        api_base="https://example.invalid/v1",
        model_name="test-model",
        mcp_cfg=mcp_cfg,
        guardrails=BrowserRunGuardrails(
            max_steps=3,
            max_failures=1,
            timeout_s=30,
            retry_once=False,
        ),
        instance=BrowserInstanceConfig(
            key=key,
            driver_mode="managed",
            profile_name=profile_name,
        ),
    )


@pytest.fixture(autouse=True)
def _clear_process_registry():
    BROWSER_SERVICE_REGISTRY.clear()
    _ACTIVE_BROWSER_RUNTIMES.clear()
    yield
    BROWSER_SERVICE_REGISTRY.clear()
    _ACTIVE_BROWSER_RUNTIMES.clear()


def test_registry_assigns_one_heartbeat_owner_and_transfers_it() -> None:
    registry = BrowserServiceRegistry()
    identity = ("browser", "profile", "headed")
    first = MagicMock()
    second = MagicMock()

    registry.acquire(identity, first)
    registry.acquire(identity, second)
    assert registry.activate_binding(identity, first) is True
    assert registry.activate_binding(identity, second) is False
    assert registry.is_heartbeat_owner(identity, first) is True

    first_release = registry.release(identity, first)
    assert first_release.close_mcp_binding is False
    assert first_release.next_heartbeat_owner is second
    assert registry.is_heartbeat_owner(identity, second) is True

    second_release = registry.release(identity, second)
    assert second_release.close_mcp_binding is True
    assert second_release.next_heartbeat_owner is None


def _identity(*, key: str, user_data_dir: str, driver_mode: str = "managed"):
    return BrowserServiceIdentity(
        browser_key=key,
        profile_name=f"profile-{key}",
        driver_mode=driver_mode,
        display_mode="headed" if driver_mode == "managed" else driver_mode,
        managed_args=(),
        browser_binary="/usr/bin/google-chrome",
        user_data_dir=user_data_dir,
        cdp_endpoint="",
        server_id=f"playwright-{key}",
    )


def test_registry_rejects_distinct_managed_identities_for_same_user_data_dir() -> None:
    registry = BrowserServiceRegistry()
    first = MagicMock()
    second = MagicMock()
    first_identity = _identity(key="first", user_data_dir="/profiles/shared")
    second_identity = _identity(key="second", user_data_dir="/profiles/shared")
    registry.acquire(first_identity, first)

    with pytest.raises(ValueError, match="already owned"):
        registry.acquire(second_identity, second)

    assert second_identity not in registry._entries


def test_registry_allows_same_managed_identity_to_share_user_data_dir() -> None:
    registry = BrowserServiceRegistry()
    identity = _identity(key="shared", user_data_dir="/profiles/shared")
    first = MagicMock()
    second = MagicMock()

    registry.acquire(identity, first)
    registry.acquire(identity, second)

    assert identity in registry._entries


def test_registry_releases_user_data_dir_after_last_non_driver_owner() -> None:
    registry = BrowserServiceRegistry()
    first = MagicMock()
    first_identity = _identity(key="first", user_data_dir="/profiles/shared")
    second_identity = _identity(key="second", user_data_dir="/profiles/shared")
    registry.acquire(first_identity, first)
    registry.release(first_identity, first)

    registry.acquire(second_identity, MagicMock())

    assert first_identity not in registry._entries
    assert second_identity in registry._entries


def test_registry_preserved_driver_keeps_user_data_dir_ownership() -> None:
    registry = BrowserServiceRegistry()
    first = MagicMock()
    first_identity = _identity(key="first", user_data_dir="/profiles/shared")
    second_identity = _identity(key="second", user_data_dir="/profiles/shared")
    driver = MagicMock(owns_process=True)
    registry.acquire(first_identity, first)
    registry.register_managed_driver(first_identity, first, driver)
    registry.release(first_identity, first)

    with pytest.raises(ValueError, match="already owned"):
        registry.acquire(second_identity, MagicMock())


@pytest.mark.parametrize("operation", ["activate_binding", "register_managed_driver"])
def test_registry_rejects_managed_profile_conflict_at_every_creation_entry(
    operation: str,
) -> None:
    registry = BrowserServiceRegistry()
    first_identity = _identity(key="first", user_data_dir="/profiles/shared")
    second_identity = _identity(key="second", user_data_dir="/profiles/shared")
    first = MagicMock()
    registry.acquire(first_identity, first)

    with pytest.raises(ValueError, match="already owned"):
        if operation == "activate_binding":
            registry.activate_binding(second_identity, MagicMock())
        else:
            registry.register_managed_driver(
                second_identity,
                MagicMock(),
                MagicMock(owns_process=True),
            )

    assert second_identity not in registry._entries


def test_registry_does_not_claim_remote_user_data_dir() -> None:
    registry = BrowserServiceRegistry()
    first = MagicMock()
    second = MagicMock()
    first_identity = _identity(
        key="first",
        user_data_dir="profile:shared",
        driver_mode="remote",
    )
    second_identity = _identity(
        key="second",
        user_data_dir="profile:shared",
        driver_mode="remote",
    )

    registry.acquire(first_identity, first)
    registry.acquire(second_identity, second)

    assert first_identity in registry._entries
    assert second_identity in registry._entries


def test_registry_blocks_acquire_during_and_after_failed_reset() -> None:
    registry = BrowserServiceRegistry()
    identity = _identity(key="blocked", user_data_dir="/profiles/blocked")
    owner = MagicMock()
    registry.acquire(identity, owner)

    registry.begin_reset(identity)
    with pytest.raises(RuntimeError, match="already in progress"):
        registry.begin_reset(identity)
    with pytest.raises(RuntimeError, match="reset_in_progress"):
        registry.acquire(identity, MagicMock())

    registry.restore_failed_reset(identity, (), "cleanup_failed:OSError")
    with pytest.raises(RuntimeError, match="cleanup_failed"):
        registry.acquire(identity, MagicMock())

    registry.begin_reset(identity)
    registry.complete_reset(identity)
    registry.acquire(identity, MagicMock())


def test_service_registry_claims_effective_managed_user_data_dir(tmp_path) -> None:
    first = _make_service(
        key="first",
        profile_name="shared-profile",
        runtime_cwd=str(tmp_path),
    )
    second = _make_service(
        key="second",
        profile_name="shared-profile",
        runtime_cwd=str(tmp_path),
    )
    expected = str(tmp_path / ".browser-profiles" / "shared-profile")

    assert first.lifecycle_identity.user_data_dir == expected
    first.acquire_task_binding()
    with pytest.raises(ValueError, match="already owned"):
        second.acquire_task_binding()
    assert second._task_binding_ref_count == 0


@pytest.mark.asyncio
async def test_release_last_task_binding_preserves_managed_chrome() -> None:
    service = _make_service()
    service.acquire_task_binding()
    assert BROWSER_SERVICE_REGISTRY.activate_binding(
        service.lifecycle_identity,
        service,
    ) is True
    service.started = True
    service._browser_agent = MagicMock()
    service._heartbeat_task = None
    driver = MagicMock()
    service._managed_driver = driver
    BROWSER_SERVICE_REGISTRY.register_managed_driver(
        service.lifecycle_identity,
        service,
        driver,
    )

    with patch.object(
        service,
        "_remove_registered_mcp_server",
        AsyncMock(),
    ) as remove_binding:
        await service.release_task_binding()

    remove_binding.assert_awaited_once()
    driver.stop.assert_not_called()
    assert service._managed_driver is driver
    assert service._browser_agent is None
    assert service._heartbeat_task is None
    assert service.started is False


@pytest.mark.asyncio
async def test_explicit_reset_stops_browser_preserved_after_task_release() -> None:
    runtime = object.__new__(BrowserAgentRuntime)
    runtime._service = _make_service()
    runtime._page_generation = 0
    runtime._last_observed_url = "https://example.com"
    runtime._service.acquire_task_binding()
    runtime._service.started = True
    runtime._service._heartbeat_task = None
    driver = MagicMock()
    runtime._service._managed_driver = driver
    _ACTIVE_BROWSER_RUNTIMES.add(runtime)

    with patch.object(
        runtime._service,
        "_remove_registered_mcp_server",
        AsyncMock(),
    ):
        await runtime.release_task_resources()
        reset_count = await reset_active_browser_runtimes()

    assert reset_count == 1
    driver.stop.assert_called_once()
    assert runtime._service._managed_driver is None


@pytest.mark.asyncio
async def test_identity_reset_stops_only_matching_idle_managed_browser(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("BROWSER_PROFILE_NAME", "jiuwenclaw")
    monkeypatch.delenv("BROWSER_MANAGED_ARGS", raising=False)
    monkeypatch.setenv(
        "BROWSER_MANAGED_USER_DATA_DIR",
        str(tmp_path / "headed-profile"),
    )
    headed = _make_service(key="")
    headed.acquire_task_binding()
    headed.started = True
    headed._heartbeat_task = None
    headed_driver = MagicMock()
    headed_driver.owns_process = True
    headed._managed_driver = headed_driver
    BROWSER_SERVICE_REGISTRY.register_managed_driver(
        headed.lifecycle_identity,
        headed,
        headed_driver,
    )
    await headed.release_task_binding()

    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "--headless=new")
    monkeypatch.setenv(
        "BROWSER_MANAGED_USER_DATA_DIR",
        str(tmp_path / "headless-profile"),
    )
    headless = _make_service(key="")
    headless.acquire_task_binding()
    headless.started = True
    headless._heartbeat_task = None
    headless_driver = MagicMock()
    headless_driver.owns_process = True
    headless._managed_driver = headless_driver
    BROWSER_SERVICE_REGISTRY.register_managed_driver(
        headless.lifecycle_identity,
        headless,
        headless_driver,
    )
    await headless.release_task_binding()

    reset_count = await reset_managed_browser_runtime(
        browser_key="",
        profile_name="jiuwenclaw",
        display_mode="headed",
        browser_binary="",
    )

    assert reset_count == 1
    headed_driver.stop.assert_called_once()
    headless_driver.stop.assert_not_called()


@pytest.mark.asyncio
async def test_reused_service_waits_for_last_concurrent_task_reference() -> None:
    service = _make_service()
    service.acquire_task_binding()
    service.acquire_task_binding()
    BROWSER_SERVICE_REGISTRY.activate_binding(
        service.lifecycle_identity,
        service,
    )
    service.started = True
    service._browser_agent = MagicMock()

    with patch.object(
        service,
        "_remove_registered_mcp_server",
        AsyncMock(),
    ) as remove_binding:
        await service.release_task_binding()
        remove_binding.assert_not_awaited()
        assert service.started is True
        assert service._browser_agent is not None

        await service.release_task_binding()
        remove_binding.assert_awaited_once()
        assert service.started is False
        assert service._browser_agent is None


@pytest.mark.asyncio
async def test_releasing_heartbeat_owner_keeps_shared_binding_alive() -> None:
    first = _make_service()
    second = _make_service()
    for service in (first, second):
        service.acquire_task_binding()
        service.started = True
        BROWSER_SERVICE_REGISTRY.activate_binding(
            service.lifecycle_identity,
            service,
        )

    first._heartbeat_task = None
    second._heartbeat_task = None
    with patch.object(
        first,
        "_remove_registered_mcp_server",
        AsyncMock(),
    ) as first_remove, patch.object(
        second,
        "_start_heartbeat",
        MagicMock(),
    ) as second_start:
        await first.release_task_binding()

    first_remove.assert_not_awaited()
    second_start.assert_called_once()
    assert BROWSER_SERVICE_REGISTRY.is_heartbeat_owner(
        second.lifecycle_identity,
        second,
    ) is True

    with patch.object(
        second,
        "_remove_registered_mcp_server",
        AsyncMock(),
    ) as second_remove:
        await second.release_task_binding()
    second_remove.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_stops_only_matching_browser_identity() -> None:
    first = _make_service(key="first")
    second = _make_service(key="second")
    first_driver = MagicMock()
    second_driver = MagicMock()
    for service, driver in ((first, first_driver), (second, second_driver)):
        service.acquire_task_binding()
        service.started = True
        service._managed_driver = driver
        BROWSER_SERVICE_REGISTRY.activate_binding(
            service.lifecycle_identity,
            service,
        )
        BROWSER_SERVICE_REGISTRY.register_managed_driver(
            service.lifecycle_identity,
            service,
            driver,
        )

    with patch.object(first, "_remove_registered_mcp_server", AsyncMock()):
        await first.reset()

    first_driver.stop.assert_called_once()
    second_driver.stop.assert_not_called()
    assert first._managed_driver is None
    assert second._managed_driver is second_driver


def test_display_mode_is_part_of_lifecycle_identity(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "--headless=new")
    headless = _make_service()
    monkeypatch.setenv("BROWSER_MANAGED_ARGS", "")
    headed = _make_service()

    assert headless.lifecycle_identity != headed.lifecycle_identity
    assert headless.lifecycle_identity.display_mode == "headless"
    assert headed.lifecycle_identity.display_mode == "headed"

@pytest.mark.asyncio
async def test_application_shutdown_includes_idle_managed_driver() -> None:
    from openjiuwen.harness.tools.browser_move import shutdown_managed_browser_runtimes
    service = _make_service()
    driver = MagicMock()
    driver.owns_process = True
    driver.stop_gracefully = AsyncMock()
    BROWSER_SERVICE_REGISTRY.register_managed_driver(service.lifecycle_identity, service, driver)
    # The last Agent may already have been destroyed; only the retained driver
    # registry can close its warm Chrome at application shutdown.
    BROWSER_SERVICE_REGISTRY.release(service.lifecycle_identity, service)
    assert not _ACTIVE_BROWSER_RUNTIMES
    await shutdown_managed_browser_runtimes()
    driver.stop_gracefully.assert_awaited_once()
    assert BROWSER_SERVICE_REGISTRY.managed_driver_identities() == ()
