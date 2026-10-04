# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-private, per-Turn pre-I/O authority for the pinned native CLI.

The host authenticates the HTTP request before calling the Harness method.
This module owns neither a listener nor a second execution state machine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from openjiuwen.harness_protocol import BeforeToolContext, McpTransport

from ..jsonsafe import to_json_safe
from .model_gateway import OpenCodeModelGateway
from .native_plugins import (
    OpenCodeNativePluginConfig,
    opencode_plugin_content_digest,
    stage_native_plugins,
    validate_native_plugin_packages,
)
from .options import native_config

_MAX_CALLS = 256
_MAX_GENERATION_CALLS = 4096
_MAX_BYTES = 256 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NONCE = re.compile(r"[a-f0-9-]{32,64}")
_PRODUCT_SERVER = "jiuwenswarm_product_tools"
PRODUCT_TICKET_FIELD = "__openjiuwen_product_ticket"
_TICKET = re.compile(r"pt_[a-f0-9]{64}")
_FIELDS = {
    "read": ({"filePath"}, {"filePath", "offset", "limit"}),
    "write": ({"filePath", "content"}, {"filePath", "content"}),
    "edit": ({"filePath", "oldString", "newString"}, {"filePath", "oldString", "newString", "replaceAll"}),
    "bash": ({"command", "description"}, {"command", "description", "timeout", "workdir"}),
}


@dataclass(frozen=True, slots=True, repr=False)
class OpenCodePreflightEndpoint:
    """Host-owned authenticated loopback endpoint, never JSON provider config."""

    url: str
    token: str = field(repr=False)
    generation: str
    product_tool_names: tuple[str, ...] = ()
    model_gateway: OpenCodeModelGateway | None = field(default=None, kw_only=True, repr=False)

    def __post_init__(self):
        if not isinstance(self.url, str):
            raise ValueError("invalid OpenCode preflight endpoint")
        url = urlsplit(self.url)
        try:
            port = url.port
        except ValueError:
            port = None
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or port is None
            or not 0 < port <= 65535
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path.startswith("/")
            or any(ord(c) < 33 for c in self.url)
        ):
            raise ValueError("invalid OpenCode preflight endpoint")
        if (
            not isinstance(self.token, str)
            or not 32 <= len(self.token) <= 256
            or not re.fullmatch(r"[A-Za-z0-9_-]+", self.token)
        ):
            raise ValueError("invalid OpenCode preflight authentication")
        if not isinstance(self.generation, str) or not _ID.fullmatch(self.generation):
            raise ValueError("invalid OpenCode preflight generation")
        names = self.product_tool_names
        if (
            not isinstance(names, (list, tuple))
            or len(names) > 128
            or any(not isinstance(name, str) or not _ID.fullmatch(name) for name in names)
            or len(set(names)) != len(names)
        ):
            raise ValueError("invalid OpenCode preflight product inventory")
        object.__setattr__(self, "product_tool_names", tuple(names))

        gateway = self.model_gateway
        if gateway is not None and (
            not isinstance(gateway, OpenCodeModelGateway)
            or urlsplit(gateway.url).netloc != url.netloc
            or gateway.generation != self.generation
        ):
            raise ValueError("model gateway must belong to the original host transport")

    def product_name(self, name):
        return name in {_PRODUCT_SERVER + "_" + tool for tool in self.product_tool_names}

    def admits_servers(self, servers):
        if not servers:
            return not self.product_tool_names
        if len(servers) != 1:
            return False
        server = servers[0]
        if server.name != _PRODUCT_SERVER or server.transport is not McpTransport.HTTP or not server.url:
            return False
        actual, expected = urlsplit(server.url), urlsplit(self.url)
        return (
            actual.scheme == expected.scheme
            and actual.netloc == expected.netloc
            and actual.path == "/mcp"
            and not actual.query
            and not actual.fragment
            and dict(server.headers) == {"Authorization": "Bearer " + self.token}
        )


def _arguments(tool, value):
    if tool not in _FIELDS or not isinstance(value, dict):
        raise ValueError
    required, allowed = _FIELDS[tool]
    if not required <= value.keys() or value.keys() - allowed:
        raise ValueError
    if len(json.dumps(value, allow_nan=False).encode()) > _MAX_BYTES:
        raise ValueError
    for key, item in value.items():
        if key in {"offset", "limit", "timeout"}:
            if not isinstance(item, int) or isinstance(item, bool) or not 0 < item <= 2**31 - 1:
                raise ValueError
        elif key == "replaceAll":
            if not isinstance(item, bool):
                raise ValueError
        elif not isinstance(item, str) or "\x00" in item:
            raise ValueError
        if key in {"filePath", "workdir"}:
            if not os.path.isabs(item) or item != os.path.normpath(item) or item.startswith("//"):
                raise ValueError
    return dict(value)


