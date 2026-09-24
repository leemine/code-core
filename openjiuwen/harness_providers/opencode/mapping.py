# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Root-message-scoped native snapshots/deltas and authoritative turn results."""

import json
import re
from collections.abc import Mapping
from decimal import Decimal

from openjiuwen.harness_protocol import (
    ContentBlock,
    ItemEventKind,
    ItemLifecycleEvent,
    MessageRole,
    MonetaryAmount,
    OutputChannel,
    OutputEvent,
    OutputKind,
    OutputOperation,
    ProviderEvent,
    TurnMessage,
    TurnResult,
    TurnStatus,
    TurnTermination,
    TurnTerminationKind,
    TurnUsage,
    UsageUpdatedEvent,
)

from .errors import OpenCodeError, http_error


def native_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[a-zA-Z0-9]+", value):
        raise OpenCodeError("invalid_native_identifier")
    return value


def native_error(error):
    if not isinstance(error, dict):
        return OpenCodeError("native_error")
    name = error.get("name")
    data = error.get("data", {})
    status = data.get("statusCode") if isinstance(data, dict) else None
    if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
        return http_error(status)
    category = {"ProviderAuthError": "auth_required", "APIError": "server_unavailable"}.get(name, "sdk_error")
    return OpenCodeError("native_error", category=category)


