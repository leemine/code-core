# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Private model routing and original-Turn proof; no credentials or HTTP client."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import OpenCodeError

SOURCE_HEADERS = (
    "x-openjiuwen-session",
    "x-openjiuwen-root",
    "x-openjiuwen-generation",
    "x-openjiuwen-agent",
    "x-openjiuwen-model",
    "x-openjiuwen-provider",
)
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True, slots=True, repr=False)
class OpenCodeModelGateway:
    """Constructor-only host gateway; destination never comes from model input."""

    url: str
    token: str = field(repr=False)
    generation: str
    model: str
    destination: str

    def __post_init__(self):
        for value in (self.url, self.destination):
            if not isinstance(value, str) or any(ord(c) < 33 for c in value):
                raise ValueError("invalid model gateway URL")
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or value.endswith("/")
                or "{env:" in value
                or "{file:" in value
            ):
                raise ValueError("invalid model gateway URL")
        local = urlsplit(self.url)
        if local.scheme != "http" or local.hostname != "127.0.0.1" or not local.port or not local.path:
            raise ValueError("model gateway must be explicit loopback")
        if not isinstance(self.token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.token):
            raise ValueError("invalid model gateway authentication")
        if not isinstance(self.generation, str) or not _ID.fullmatch(self.generation):
            raise ValueError("invalid model gateway generation")
        if (
            not isinstance(self.model, str)
            or not self.model
            or len(self.model) > 256
            or any(ord(c) < 33 for c in self.model)
            or "{env:" in self.model
            or "{file:" in self.model
        ):
            raise ValueError("invalid model gateway model")

    def validate_model(self, model):
        """Reject config conflicts before service allocation or credential rendering."""
        if (
            model is None
            or model.model != self.model
            or model.api_base != self.destination
            or model.api_key is not None
        ):
            raise OpenCodeError("model_gateway_config_conflict", category="process_start_failed")


@dataclass(frozen=True, slots=True, repr=False)
class OpenCodeModelSource:
    """Captured source for ONE authenticated POST; recheck after every await.

    It proves Provider source, not host resource authorization. The host still
    fixes the body bytes and authorizes its exact catalog destination.
    """

    turn_id: str
    session_id: str
    root_message: str
    model: str
    method: str
    path: str
    _config: object
    _turn: object
    _context: object
    _transport: object
    _origin: object
    _gateway: OpenCodeModelGateway
    _gate: object
    _harness: object
    _identity: object = field(default=None, init=False)


def model_source_current(gate, harness, source):
    """Validate the original captured object against the existing live Turn."""
    if not isinstance(source, OpenCodeModelSource):
        return False
    return (
        source._identity is source
        and source._gate is gate
        and source._harness is harness
        and source._config is harness._config
        and not gate.closed
        and source._origin is gate.model_origin
        and gate.model_origin is not None
        and gate.turn is source._turn
        and harness.active_turn is source._turn
        and source._turn.turn_id == source.turn_id
        and gate.root_message == source.root_message
        and harness.context is source._context
        and harness._transport is source._transport
        and source._transport is not None
        and harness.provider_session_id == source.session_id
        and gate.endpoint.model_gateway is source._gateway
        and source._gateway.generation == gate.endpoint.generation
        and source._gateway.model == source.model
        and source.method == "POST"
        and source.path == urlsplit(source._gateway.url).path + "/chat/completions"
        and not source._turn.abort_requested
        and not source._turn.stop_requested
        and not harness._stopping
        and not harness._poisoned
    )


async def capture_model_source(gate, harness, headers, *, method, path, model):
    """Host must authenticate local bearer before invoking this private seam."""
    gateway = gate.endpoint.model_gateway
    if gateway is None or method != "POST" or path != urlsplit(gateway.url).path + "/chat/completions":
        return None
    # Host passes only reserved headers; unknown, duplicate/case aliases fail closed.
    if not isinstance(headers, dict) or set(headers) != set(SOURCE_HEADERS):
        return None
    if (
        any(not isinstance(value, str) for value in headers.values())
        or not _ID.fullmatch(headers[SOURCE_HEADERS[0]])
        or not _ID.fullmatch(headers[SOURCE_HEADERS[1]])
        or headers[SOURCE_HEADERS[2]] != gateway.generation
        or headers[SOURCE_HEADERS[3]] != "build"
        or headers[SOURCE_HEADERS[4]] != gateway.model
        or model != gateway.model
        or headers[SOURCE_HEADERS[5]] != harness._config.model.provider
    ):
        return None
    turn, context, transport = gate.turn, harness.context, harness._transport
    if turn is None or context is None or transport is None:
        return None
    source = OpenCodeModelSource(
        turn.turn_id,
        headers[SOURCE_HEADERS[0]],
        headers[SOURCE_HEADERS[1]],
        model,
        method,
        path,
        harness._config,
        turn,
        context,
        transport,
        gate.model_origin,
        gateway,
        gate,
        harness,
    )
    object.__setattr__(source, "_identity", source)
    if not model_source_current(gate, harness, source):
        return None
    try:
        async with asyncio.timeout(harness._config.request_timeout_s):
            messages = await transport.request("GET", f"/session/{source.session_id}/message")
        if not model_source_current(gate, harness, source) or not isinstance(messages, list):
            return None
        roots = [
            message.get("info")
            for message in messages
            if isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("id") == source.root_message
        ]
        if (
            len(roots) != 1
            or roots[0].get("role") != "user"
            or roots[0].get("sessionID") != source.session_id
            or roots[0].get("agent") != "build"
            or roots[0].get("model") != {"providerID": harness._config.model.provider, "modelID": model}
        ):
            return None
        return source
    except Exception:
        return None