def _product_arguments(value):
    remaining = 8192

    def validate(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError
        if item is None or isinstance(item, (str, bool)):
            return
        if isinstance(item, (int, float)):
            if isinstance(item, int) and abs(item) > 2**53 - 1:
                raise ValueError
            return
        if isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key in {"__proto__", "prototype", "constructor"}:
                    raise ValueError
                validate(child, depth + 1)
            return
        raise ValueError

    if not isinstance(value, dict):
        raise ValueError
    validate(value, 0)
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded.encode()) > _MAX_BYTES:
        raise ValueError
    return json.loads(encoded)


@dataclass(slots=True)
class _Record:
    turn: object
    context: object
    callback: object
    request: BeforeToolContext
    session: str
    generation: str
    root_message: str
    transport: object
    native_message: str | None = None
    product: bool = False
    authorized: bool = False
    consumed: bool = False
    permission_allowed: bool = False
    product_consumed: bool = False


class PreflightGate:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.turn = None
        self.root_message = None
        self.model_origin = None
        self.records = {}
        self.tickets = {}
        self.nonces = set()
        self.calls = set()
        self.closed = False

    def begin(self, turn, root_message):
        self.clear()
        self.turn = turn
        self.root_message = root_message
        self.model_origin = object()

    def clear(self):
        self.turn = None
        self.root_message = None
        self.model_origin = None
        self.records.clear()
        self.tickets.clear()

    def close(self):
        self.clear()
        self.nonces.clear()
        self.calls.clear()
        self.closed = True

    def current(self, harness, record):
        return (
            not self.closed
            and self.turn is record.turn
            and harness.active_turn is record.turn
            and self.root_message == record.root_message
            and isinstance(record.root_message, str)
            and _ID.fullmatch(record.root_message) is not None
            and harness._transport is record.transport
            and record.transport is not None
            and harness.context is record.context
            and harness.provider_session_id == record.session
            and self.endpoint.generation == record.generation
            and self.records.get(record.request.call_id) is record
            and not record.turn.abort_requested
            and not record.turn.stop_requested
            and not harness._stopping
            and not harness._poisoned
        )

    async def prove_native_call(self, harness, record):
        """Query native persistence independently of the single SSE consumer.

        A first-delivery request may belong to an already cancelled Turn. Never
        infer its root from arrival time or assign it the new Turn's authority.
        """
        if not self.current(harness, record):
            return False
        try:
            async with asyncio.timeout(harness._config.request_timeout_s):
                messages = await record.transport.request("GET", f"/session/{record.session}/message")
            if not self.current(harness, record) or not isinstance(messages, list):
                return False
            roots, matches = [], []
            for message in messages:
                if not isinstance(message, dict):
                    return False
                info, parts = message.get("info"), message.get("parts")
                if not isinstance(info, dict) or not isinstance(parts, list):
                    return False
                if info.get("id") == record.root_message:
                    roots.append(info)
                for part in parts:
                    if not isinstance(part, dict):
                        return False
                    if part.get("callID") == record.request.call_id:
                        matches.append((info, part))
            if len(roots) != 1 or len(matches) != 1:
                return False
            root = roots[0]
            info, part = matches[0]
            state = part.get("state")
            message_id = info.get("id")
            if (
                root.get("role") != "user"
                or root.get("sessionID") != record.session
                or info.get("role") != "assistant"
                or info.get("sessionID") != record.session
                or info.get("parentID") != record.root_message
                or not isinstance(message_id, str)
                or not _ID.fullmatch(message_id)
                or part.get("messageID") != message_id
                or part.get("sessionID") != record.session
                or part.get("type") != "tool"
                or part.get("tool") != record.request.tool_name
                or not isinstance(state, dict)
                or state.get("status") != "running"
            ):
                return False
            native_args = (
                _product_arguments(state.get("input"))
                if record.product
                else _arguments(record.request.tool_name, state.get("input"))
            )
            # Canonical JSON distinguishes bool from int; dict equality does not.
            if json.dumps(native_args, sort_keys=True, allow_nan=False) != json.dumps(
                to_json_safe(record.request.arguments), sort_keys=True, allow_nan=False
            ):
                return False
            record.native_message = message_id
            return self.current(harness, record)
        except Exception:
            return False

    async def check(self, harness, record):
        if not self.current(harness, record):
            return False
        if record.product:
            # Scope admission only: the authenticated product ToolGateway owns final resource authorization.
            return True
        try:
            async with asyncio.timeout(harness._config.request_timeout_s):
                allowed = await record.callback(record.request) is True
        except Exception:
            return False
        return allowed and self.current(harness, record)

    async def authorize(self, harness, payload):
        nonce = payload.get("nonce") if isinstance(payload, dict) else None
        result = {"allowed": False, "nonce": nonce if isinstance(nonce, str) and len(nonce) <= 64 else ""}
        try:
            if not isinstance(payload, dict) or set(payload) != {
                "version",
                "generation",
                "nonce",
                "session_id",
                "call_id",
                "tool",
                "args",
            }:
                return result
            if type(payload["version"]) is not int or payload["version"] != 1:
                return result
            if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
                return result
            call_id, session, tool = payload["call_id"], payload["session_id"], payload["tool"]
            if (
                not isinstance(call_id, str)
                or not _ID.fullmatch(call_id)
                or not isinstance(session, str)
                or not _ID.fullmatch(session)
                or not isinstance(tool, str)
                or payload["generation"] != self.endpoint.generation
                or call_id in self.calls
                or nonce in self.nonces
                or len(self.records) >= _MAX_CALLS
                or len(self.calls) >= _MAX_GENERATION_CALLS
                or self.closed
            ):
                return result
            product = self.endpoint.product_name(tool)
            args = _product_arguments(payload["args"]) if product else _arguments(tool, payload["args"])
            if product and PRODUCT_TICKET_FIELD in args:
                return result
            turn, context = harness.active_turn, harness.context
            if turn is None or context is None or context.tool_authorizer is None:
                return result
            if not self.endpoint.admits_servers(context.mcp_servers):
                return result
            permissions = native_config(
                harness._config,
                context.host_capabilities,
                runtime_policy=context.runtime_policy,
                governed=True,
            )["permission"]
            permission = "edit" if tool in {"write", "edit"} else tool
            if not product and permissions.get(permission, permissions.get("*")) != "ask":
                return result
            request = BeforeToolContext(context.agent_name, session, turn.turn_id, call_id, tool, args)
            record = _Record(
                turn,
                context,
                context.tool_authorizer,
                request,
                session,
                self.endpoint.generation,
                self.root_message,
                harness._transport,
                product=product,
            )
            # Reserve before any await, including denials: duplicates never re-enter authority.
            self.records[call_id] = record
            self.nonces.add(nonce)
            self.calls.add(call_id)
            if not self.current(harness, record):
                return result
            if not await self.prove_native_call(harness, record):
                return result
            record.authorized = await self.check(harness, record)
            result["allowed"] = record.authorized
            if product and record.authorized:
                ticket = "pt_" + secrets.token_hex(32)
                self.tickets[ticket] = record
                result["ticket"] = ticket
            return result
        except (TypeError, ValueError, KeyError, OverflowError):
            return result

    def consume_product(self, harness, tool_name, arguments):
        """Consume a transport-only ticket, never infer identity from arguments."""
        if not isinstance(arguments, dict):
            return None
        ticket = arguments.get(PRODUCT_TICKET_FIELD)
        if not isinstance(ticket, str) or not _TICKET.fullmatch(ticket):
            return None
        record = self.tickets.pop(ticket, None)
        if (
            record is None
            or not record.product
            or record.product_consumed
            or not record.authorized
            or not record.permission_allowed
            or not record.consumed
            or not self.current(harness, record)
            or not isinstance(tool_name, str)
            or tool_name not in self.endpoint.product_tool_names
            or record.request.tool_name != _PRODUCT_SERVER + "_" + tool_name
        ):
            return None
        try:
            clean = _product_arguments({key: value for key, value in arguments.items() if key != PRODUCT_TICKET_FIELD})
            if json.dumps(clean, sort_keys=True, allow_nan=False) != json.dumps(
                to_json_safe(record.request.arguments), sort_keys=True, allow_nan=False
            ):
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        record.product_consumed = True
        return record.request

    def product_current(self, harness, operation):
        if not isinstance(operation, BeforeToolContext):
            return False
        record = self.records.get(operation.call_id)
        return (
            record is not None
            and record.request is operation
            and record.product
            and record.product_consumed
            and record.consumed
            and record.permission_allowed
            and record.authorized
            and self.current(harness, record)
        )

    def claim(self, harness, call_id, props):
        record = self.records.get(call_id)
        if record is None or record.consumed or not record.authorized or not self.current(harness, record):
            return None
        record.consumed = True
        tool, args = record.request.tool_name, record.request.arguments
        expected = "edit" if tool in {"write", "edit"} else tool
        if props.get("sessionID") != record.session or props.get("permission") != expected:
            return None
        native_tool = props.get("tool")
        if not isinstance(native_tool, dict) or native_tool.get("messageID") != record.native_message:
            return None
        metadata = props.get("metadata")
        if not isinstance(metadata, dict):
            return None
        if tool in {"write", "edit"} and metadata.get("filepath") != args["filePath"]:
            return None
        if tool == "bash" and metadata.get("command") != args["command"]:
            return None
        return record


