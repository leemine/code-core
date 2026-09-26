# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OpenCode adapter using the shared serialized turn and interaction lifecycle."""

import asyncio
import sys
import time
import uuid
from collections.abc import Mapping
from typing import Any

from openjiuwen.harness_protocol import (
    AbortMode,
    CheckpointReason,
    HarnessCapability,
    HarnessCard,
    HarnessContext,
    HarnessProtocolError,
    HostCapability,
    InteractionCancelReason,
    InteractionResponseStatus,
    ResumePolicy,
    ToolApprovalDecision,
    ToolApprovalRequest,
    TurnEventKind,
    TurnResult,
    UnsupportedHarnessCapabilityError,
    UserInputRequest,
    json_value_to_builtin,
)
from openjiuwen.harness_providers.base import PendingTurn, ProviderStartupError, SerializedTurnHarness, TurnTiming
from openjiuwen.harness_providers.inputs import harness_input_text
from openjiuwen.harness_providers.skills import install_skills, isolated_skill_scan_directory

from .config import CLI_VERSION, OpenCodeHarnessConfig
from .errors import OpenCodeError
from .mapping import Accumulator, native_id
from .native_plugins import validate_native_plugin_packages
from .options import validate_readback


class OpenCodeHarness(SerializedTurnHarness):
    card = HarnessCard(
        name="opencode",
        implementation_version="0.2.0",
        capabilities=frozenset(
            {
                HarnessCapability.GRACEFUL_ABORT,
                HarnessCapability.PERSISTENT_SESSION,
                HarnessCapability.CHECKPOINT,
                HarnessCapability.MCP_TOOLS,
            }
        ),
        optional_host_capabilities=frozenset(
            {
                HostCapability.CHECKPOINT_SINK,
                HostCapability.MCP_SERVERS,
                HostCapability.TOOL_APPROVAL,
                HostCapability.USER_INPUT,
            }
        ),
    )

    def __init__(self, config: OpenCodeHarnessConfig | None = None) -> None:
        self._config = config or OpenCodeHarnessConfig()
        super().__init__(event_buffer_capacity=self._config.event_buffer_capacity)
        self._server = self._transport = None
        self._poisoned = False
        self._native_plugin_fingerprint = None

    def _validate_context(self, context: HarnessContext) -> None:
        super()._validate_context(context)
        if context.resume_policy is ResumePolicy.REQUIRE_RESUME and context.checkpoint is None:
            raise HarnessProtocolError("OpenCode cannot require resume without a checkpoint")
        if context.checkpoint is not None and context.resume_policy is ResumePolicy.NEW:
            raise HarnessProtocolError("OpenCode checkpoint requires an explicit resume policy")
        if (
            context.host_capabilities & {HostCapability.TOOL_APPROVAL, HostCapability.USER_INPUT}
            and context.interactions is None
        ):
            raise HarnessProtocolError("OpenCode interactive host capabilities require an interaction handler")
        if context.mcp_servers and HostCapability.MCP_SERVERS not in context.host_capabilities:
            raise HarnessProtocolError("OpenCode MCP configuration requires the MCP_SERVERS host capability")
        if context.env or context.tools is not None or context.hooks is not None:
            raise UnsupportedHarnessCapabilityError("OpenCode does not support environment, native host tools or hooks")

    async def _open_session(self, context: HarnessContext) -> str:
        # Imports remain cheap and platform-independent until runtime startup.
        self._poisoned = False
        try:
            if sys.platform != "linux":
                raise OpenCodeError("unsupported_supervisor_platform", category="process_start_failed")
            from .server import ManagedServer
            from .transport import Transport

            async with asyncio.timeout(self._config.startup_timeout_s):
                self._native_plugin_fingerprint = (
                    await asyncio.to_thread(validate_native_plugin_packages, self._config.native_plugins)
                    if self._config.native_plugins is not None
                    else None
                )
                skill_path = None
                if self._config.skills:
                    skill_path = isolated_skill_scan_directory(
                        self._config.skills,
                        provider="opencode",
                        cwd=context.cwd,
                        conflict=self._config.skill_conflict,
                    )
                    await asyncio.to_thread(
                        install_skills,
                        self._config.skills,
                        provider="opencode",
                        cwd=context.cwd,
                        conflict=self._config.skill_conflict,
                        scan_dir=skill_path,
                    )
                self._server = ManagedServer(self._config, context, skill_path=skill_path)
                await self._server.start()
                self._transport = Transport(self._server, self._config)
                while True:
                    if self._server.process.returncode is not None:
                        raise OpenCodeError("server_exited", category="process_start_failed")
                    # Never attach merely because a recycled port answers health.
                    if self._server.url in self._server.log_path.read_text(errors="replace"):
                        try:
                            health = await self._transport.request("GET", "/global/health")
                            if health == {"healthy": True, "version": CLI_VERSION}:
                                break
                        except OpenCodeError as exc:
                            if exc.reason != "http_transport_failed":
                                raise
                    await asyncio.sleep(0.05)
                await self._verify()
                await self._transport.connect()
                session_id, resumed = await self._activate_session(context)
                self._session_id = session_id
                await self._publish_session_checkpoint(
                    reason=CheckpointReason.SESSION_ACTIVATED,
                    resumable=True,
                    state="idle",
                    resumed=resumed,
                )
                return session_id
        except HarnessProtocolError:
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, OpenCodeError)
                else OpenCodeError(
                    "startup_timeout" if isinstance(exc, TimeoutError) else "startup_failed",
                    category="process_start_failed",
                )
            )
            raise ProviderStartupError("failed to start managed OpenCode", error=error.turn_error()) from None

    async def _activate_session(self, context: HarnessContext) -> tuple[str, bool]:
        restored = self._restored_checkpoint_data(context)
        if restored is None:
            if context.resume_policy is ResumePolicy.REQUIRE_RESUME:
                raise HarnessProtocolError("OpenCode checkpoint is required for resume")
            info = await self._transport.request("POST", "/session", {})
            return native_id(info["id"], "ses"), False
        session_id = restored.get("session_id")
        if not isinstance(session_id, str):
            raise HarnessProtocolError("OpenCode checkpoint does not carry a session id")
        session_id = native_id(session_id, "ses")
        if restored.get("resumable") is not True or restored.get("state") != "idle":
            raise HarnessProtocolError("OpenCode checkpoint does not describe a confirmed idle session")
        if restored.get("native_plugin_fingerprint") != self._native_plugin_fingerprint:
            raise HarnessProtocolError("OpenCode native plugin snapshot changed since session activation")
        await self._validate_resumable_session(session_id)
        return session_id, True

    async def _validate_resumable_session(self, session_id: str) -> None:
        info = await self._transport.request("GET", f"/session/{session_id}")
        if not isinstance(info, Mapping) or info.get("id") != session_id:
            raise OpenCodeError("resume_session_identity_mismatch", category="server_unavailable")
        statuses = await self._transport.request("GET", "/session/status")
        if not isinstance(statuses, Mapping):
            raise OpenCodeError("invalid_session_status")
        status = statuses.get(session_id)
        if status is not None and (not isinstance(status, Mapping) or status.get("type") != "idle"):
            raise OpenCodeError("resume_session_not_idle", category="server_unavailable")
        for path in ("/permission", "/question"):
            pending = await self._transport.request("GET", path)
            if not isinstance(pending, list) or any(not isinstance(item, Mapping) for item in pending):
                raise OpenCodeError("invalid_pending_interactions")
            if any(item.get("sessionID") == session_id for item in pending):
                raise OpenCodeError("resume_session_has_pending_interaction", category="server_unavailable")
        messages = await self._transport.request("GET", f"/session/{session_id}/message")
        if not isinstance(messages, list) or any(not isinstance(item, Mapping) for item in messages):
            raise OpenCodeError("invalid_session_history")
        if messages:
            last_info = messages[-1].get("info")
            if (
                not isinstance(last_info, Mapping)
                or last_info.get("role") != "assistant"
                or not last_info.get("time", {}).get("completed")
            ):
                raise OpenCodeError("resume_session_result_unknown", category="server_unavailable")

    async def _publish_session_checkpoint(
        self,
        *,
        reason: CheckpointReason,
        resumable: bool,
        state: str,
        resumed: bool = False,
        turn_id: str | None = None,
    ) -> None:
        await self._publish_checkpoint(
            {
                "session_id": self._session_id,
                "resumable": resumable,
                "state": state,
                "resumed": resumed,
                "turn_id": turn_id,
                "native_plugin_fingerprint": self._native_plugin_fingerprint,
            },
            reason=reason,
        )

    async def _verify(self):
        await self._server.verify_running()
        validate_readback(await self._transport.request("GET", "/config"), self._server.native_config)
        await self._server.verify_native_plugin_inventory()

    async def _close_session(self) -> None:
        self._poisoned = True
        if self._transport:
            await self._transport.close()
        if self._server:
            await self._server.stop()
        self._transport = self._server = None

    async def _execute_turn(self, turn: PendingTurn) -> tuple[TurnEventKind, TurnResult]:
        timing = TurnTiming()
        # Fixed native ascending ID: low six bytes of millis * 4096, plus entropy.
        user_id = f"msg_{(int(time.time() * 1000) * 4096) & ((1 << 48) - 1):012x}{uuid.uuid4().hex[:14]}"
        acc = Accumulator(self._session_id, user_id, self._config.max_turn_bytes)
        transport = self._transport
        try:
            async with asyncio.timeout(self._config.turn_timeout_s):
                if self._poisoned or transport is None:
                    raise OpenCodeError("session_requires_restart", category="server_unavailable")
                if self._config.native_plugins is not None:
                    current_plugins = await asyncio.to_thread(
                        validate_native_plugin_packages,
                        self._config.native_plugins,
                    )
                    if current_plugins != self._native_plugin_fingerprint:
                        raise OpenCodeError("native_plugin_source_drift", category="process_start_failed")
                await self._verify()
                await self._publish_session_checkpoint(
                    reason=CheckpointReason.STATE_CHANGED,
                    resumable=False,
                    state="turn_active",
                    turn_id=turn.turn_id,
                )
                model = self._config.model
                await transport.request(
                    "POST",
                    f"/session/{self._session_id}/prompt_async",
                    {
                        "messageID": user_id,
                        "model": {"providerID": model.provider, "modelID": model.model},
                        "system": self.context.system_prompt,
                        "parts": [{"type": "text", "text": harness_input_text(turn.content)}],
                    },
                )
                while True:
                    event = await transport.next_event()
                    if await self._route_interaction(turn, acc, event):
                        continue
                    await self._emit_mapped(turn, acc.consume(event, abort_requested=turn.abort_requested))
                    if acc.is_idle(event) and acc.final_id:
                        message = await transport.request("GET", f"/session/{self._session_id}/message/{acc.final_id}")
                        await self._emit_mapped(turn, acc.reconcile(message))
                        await self._publish_session_checkpoint(
                            reason=CheckpointReason.TURN_COMPLETED,
                            resumable=True,
                            state="idle",
                        )
                        return TurnEventKind.FINISHED, acc.result(timing)
                    if acc.is_idle(event) and acc.rejected_id:
                        message = await transport.request(
                            "GET", f"/session/{self._session_id}/message/{acc.rejected_id}"
                        )
                        await self._emit_mapped(turn, acc.reconcile(message, rejected=True))
                        await self._publish_session_checkpoint(
                            reason=CheckpointReason.TURN_COMPLETED,
                            resumable=True,
                            state="idle",
                        )
                        error = OpenCodeError("interaction_declined")
                        return TurnEventKind.FAILED, acc.result(timing, error=error)
                    if acc.is_idle(event) and acc.aborted_id and turn.abort_requested:
                        await self._publish_session_checkpoint(
                            reason=CheckpointReason.TURN_COMPLETED,
                            resumable=True,
                            state="idle",
                        )
                        return TurnEventKind.ABORTED, acc.result(
                            timing,
                            stopped=turn.stop_requested,
                            aborted=not turn.stop_requested,
                        )
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, OpenCodeError)
                else OpenCodeError(
                    "turn_timeout" if isinstance(exc, TimeoutError) else "native_protocol_error",
                    category="network_timeout" if isinstance(exc, TimeoutError) else "sdk_error",
                )
            )
            self._poisoned = True
            # Closing wakes the stream reader and reaps tools, including detached
            # native bash children. Retain uncertain handles for stop() retry.
            try:
                await self._close_session()
            except Exception:
                error = OpenCodeError("owned_exit_unconfirmed", category="server_unavailable")
            return (
                TurnEventKind.ABORTED if turn.abort_requested else TurnEventKind.FAILED,
                acc.result(
                    timing,
                    error=error,
                    stopped=turn.stop_requested,
                    aborted=turn.abort_requested and not turn.stop_requested,
                ),
            )

    async def _interrupt_turn(self, turn: PendingTurn, mode: AbortMode) -> None:
        _ = turn, mode
        transport = self._transport
        if transport is None or self._session_id is None:
            return
        result = await transport.request("POST", f"/session/{self._session_id}/abort")
        if not isinstance(result, bool):
            raise OpenCodeError("invalid_abort_response")

    async def _route_interaction(self, turn: PendingTurn, acc: Accumulator, event: Mapping[str, Any]) -> bool:
        kind = event.get("type")
        if kind not in {"permission.asked", "question.asked"}:
            return False
        claimed = acc.claim_interaction(event)
        if claimed is None:
            return True
        native_request_id, call_id = claimed
        props = event["properties"]
        if kind == "permission.asked":
            await self._route_permission(turn, acc, native_request_id, call_id, props)
        else:
            await self._route_question(turn, acc, native_request_id, call_id, props)
        return True

    async def _route_permission(self, turn, acc, native_request_id, call_id, props):
        context = self.context
        response = None
        if context is not None and HostCapability.TOOL_APPROVAL in context.host_capabilities:
            request = ToolApprovalRequest(
                request_id=f"opencode-approval:{native_request_id}",
                call_id=call_id,
                tool_name=str(props.get("permission") or "opencode-tool"),
                arguments=props.get("metadata") if isinstance(props.get("metadata"), Mapping) else {},
                provider_session_id=self._session_id,
                turn_id=turn.turn_id,
                provider_data={
                    "opencode": {
                        "request_id": native_request_id,
                        "patterns": props.get("patterns", []),
                        "always": props.get("always", []),
                    }
                },
            )
            response = await self._await_host_interaction(request)
        reply = "reject"
        abort = False
        if response is not None and not turn.abort_requested:
            if response.updated_arguments is not None:
                reply = "reject"
            elif response.decision is ToolApprovalDecision.ALLOW:
                reply = "once"
            elif response.decision is ToolApprovalDecision.ALLOW_FOR_SESSION:
                reply = "always"
            elif response.decision is ToolApprovalDecision.ABORT:
                abort = True
        if reply == "reject":
            acc.mark_denied(call_id)
        if turn.abort_requested:
            reply = "reject"
        await self._reply_native(
            turn,
            "POST",
            f"/session/{self._session_id}/permissions/{native_request_id}",
            {"response": reply},
        )
        if abort and not turn.abort_requested:
            turn.abort_requested = True
            turn.abort_mode = AbortMode.GRACEFUL
            await self._interrupt_turn(turn, AbortMode.GRACEFUL)

    async def _route_question(self, turn, acc, native_request_id, call_id, props):
        questions = self._questions(props.get("questions"))
        context = self.context
        response = None
        if questions and context is not None and HostCapability.USER_INPUT in context.host_capabilities:
            request = UserInputRequest(
                request_id=f"opencode-question:{native_request_id}",
                prompt=self._render_questions(questions),
                choices=self._first_choices(questions),
                provider_session_id=self._session_id,
                turn_id=turn.turn_id,
                provider_data={
                    "opencode": {
                        "request_id": native_request_id,
                        "call_id": call_id,
                        "questions": questions,
                    }
                },
            )
            response = await self._await_host_interaction(request)
        if response is not None and response.status is InteractionResponseStatus.COMPLETED and not turn.abort_requested:
            answers = self._answers(json_value_to_builtin(response.content), questions)
        else:
            answers = None
        if answers is None:
            acc.mark_denied(call_id)
            await self._reply_native(turn, "POST", f"/question/{native_request_id}/reject")
        else:
            await self._reply_native(
                turn,
                "POST",
                f"/question/{native_request_id}/reply",
                {"answers": answers},
            )

    async def _await_host_interaction(self, request):
        transport = self._transport
        if transport is None:
            raise OpenCodeError("session_requires_restart", category="server_unavailable")
        interaction = asyncio.create_task(self._request_interaction(request))
        disconnected = asyncio.create_task(transport.closed.wait())
        try:
            done, _ = await asyncio.wait({interaction, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if interaction in done:
                return interaction.result()
            await self._cancel_pending_interactions(InteractionCancelReason.PROVIDER_WITHDREW)
            interaction.cancel()
            await asyncio.gather(interaction, return_exceptions=True)
            raise transport.failure or OpenCodeError("event_stream_closed", category="server_unavailable")
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)

    async def _reply_native(self, turn, method, path, body=None):
        try:
            await self._transport.request(method, path, body)
        except OpenCodeError as exc:
            # Abort may withdraw a native request before the locally-cancelled
            # host handler returns.  A 404 in that exact race is not a reply
            # that can be retried against another request.
            if turn.abort_requested and exc.status == 404:
                return
            raise

    @staticmethod
    def _questions(value):
        if not isinstance(value, list) or not value:
            return []
        result = []
        for item in value:
            if not isinstance(item, Mapping) or not str(item.get("question") or "").strip():
                return []
            options = item.get("options", [])
            if not isinstance(options, list) or any(not isinstance(option, Mapping) for option in options):
                return []
            result.append(
                {
                    "question": str(item["question"]),
                    "header": str(item.get("header") or ""),
                    "options": [
                        {
                            "label": str(option.get("label") or ""),
                            "description": str(option.get("description") or ""),
                        }
                        for option in options
                        if option.get("label")
                    ],
                }
            )
        return result

    @staticmethod
    def _render_questions(questions):
        lines = []
        for question in questions:
            label = question["question"]
            if question["header"]:
                label = f"{question['header']}: {label}"
            lines.append(label)
            for option in question["options"]:
                suffix = f": {option['description']}" if option["description"] else ""
                lines.append(f"  - {option['label']}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _first_choices(questions):
        return tuple(option["label"] for option in questions[0]["options"]) if questions else ()

    @staticmethod
    def _answers(content, questions):
        if not questions:
            return None
        if isinstance(content, Mapping):
            content = content.get("answers", content)
            values = []
            for question in questions:
                value = content.get(question["question"])
                if value is None and question["header"]:
                    value = content.get(question["header"])
                values.append(value)
        elif isinstance(content, list):
            values = list(content)
        else:
            values = [content]
        answers = []
        for index in range(len(questions)):
            value = values[index] if index < len(values) else None
            if value is None:
                answers.append([])
            elif isinstance(value, list):
                answers.append([str(item) for item in value])
            else:
                answers.append([str(value)])
        return answers if any(answers) else None

    async def _emit_mapped(self, turn, events):
        for payload, item_id in events:
            await self._emit(payload, turn=turn, item_id=item_id, provider_session_id=self._session_id)
