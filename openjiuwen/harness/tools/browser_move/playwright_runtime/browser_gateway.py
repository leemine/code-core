# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral Browser tool host backed by the shared Browser runtime."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from openjiuwen.core.common.utils.schema_utils import SchemaUtils
from openjiuwen.harness_protocol import (
    ToolDefinition,
    ToolExecutionResult,
    ToolInvocation,
    json_value_to_builtin,
)

from .browser_tools import get_registered_client
from .identity import BrowserExecutionIdentity
from .runtime_tools import build_browser_runtime_tools

if TYPE_CHECKING:
    from .runtime import BrowserAgentRuntime

BrowserToolAdmission = Callable[
    [BrowserExecutionIdentity, ToolInvocation],
    bool | Awaitable[bool],
]


def _json_value(value: Any) -> Any:
    """Convert existing tool output containers to protocol JSON values."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _reference_values(value: Any) -> tuple[str, ...]:
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower() == "ref" and isinstance(nested, str):
                normalized = nested.strip()
                if normalized:
                    values.append(normalized)
            else:
                values.extend(_reference_values(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            values.extend(_reference_values(nested))
    return tuple(dict.fromkeys(values))


class BrowserExecutionToolGateway:
    """Expose one identity-bound Browser catalog without a nested worker model.

    The gateway is deliberately task scoped. It starts the existing Browser
    runtime lazily, exposes only the frozen allowlist plus deterministic core
    helpers, serializes all calls, and releases task resources without deleting
    the authorized login Profile.
    """

    def __init__(
        self,
        runtime: "BrowserAgentRuntime",
        *,
        admit: BrowserToolAdmission | None = None,
    ) -> None:
        identity = runtime.service.execution_identity
        if not isinstance(identity, BrowserExecutionIdentity):
            raise ValueError(
                "Browser tool gateway requires an identity-bound Browser runtime"
            )
        if runtime.service.file_roots is None:
            raise ValueError("Browser tool gateway requires explicit file roots")
        allowed = runtime.service.allowed_tool_names
        if allowed is None:
            raise ValueError("Browser tool gateway requires an explicit allowlist")
        self._runtime = runtime
        self._identity = identity
        self._allowed_tool_names = tuple(dict.fromkeys(allowed))
        self._admit = admit
        self._definitions: tuple[ToolDefinition, ...] | None = None
        self._tools: dict[str, Any] = {}
        self._catalog_lock = asyncio.Lock()
        self._invoke_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def execution_identity(self) -> BrowserExecutionIdentity:
        return self._identity

    @property
    def closed(self) -> bool:
        return self._closed

    async def definitions(self) -> tuple[ToolDefinition, ...]:
        if self._closed:
            raise RuntimeError("Browser tool gateway is closed")
        if self._definitions is not None:
            return self._definitions
        async with self._catalog_lock:
            if self._definitions is not None:
                return self._definitions
            await self._runtime.ensure_runtime_ready()
            server_id = str(
                getattr(self._runtime.service.mcp_cfg, "server_id", "") or ""
            ).strip()
            client = get_registered_client(server_id)
            if client is None:
                raise RuntimeError("Browser MCP client is unavailable")
            cards = await client.list_tools()
            card_by_name = {
                str(getattr(card, "name", "") or "").strip(): card
                for card in cards
                if str(getattr(card, "name", "") or "").strip()
            }
            missing = [
                name for name in self._allowed_tool_names if name not in card_by_name
            ]
            if missing:
                raise RuntimeError(
                    "Browser MCP is missing allowed tools: " + ", ".join(missing)
                )

            definitions: list[ToolDefinition] = []
            tools: dict[str, Any] = {}
            for name in self._allowed_tool_names:
                card = card_by_name[name]
                definitions.append(
                    ToolDefinition(
                        name=name,
                        description=str(getattr(card, "description", "") or ""),
                        input_schema=dict(getattr(card, "input_params", {}) or {}),
                    )
                )
                tools[name] = None

            for tool in build_browser_runtime_tools(self._runtime, language="en"):
                card = tool.card
                name = str(card.name or "").strip()
                if not name or name in tools:
                    raise RuntimeError(
                        f"duplicate Browser tool name in gateway catalog: {name}"
                    )
                definitions.append(
                    ToolDefinition(
                        name=name,
                        description=str(card.description or ""),
                        input_schema=dict(card.input_params or {}),
                    )
                )
                tools[name] = tool

            self._tools = tools
            self._definitions = tuple(definitions)
            return self._definitions

    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult:
        if self._closed:
            return ToolExecutionResult(
                content="Browser tool gateway is closed",
                is_error=True,
            )
        try:
            definitions = await self.definitions()
        except Exception as exc:  # noqa: BLE001 - do not leak catalog transport
            return ToolExecutionResult(
                content=f"Browser tool catalog failed: {type(exc).__name__}",
                is_error=True,
            )
        definition_by_name = {definition.name: definition for definition in definitions}
        definition = definition_by_name.get(invocation.name)
        if definition is None:
            return ToolExecutionResult(
                content=f"Unknown Browser tool: {invocation.name}",
                is_error=True,
            )
        if self._admit is not None:
            try:
                admitted = self._admit(self._identity, invocation)
                if inspect.isawaitable(admitted):
                    admitted = await admitted
            except Exception as exc:  # noqa: BLE001 - stable policy rejection
                return ToolExecutionResult(
                    content=f"Browser tool admission failed: {type(exc).__name__}",
                    is_error=True,
                )
            if admitted is not True:
                return ToolExecutionResult(
                    content=f"Browser tool is not allowed: {invocation.name}",
                    is_error=True,
                )

        arguments = json_value_to_builtin(invocation.arguments)
        if not isinstance(arguments, dict):
            return ToolExecutionResult(
                content="Browser tool arguments must be an object",
                is_error=True,
            )
        try:
            input_schema = json_value_to_builtin(definition.input_schema)
            if not isinstance(input_schema, dict):
                raise TypeError("Browser tool input schema must be an object")
            SchemaUtils.validate_with_schema(arguments, input_schema)
            self._validate_task_scope(
                arguments,
                definition=definition,
                helper=self._tools[invocation.name],
            )
            self._runtime.validate_reference_values(_reference_values(arguments))
        except Exception as exc:  # noqa: BLE001 - stable protocol rejection
            return ToolExecutionResult(
                content=f"Browser tool validation failed: {type(exc).__name__}",
                is_error=True,
            )

        async with self._invoke_lock:
            if self._closed:
                return ToolExecutionResult(
                    content="Browser tool gateway is closed",
                    is_error=True,
                )
            try:
                helper = self._tools[invocation.name]
                if helper is None:
                    tool = await self._runtime._get_playwright_mcp_tool(
                        invocation.name
                    )
                    raw_result = await tool.invoke(arguments)
                else:
                    raw_result = await helper.invoke(arguments)
                from .runtime import BrowserRuntimeRail

                result = BrowserRuntimeRail._normalize_tool_result(raw_result)
                outcome = self._runtime.classify_tool_result(result)
                if outcome["success"]:
                    self._runtime.record_tool_reference_state(
                        tool_name=invocation.name,
                        tool_args=arguments,
                        tool_result=result,
                    )
                if isinstance(result, Mapping):
                    content = dict(result)
                else:
                    content = {"result": result}
                content.setdefault("page_state", self._runtime.export_page_state())
                content.setdefault("task_id", self._identity.task.task_id)
                content.setdefault("request_id", self._identity.task.request_id)
                return ToolExecutionResult(
                    content=_json_value(content),
                    is_error=not bool(outcome["success"]),
                )
            except Exception as exc:  # noqa: BLE001 - do not leak transport detail
                return ToolExecutionResult(
                    content=f"Browser tool execution failed: {type(exc).__name__}",
                    is_error=True,
                )

    def _validate_task_scope(
        self,
        arguments: dict[str, Any],
        *,
        definition: ToolDefinition,
        helper: Any,
    ) -> None:
        properties = definition.input_schema.get("properties", {})
        properties = properties if isinstance(properties, Mapping) else {}
        expected_request_id = self._identity.task.request_id
        supplied_request_id = str(arguments.get("request_id") or "").strip()
        if supplied_request_id and supplied_request_id != expected_request_id:
            raise ValueError("Browser tool request_id does not match task identity")
        if "request_id" in properties:
            arguments["request_id"] = expected_request_id

        expected_session_id = self._identity.task.task_id
        supplied_session_id = str(arguments.get("session_id") or "").strip()
        if supplied_session_id and supplied_session_id != expected_session_id:
            raise ValueError("Browser tool session_id does not match task identity")
        if "session_id" in properties:
            arguments["session_id"] = expected_session_id
        target_id = str(arguments.get("target_id") or "").strip()
        if helper is None and target_id.startswith("t_g"):
            raise ValueError(
                "PageState target_id requires a deterministic Browser helper"
            )

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            async with self._catalog_lock:
                async with self._invoke_lock:
                    await self._runtime.release_task_resources()
                    self._closed = True


__all__ = [
    "BrowserExecutionToolGateway",
    "BrowserToolAdmission",
]