# No plugin dependency import or file reads in the hook. The original args object
# is frozen: assigning output.args would not replace the CLI's actual argument.
_GATE_JS = r"""
export const Preflight = async () => ({
  "chat.headers": async (input, output) => {
    const cfg = __ENDPOINT__
    if (!cfg.model) return
    const fail = () => { throw new Error("mandatory model source denied") }
    const message = input.message
    if (!message || message.role !== "user" || message.sessionID !== input.sessionID ||
        input.agent !== "build" || !/^[A-Za-z0-9_-]{1,128}$/.test(input.sessionID) ||
        !/^[A-Za-z0-9_-]{1,128}$/.test(message.id) ||
        input.model?.id !== cfg.model || input.model?.providerID !== cfg.provider ||
        message.model?.modelID !== cfg.model || message.model?.providerID !== cfg.provider ||
        message.agent !== "build" || !output.headers ||
        Object.getPrototypeOf(output.headers) !== Object.prototype) fail()
    for (const key of Reflect.ownKeys(output.headers)) {
      if (typeof key !== "string" || key.toLowerCase().startsWith("x-openjiuwen-")) fail()
    }
    const source = {
      "x-openjiuwen-session": input.sessionID, "x-openjiuwen-root": message.id,
      "x-openjiuwen-generation": cfg.generation, "x-openjiuwen-agent": input.agent,
      "x-openjiuwen-model": cfg.model, "x-openjiuwen-provider": cfg.provider,
    }
    for (const [key, value] of Object.entries(source)) {
      Object.defineProperty(output.headers, key, {value, enumerable:true, writable:false, configurable:false})
    }
    Object.freeze(output.headers)
    Object.freeze(output)
  },
  "tool.execute.before": async (input, output) => {
    const fail = () => { throw new Error("mandatory native preflight denied") }
    const cfg = __ENDPOINT__
    const specs = {
      read: [["filePath"], ["filePath", "offset", "limit"]],
      write: [["filePath", "content"], ["filePath", "content"]],
      edit: [["filePath", "oldString", "newString"], ["filePath", "oldString", "newString", "replaceAll"]],
      bash: [["command", "description"], ["command", "description", "timeout", "workdir"]],
    }
    const tool = input.tool, session = input.sessionID, call = input.callID
    const product = cfg.product_tools.includes(tool)
    if (typeof tool !== "string" || typeof session !== "string" || typeof call !== "string" ||
        (!product && !Object.hasOwn(specs, tool)) || !/^[A-Za-z0-9_-]{1,128}$/.test(session) ||
        !/^[A-Za-z0-9_-]{1,128}$/.test(call)) fail()
    const args = output.args, spec = specs[tool]
    if (!args || Object.getPrototypeOf(args) !== Object.prototype) fail()
    let budget = 8192
    const freezeJson = (value, depth = 0) => {
      if (--budget < 0 || depth > 16) fail()
      if (value === null || ["string", "boolean"].includes(typeof value)) return
      if (typeof value === "number") {
        if (!Number.isFinite(value) || Math.abs(value) > 9007199254740991) fail()
        return
      }
      if (typeof value !== "object" ||
          (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype)) fail()
      for (const key of Reflect.ownKeys(value)) {
        if (Array.isArray(value) && key === "length") continue
        if (typeof key !== "string" || ["__proto__", "prototype", "constructor"].includes(key)) fail()
        const d = Object.getOwnPropertyDescriptor(value, key)
        if (!d || !Object.hasOwn(d, "value") || !d.enumerable) fail()
        freezeJson(d.value, depth + 1)
      }
      Object.freeze(value)
    }
    if (product) {
      if (Object.hasOwn(args, cfg.ticket_field)) fail()
      // Freeze the original input while reserving one transport-only slot.
      for (const key of Reflect.ownKeys(args)) {
        if (typeof key !== "string" || ["__proto__", "prototype", "constructor"].includes(key)) fail()
        const d = Object.getOwnPropertyDescriptor(args, key)
        if (!d || !Object.hasOwn(d, "value") || !d.enumerable) fail()
        freezeJson(d.value, 1)
        Object.defineProperty(args, key, {writable:false, configurable:false})
      }
      Object.defineProperty(args, cfg.ticket_field, {value:undefined, writable:true, enumerable:true})
      Object.preventExtensions(args)
    }
    const keys = Reflect.ownKeys(args)
    if (!product && (keys.some(k => typeof k !== "string" || !spec[1].includes(k)) ||
        spec[0].some(k => !Object.hasOwn(args, k)))) fail()
    for (const key of product ? [] : keys) {
      const d = Object.getOwnPropertyDescriptor(args, key)
      if (!d || !Object.hasOwn(d, "value") || !d.enumerable) fail()
      const value = d.value
      if (["offset", "limit", "timeout"].includes(key)) {
        if (!Number.isSafeInteger(value) || value <= 0 || value > 2147483647) fail()
      } else if (key === "replaceAll") {
        if (typeof value !== "boolean") fail()
      } else if (typeof value !== "string" || value.includes("\0")) fail()
      if (["filePath", "workdir"].includes(key) &&
          (!value.startsWith("/") || value.startsWith("//") ||
           value.split("/").some((v, i) => i > 0 && (v === ".." || v === "." || v === "")))) fail()
    }
    if (!product) Object.freeze(args)
    Object.freeze(output)
    Object.freeze(input)
    const nonce = crypto.randomUUID()
    const body = JSON.stringify({version: 1, generation: cfg.generation, nonce,
      session_id: session, call_id: call, tool, args})
    if (new TextEncoder().encode(body).length > 262144) fail()
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), cfg.timeout)
    try {
      const response = await fetch(cfg.url, {method: "POST", redirect: "error",
        headers: {"Authorization": "Bearer " + cfg.token, "Content-Type": "application/json"},
        body, signal: controller.signal})
      if (response.status !== 200) fail()
      const raw = await response.text()
      if (raw.length > 1024) fail()
      const answer = JSON.parse(raw)
      const fields = product ? "allowed,nonce,ticket" : "allowed,nonce"
      if (!answer || Object.keys(answer).sort().join(",") !== fields ||
          answer.allowed !== true || answer.nonce !== nonce) fail()
      if (product) {
        if (typeof answer.ticket !== "string" || !/^pt_[a-f0-9]{64}$/.test(answer.ticket)) fail()
        Object.defineProperty(args, cfg.ticket_field, {value:answer.ticket, writable:false})
        Object.freeze(args)
      }
    } catch { fail() } finally { clearTimeout(timeout) }
  },
})
"""
GATE_FINGERPRINT = hashlib.sha256(_GATE_JS.encode()).hexdigest()


