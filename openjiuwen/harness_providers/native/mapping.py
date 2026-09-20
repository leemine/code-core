# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native output compatibility data carried on the existing event stream.

This is a JSON snapshot for legacy consumers, not a control channel or a
replacement for shared mode/step/subagent contracts. Interaction requests
must continue through the protocol interaction handler.
"""

from __future__ import annotations

from typing import Any, Mapping

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import ItemLifecycleEvent, OutputEvent, ProviderEvent, json_value_to_builtin
from openjiuwen.harness_providers.jsonsafe import to_json_safe

NATIVE_CHUNK_KEY = "native.output_chunk.v1"


def snapshot_chunk(chunk: Any) -> dict[str, Any]:
    """Preserve the original type/index/payload in provider-private data."""
    if isinstance(chunk, Mapping):
        chunk_type, index, payload = chunk.get("type"), chunk.get("index", 0), chunk.get("payload")
    else:
        chunk_type = getattr(chunk, "type", None)
        index, payload = getattr(chunk, "index", 0), getattr(chunk, "payload", None)
    return {
        "type": str(chunk_type or "chunk"),
        "index": index if isinstance(index, int) else 0,
        "payload": to_json_safe(payload),
    }


def restore_chunk(event: Any) -> OutputSchema | None:
    """Restore one Native chunk; never reconstruct an interaction from events."""
    if isinstance(event, (OutputEvent, ItemLifecycleEvent)):
        frame = event.data.get(NATIVE_CHUNK_KEY)
    elif isinstance(event, ProviderEvent) and event.provider == "deepagent":
        frame = event.payload.get(NATIVE_CHUNK_KEY)
    else:
        return None
    if not isinstance(frame, Mapping):
        return None
    chunk_type, index = frame.get("type"), frame.get("index")
    if not isinstance(chunk_type, str) or not isinstance(index, int) or chunk_type == INTERACTION:
        return None
    return OutputSchema(type=chunk_type, index=index, payload=json_value_to_builtin(frame.get("payload")))


def tool_payload(chunk_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept core's flat rail and swarm's nested rail without adding a rail."""
    nested = payload.get(chunk_type)
    if "tool_call_id" not in payload and isinstance(nested, Mapping) and "tool_call_id" in nested:
        return nested
    return payload