class Accumulator:
    def __init__(self, session_id, user_id, limit):
        self.session_id, self.user_id, self.limit = session_id, user_id, limit
        self.infos, self.parts = {}, {}
        self.tool_started, self.tool_completed = set(), set()
        self.final_id = None
        self.aborted_id = None
        self.denied_calls = set()
        self.interactions = {}
        self.seen_bytes = 0

    def consume(self, event, *, abort_requested=False):
        kind, props = event["type"], event["properties"]
        if props.get("sessionID") != self.session_id:
            return []
        self.seen_bytes += len(json.dumps(event, ensure_ascii=False).encode())
        if self.seen_bytes > self.limit:
            raise OpenCodeError("turn_event_limit")
        if kind in {"permission.asked", "question.asked"}:
            raise OpenCodeError("interaction_event_not_routed")
        if kind == "session.error":
            raise native_error(props.get("error"))
        if kind == "message.updated":
            info = props["info"]
            if (
                info.get("role") != "assistant"
                or info.get("parentID") != self.user_id
                or info.get("sessionID") != self.session_id
            ):
                return []
            mid = native_id(info["id"], "msg")
            if info.get("error"):
                error = info["error"]
                if (
                    abort_requested
                    and isinstance(error, Mapping)
                    and error.get("name") == "MessageAbortedError"
                    and info.get("parentID") == self.user_id
                ):
                    self.infos[mid] = info
                    self.parts.setdefault(mid, {})
                    self.aborted_id = mid
                    self.final_id = None
                    return []
                raise native_error(info["error"])
            self.infos[mid] = info
            self.parts.setdefault(mid, {})
            if mid == next(reversed(self.infos)):
                self.final_id = mid if info.get("time", {}).get("completed") and info.get("finish") == "stop" else None
            usage = self.usage()
            return [(UsageUpdatedEvent(usage), None)] if usage else []
        if kind == "message.part.updated":
            return self.part(props["part"])
        if kind == "message.part.delta":
            mid, pid = props["messageID"], props["partID"]
            if mid not in self.infos:
                return []
            part = self.parts[mid].get(pid)
            if part is None or props.get("field") != "text" or part.get("type") not in {"text", "reasoning"}:
                raise OpenCodeError("unmapped_native_delta")
            delta = props["delta"]
            if not isinstance(delta, str):
                raise OpenCodeError("invalid_native_delta")
            part["text"] = part.get("text", "") + delta
            return [(self.output(part, delta, OutputOperation.DELTA), pid)]
        return []

    def claim_interaction(self, event):
        """Validate and claim one native request belonging to this root turn.

        The legacy OpenCode permission endpoint does not enforce the session in
        its URL.  This local ledger is therefore the authority for session,
        root-message and duplicate ownership before any reply is sent.
        """
        kind, props = event["type"], event["properties"]
        if props.get("sessionID") != self.session_id:
            return None
        prefix = "per" if kind == "permission.asked" else "que" if kind == "question.asked" else None
        if prefix is None:
            raise OpenCodeError("invalid_interaction_event")
        request_id = native_id(props.get("id"), prefix)
        tool = props.get("tool")
        if not isinstance(tool, Mapping):
            raise OpenCodeError("invalid_interaction_tool")
        message_id = native_id(tool.get("messageID"), "msg")
        call_id = tool.get("callID")
        info = self.infos.get(message_id)
        if (
            info is None
            or info.get("parentID") != self.user_id
            or not isinstance(call_id, str)
            or not call_id
        ):
            raise OpenCodeError("interaction_scope_mismatch")
        fingerprint = json.dumps(props, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        previous = self.interactions.get(request_id)
        if previous is not None:
            if previous != fingerprint:
                raise OpenCodeError("interaction_identity_conflict")
            return None
        self.interactions[request_id] = fingerprint
        return request_id, call_id

    def mark_denied(self, call_id):
        self.denied_calls.add(call_id)

    @property
    def rejected_id(self):
        if not self.infos:
            return None
        message_id = next(reversed(self.infos))
        info = self.infos[message_id]
        if not info.get("time", {}).get("completed") or info.get("finish") != "tool-calls":
            return None
        for part in self.parts.get(message_id, {}).values():
            if (
                part.get("type") == "tool"
                and part.get("callID") in self.denied_calls
                and part.get("state", {}).get("status") == "error"
            ):
                return message_id
        return None

    @staticmethod
    def output(part, text, operation):
        return OutputEvent(
            output_id=part["id"],
            kind=OutputKind.TEXT,
            content=text,
            operation=operation,
            channel=OutputChannel.REASONING if part["type"] == "reasoning" else OutputChannel.ANSWER,
            data={"opencode": {"message_id": part["messageID"], "part_id": part["id"]}},
        )

    def part(self, part):
        mid = part.get("messageID")
        if mid not in self.infos or part.get("sessionID") != self.session_id:
            return []
        pid = native_id(part["id"], "prt")
        previous = self.parts[mid].get(pid)
        self.parts[mid][pid] = dict(part)
        if previous == part:
            return []
        kind = part["type"]
        if kind in {"text", "reasoning"}:
            operation = OutputOperation.FINAL if part.get("time", {}).get("end") else OutputOperation.SNAPSHOT
            return [(self.output(part, part.get("text", ""), operation), pid)]
        if kind == "tool":
            call = part.get("callID")
            if not isinstance(call, str) or not call:
                raise OpenCodeError("invalid_native_tool_call")
            state = part["state"]
            data = {
                "name": part["tool"],
                "arguments": state.get("input", {}),
                "opencode": {"part_id": pid, "message_id": mid, "status": state["status"]},
            }
            events = []
            if call not in self.tool_started:
                events.append((ItemLifecycleEvent(ItemEventKind.STARTED, "tool", data), call))
                self.tool_started.add(call)
            if state["status"] in {"completed", "error"} and call not in self.tool_completed:
                data["result"] = state.get("output", state.get("error", ""))
                data["rendered_result"] = data["result"]
                events.append((ItemLifecycleEvent(ItemEventKind.COMPLETED, "tool", data), call))
                self.tool_completed.add(call)
            elif call not in self.tool_completed:
                events.append((ItemLifecycleEvent(ItemEventKind.UPDATED, "tool", data), call))
            return events
        return [(ProviderEvent("opencode", "message.part.updated", "1", {"part": part}), pid)]

    def is_idle(self, event):
        props = event["properties"]
        return props.get("sessionID") == self.session_id and (
            event["type"] == "session.idle"
            or event["type"] == "session.status"
            and props.get("status", {}).get("type") == "idle"
        )

    def reconcile(self, message, *, rejected=False):
        info = message["info"]
        expected_id = self.rejected_id if rejected else self.final_id
        expected_finish = "tool-calls" if rejected else "stop"
        if (
            info.get("id") != expected_id
            or info.get("parentID") != self.user_id
            or info.get("sessionID") != self.session_id
            or info.get("role") != "assistant"
            or info.get("finish") != expected_finish
            or not info.get("time", {}).get("completed")
            or info.get("error")
        ):
            raise OpenCodeError("terminal_reconciliation_failed")
        result = self.consume({"type": "message.updated", "properties": {"sessionID": self.session_id, "info": info}})
        for part in message["parts"]:
            result.extend(
                self.consume(
                    {"type": "message.part.updated", "properties": {"sessionID": self.session_id, "part": part}}
                )
            )
        return result

    def usage(self):
        tokens = [info["tokens"] for info in self.infos.values() if isinstance(info.get("tokens"), dict)]
        if not tokens:
            return None

        def count(key, nested=None):
            values = [(t.get(nested, {}) if nested else t).get(key) for t in tokens]
            return (
                sum(values) if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values) else None
            )

        return TurnUsage(
            input_tokens=count("input"),
            output_tokens=count("output"),
            cached_input_tokens=count("read", "cache"),
            reasoning_output_tokens=count("reasoning"),
            total_tokens=count("total"),
            provider_data={"opencode": {"cache_write_tokens": count("write", "cache")}},
        )

    def result(self, timing, *, error=None, stopped=False, aborted=False):
        messages = []
        for mid, info in self.infos.items():
            blocks = []
            for pid, part in self.parts[mid].items():
                kind = part["type"]
                content = part.get("text", part)
                if kind == "tool":
                    state = part["state"]
                    blocks.append(
                        ContentBlock(
                            pid + ":call",
                            "tool_call",
                            state.get("input", {}),
                            {"name": part["tool"], "call_id": part["callID"], "opencode": part},
                        )
                    )
                    if state["status"] in {"completed", "error"}:
                        blocks.append(
                            ContentBlock(
                                pid,
                                "tool_result",
                                state.get("output", state.get("error", "")),
                                {"name": part["tool"], "call_id": part["callID"], "opencode": part},
                            )
                        )
                else:
                    blocks.append(ContentBlock(pid, kind, content, {"opencode": part}))
            messages.append(TurnMessage(mid, MessageRole.ASSISTANT, tuple(blocks), {"opencode": info}))
        final = "".join(p.get("text", "") for p in self.parts.get(self.final_id, {}).values() if p["type"] == "text")
        costs = [Decimal(str(i["cost"])) for i in self.infos.values() if "cost" in i]
        cost = (
            MonetaryAmount(int(sum(costs) * 1_000_000))
            if costs and all(c.is_finite() and c >= 0 for c in costs)
            else None
        )
        return TurnResult(
            status=(
                TurnStatus.INTERRUPTED if stopped or aborted else TurnStatus.FAILED if error else TurnStatus.COMPLETED
            ),
            messages=tuple(messages),
            final_output=final or None,
            stop_reason="stop" if not (stopped or error) else None,
            termination=(
                TurnTermination(TurnTerminationKind.HARNESS_STOP if stopped else TurnTerminationKind.USER_ABORT)
                if stopped or aborted
                else None
            ),
            error=error.turn_error() if error and not (stopped or aborted) else None,
            usage=self.usage(),
            cost=cost,
            started_at=timing.started_at,
            completed_at=timing.completed_at(),
            duration_ms=timing.duration_ms(),
            provider_data={"opencode": {"session_id": self.session_id, "user_message_id": self.user_id}},
        )