def preflight_fingerprint(endpoint):
    return hashlib.sha256(
        json.dumps(
            {
                "gate": GATE_FINGERPRINT,
                "product_tools": sorted(endpoint.product_tool_names),
                **({"model_gateway": {"model": endpoint.model_gateway.model,
                                      "destination": endpoint.model_gateway.destination}}
                   if endpoint.model_gateway else {}),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def gate_source(endpoint, timeout, *, provider="openjiuwen"):
    return _GATE_JS.replace(
        "__ENDPOINT__",
        json.dumps(
            {
                "url": endpoint.url,
                "token": endpoint.token,
                "generation": endpoint.generation,
                "timeout": int(timeout * 1000),
                "product_tools": [_PRODUCT_SERVER + "_" + name for name in endpoint.product_tool_names],
                "ticket_field": PRODUCT_TICKET_FIELD,
                "model": endpoint.model_gateway.model if endpoint.model_gateway else None,
                "provider": provider,
            }
        ),
    )


def stage_preflight(root: Path, endpoint, timeout, *, provider="openjiuwen"):
    source = root / "preflight-source"
    source.mkdir(mode=0o700)
    entry = source / "gate.js"
    entry.write_text(gate_source(endpoint, timeout, provider=provider), encoding="utf-8")
    entry.chmod(0o400)
    source.chmod(0o500)
    plugin = OpenCodeNativePluginConfig(
        "openjiuwen-preflight",
        "local",
        str(source),
        "1",
        opencode_plugin_content_digest(source),
        "gate.js",
        "Preflight",
        ("tool.execute.before", "chat.headers"),
    )
    return stage_native_plugins(root, (plugin,), fingerprint=validate_native_plugin_packages((plugin,)))
