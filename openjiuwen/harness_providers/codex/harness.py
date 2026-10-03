# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HarnessProtocol implementation backed by the Codex Python SDK."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import uuid4

from openjiuwen.harness_protocol import (
    PROTOCOL_VERSION,
    AbortMode,
    BeforeToolContext,
    CheckpointReason,
    DeliveryMode,
    HarnessCapability,
    HarnessCard,
    HarnessContext,
    HarnessInput,
    HarnessProtocolError,
    HarnessStateError,
    HostCapability,
    InteractionResponseStatus,
    ProviderEvent,
    ResumePolicy,
    SendReceipt,
    ToolApprovalDecision,
    ToolApprovalRequest,
    TurnError,
    TurnEventKind,
    TurnResult,
    UnsupportedHarnessCapabilityError,
    UserInputRequest,
    json_value_to_builtin,
)
from openjiuwen.harness_providers.base import (
    PendingTurn,
    ProviderStartupError,
    SerializedTurnHarness,
    TurnTiming,
    interrupted_result,
    logger,
)
from openjiuwen.harness_providers.codex.config import CodexHarnessConfig, CodexModelConfig
from openjiuwen.harness_providers.codex.failure_classifier import classify_codex_exception
from openjiuwen.harness_providers.codex.mapping import PROVIDER_NAME, CodexTurnAccumulator
from openjiuwen.harness_providers.codex.native_plugins import (
    validate_native_plugin_inventory,
    validate_native_plugin_mcp_runtime,
    validate_native_plugin_packages,
)
from openjiuwen.harness_providers.codex.options import (
    append_developer_instructions,
    build_codex_config,
    build_process_env,
    build_thread_options,
    load_codex_sdk,
    start_thread_with_raw_events,
)
from openjiuwen.harness_providers.codex.runtime_policy import compile_runtime_policy
from openjiuwen.harness_providers.codex.sdk_compat import (
    connect_with_host_approvals,
    isolate_process_environment,
    validate_effective_startup_sources,
)
from openjiuwen.harness_providers.codex.source_policy import validate_startup_sources
from openjiuwen.harness_providers.inputs import harness_input_text
from openjiuwen.harness_providers.jsonsafe import to_json_object, to_json_safe
from openjiuwen.harness_providers.skills import install_skills

ADAPTER_VERSION = "0.1.0"
_INTERRUPT_TIMEOUT_S = 5.0
_DRAIN_TIMEOUT_S = 5.0


class _SteerNotAccepted(HarnessStateError):
    """Native acknowledgement proves this input was not consumed."""


@dataclass
class _NativeTurnDrain:
    """Ownership of one physical SDK turn, including an outstanding start."""

    client: Any
    failure: asyncio.Future[Exception]
    handle: Any = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    reader: asyncio.Task[None] | None = None
    confirmed: bool = False
    project_events: bool = True


_APPROVAL_WAIT_TIMEOUT_S = 600.0
# A human answering a question has no natural deadline; the reader thread
# re-checks the event loop between slices instead of giving up.
_USER_INPUT_WAIT_SLICE_S = 30.0
_NO_ACTIVE_TURN_ERROR_CODE = -32600
_NO_ACTIVE_TURN_ERROR_MESSAGE = "no active turn to steer"
_APPROVAL_METHODS = frozenset({"item/commandExecution/requestApproval", "item/fileChange/requestApproval"})
# App Server request emitted by Codex's experimental ``request_user_input`` tool.
USER_INPUT_METHOD = "item/tool/requestUserInput"
_INTERACTIVE_HOST_CAPABILITIES = frozenset({HostCapability.TOOL_APPROVAL, HostCapability.USER_INPUT})
# Provider interaction asking the host to ratify (persist) an auth fallback.
AUTH_FALLBACK_REQUEST_TYPE = "auth_fallback"
# Provider event announcing the active model (session activation / fallback
# switch) so the host reliability context can attribute failures to it.
_MODEL_CHANGED_EVENT = "session/model_changed"

NotificationObserver = Callable[[Any], None]


class _TurnIdleTimeout(RuntimeError):
    """One Codex turn stopped producing SDK notifications."""

    def __init__(self, *, notifications_seen: int, interrupted: bool) -> None:
        super().__init__("codex turn idle timeout")
        self.notifications_seen = notifications_seen
        self.interrupted = interrupted


class _RetryBudgetExceeded(RuntimeError):
    """Codex kept emitting ``will_retry`` beyond the allowed count."""

    def __init__(self, error: TurnError) -> None:
        super().__init__(f"codex exceeded the will_retry budget ({error.category})")
        self.error = error


class CodexHarness(SerializedTurnHarness):
    """Adapt one Codex SDK client and one isolated thread to protocol v1.

    One external Turn is one ``thread.turn()`` streamed to its
    ``turn/completed`` notification.  Steering uses the SDK turn steer
    request; abort maps to a bounded ``interrupt()``.
    """

    card = HarnessCard(
        name=PROVIDER_NAME,
        implementation_version=ADAPTER_VERSION,
        protocol_version=PROTOCOL_VERSION,
        compatible_protocol_versions=frozenset({PROTOCOL_VERSION}),
        capabilities=frozenset(
            {
                HarnessCapability.STEER,
                HarnessCapability.GRACEFUL_ABORT,
                HarnessCapability.PERSISTENT_SESSION,
                HarnessCapability.CHECKPOINT,
                HarnessCapability.MCP_TOOLS,
            }
        ),
        optional_host_capabilities=frozenset(
            {
                HostCapability.TOOL_APPROVAL,
                HostCapability.USER_INPUT,
                HostCapability.CHECKPOINT_SINK,
                HostCapability.MCP_SERVERS,
                HostCapability.PROVIDER_INTERACTION,
            }
        ),
    )

    def __init__(
        self,
        config: CodexHarnessConfig | None = None,
        *,
        notification_observer: NotificationObserver | None = None,
    ) -> None:
        """Bind the provider configuration; the SDK client starts on ``start``.

        Args:
            config: Provider-owned options; defaults use the local Codex CLI.
            notification_observer: Provider-private hook receiving every raw
                SDK notification (observability bridges).  It must not raise
                and it never reaches the public event stream.
        """
        self._config = config or CodexHarnessConfig()
        self._runtime_config = self._config
        self._expected_sandbox: str | None = None
        super().__init__(event_buffer_capacity=self._config.event_buffer_capacity)
        self._notification_observer = notification_observer
        self._sdk: Any = None
        self._client: Any = None
        self._thread: Any = None
        self._thread_id: str | None = None
        self._confirmed_model = ""
        self._permission_fingerprint: str | None = None
        self._runtime_policy_fingerprint: str | None = None
        self._startup_source_fingerprint: str | None = None
        self._native_plugin_fingerprint: str | None = None
        self._active_handle: Any = None
        self._native_turn: _NativeTurnDrain | None = None
        self._closing_process: Any = None
        self._closing_process_scoped = False
        self._closing_client: Any = None
        self._connecting: asyncio.Future[None] | None = None
        self._session_close_lock = asyncio.Lock()
        self._client_close_lock = asyncio.Lock()
        self._pending_steers: list[tuple[str, asyncio.Future[None]]] = []
        self._active_model: CodexModelConfig | None = self._config.model
        self._fallback_activated = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def fallback_activated(self) -> bool:
        """Return whether the authentication fallback endpoint is in use."""
        return self._fallback_activated

    # ------------------------------------------------------------------
    # Provider hooks
    # ------------------------------------------------------------------

    def _validate_context(self, context: HarnessContext) -> None:
        if context.tool_authorizer is not None:
            raise UnsupportedHarnessCapabilityError(
                "Codex 0.144.4 native view_image bypasses host tool approvals; "
                "mandatory per-tool authorization is not supported"
            )
        super()._validate_context(context)
        compiled = compile_runtime_policy(self._config, context.runtime_policy)
        if HostCapability.TOOL_APPROVAL in context.host_capabilities:
            if compiled.config.bypass_approvals_and_sandbox:
                raise HarnessProtocolError("Codex host tool approvals conflict with permission bypass")
            if context.interactions is None:
                raise HarnessProtocolError("Codex host tool approvals require an interaction handler")
        if context.resume_policy is ResumePolicy.REQUIRE_RESUME and context.checkpoint is None:
            raise HarnessProtocolError("Codex cannot resume a thread without a checkpoint")

    async def _open_session(self, context: HarnessContext) -> str | None:
        compiled = compile_runtime_policy(self._config, context.runtime_policy)
        self._runtime_config = compiled.config
        self._expected_sandbox = compiled.expected_sandbox
        config = self._runtime_config
        source_fingerprint = await asyncio.to_thread(validate_startup_sources, config, context)
        process_env = build_process_env(config, context.env)
        plugin_fingerprint = await asyncio.to_thread(
            validate_native_plugin_packages,
            config.native_plugins,
            env=process_env,
            protocol_mcp_names=tuple(server.name for server in context.mcp_servers),
        )
        restored = self._restored_checkpoint_data(context)
        restored_thread = restored.get("thread_id") if restored else None
        resume_thread_id: str | None = None
        if context.resume_policy is not ResumePolicy.NEW and isinstance(restored_thread, str) and restored_thread:
            resume_thread_id = restored_thread
        elif context.resume_policy is ResumePolicy.REQUIRE_RESUME:
            raise HarnessProtocolError("Codex checkpoint does not carry a thread id to resume")
        runtime_policy_fingerprint = context.runtime_policy.fingerprint if context.runtime_policy is not None else None
        self._runtime_policy_fingerprint = runtime_policy_fingerprint
        restored_policy_fingerprint = (
            restored.get("runtime_policy_fingerprint") if resume_thread_id and restored else None
        )
        self._permission_fingerprint = (
            restored.get("permission_fingerprint")
            if resume_thread_id and restored and restored_policy_fingerprint == runtime_policy_fingerprint
            else None
        )
        if self._permission_fingerprint is not None and not isinstance(self._permission_fingerprint, str):
            raise HarnessProtocolError("Codex checkpoint permission fingerprint must be a string")
        if self._permission_fingerprint is not None and HostCapability.TOOL_APPROVAL not in context.host_capabilities:
            raise HarnessProtocolError("Codex permission-bound checkpoint requires host tool approvals")
        restored_sources = restored.get("startup_source_fingerprint") if resume_thread_id and restored else None
        if resume_thread_id and restored_sources != source_fingerprint:
            raise HarnessProtocolError("Codex startup source scope changed since session activation")
        restored_plugins = restored.get("native_plugin_fingerprint") if resume_thread_id and restored else None
        if resume_thread_id and restored_plugins != plugin_fingerprint:
            raise HarnessProtocolError("Codex native plugin snapshot changed since session activation")
        self._startup_source_fingerprint = source_fingerprint
        self._native_plugin_fingerprint = plugin_fingerprint
        await asyncio.to_thread(
            install_skills,
            config.skills,
            provider="codex",
            cwd=context.cwd or config.cwd,
            conflict=config.skill_conflict,
        )
        self._sdk = load_codex_sdk()
        self._loop = asyncio.get_running_loop()
        self._active_model = config.model
        self._fallback_activated = False
        try:
            await self._connect(context, model=self._active_model, resume_thread_id=resume_thread_id)
        except asyncio.CancelledError:
            raise
        except ProviderStartupError:
            raise
        except Exception as exc:
            error = classify_codex_exception(exc)
            if resume_thread_id is not None:
                error = TurnError(
                    message=f"failed to resume Codex thread {resume_thread_id!r}: {error.message}",
                    code=error.code,
                    category=error.category,
                    retryable=error.retryable,
                    provider_data=error.provider_data,
                )
            raise ProviderStartupError(f"Codex startup failed: {type(exc).__name__}", error=error) from exc
        await self._emit_model_changed()
        await self._publish_checkpoint(
            {
                "thread_id": self._thread_id,
                "resumed": resume_thread_id is not None,
                "permission_fingerprint": self._permission_fingerprint,
                "runtime_policy_fingerprint": runtime_policy_fingerprint,
                "startup_source_fingerprint": self._startup_source_fingerprint,
                "native_plugin_fingerprint": self._native_plugin_fingerprint,
            },
            reason=CheckpointReason.SESSION_ACTIVATED,
        )
        return self._thread_id

    async def _connect(
        self,
        context: HarnessContext,
        *,
        model: CodexModelConfig | None,
        resume_thread_id: str | None,
    ) -> None:
        if self._stopping:
            raise HarnessStateError("Codex stopped before connecting")
        connecting = asyncio.get_running_loop().create_future()
        self._connecting = connecting
        try:
            await self._connect_session(context, model=model, resume_thread_id=resume_thread_id)
        finally:
            connecting.set_result(None)

    async def _connect_session(
        self,
        context: HarnessContext,
        *,
        model: CodexModelConfig | None,
        resume_thread_id: str | None,
    ) -> None:
        sdk = self._sdk
        config = self._runtime_config
        source_fingerprint = await asyncio.to_thread(validate_startup_sources, config, context)
        if source_fingerprint != self._startup_source_fingerprint:
            raise HarnessProtocolError("Codex startup source scope changed since session activation")
        cwd = context.cwd or config.cwd
        process_env = build_process_env(config, context.env)
        plugin_fingerprint = await asyncio.to_thread(
            validate_native_plugin_packages,
            config.native_plugins,
            env=process_env,
            protocol_mcp_names=tuple(server.name for server in context.mcp_servers),
        )
        if plugin_fingerprint != self._native_plugin_fingerprint:
            raise HarnessProtocolError("Codex native plugin snapshot changed since session activation")
        codex_config = build_codex_config(
            sdk=sdk,
            config=config,
            model=model,
            cwd=cwd,
            env=process_env,
            mcp_servers=context.mcp_servers,
            enable_user_input=HostCapability.USER_INPUT in context.host_capabilities,
        )
        options = build_thread_options(
            sdk=sdk,
            config=config,
            model=model,
            cwd=cwd,
            system_prompt=context.system_prompt,
        )
        client = sdk.AsyncCodex(config=codex_config)
        try:
            if context.host_capabilities & _INTERACTIVE_HOST_CAPABILITIES:
                _install_approval_handler(client, self._approval_handler)
            if not config.inherit_process_env or sys.platform == "linux":
                isolate_process_environment(client, sdk)
            await validate_native_plugin_inventory(
                client,
                config.native_plugins,
                cwd=cwd,
                env=process_env,
            )
            if source_fingerprint is not None and HostCapability.TOOL_APPROVAL not in context.host_capabilities:
                await validate_effective_startup_sources(
                    client=client,
                    cwd=cwd,
                    allow_native_plugins=config.native_plugins is not None,
                    managed_mcp_names=tuple(server.name for server in context.mcp_servers),
                    expected_mcp_approval_mode="auto",
                    require_permissions=False,
                )
            if config.system_prompt_mode == "append" and context.system_prompt:
                options["developer_instructions"] = await append_developer_instructions(
                    client,
                    sdk,
                    config,
                    cwd=cwd,
                    system_prompt=context.system_prompt,
                )
            confirmed_model = ""
            if HostCapability.TOOL_APPROVAL in context.host_capabilities:
                thread, confirmed_model, fingerprint = await connect_with_host_approvals(
                    client=client,
                    sdk=sdk,
                    options=options,
                    resume_thread_id=resume_thread_id,
                    raw_events=config.experimental_raw_events,
                    expected_fingerprint=self._permission_fingerprint,
                    source_fingerprint=source_fingerprint,
                    allow_native_plugins=config.native_plugins is not None,
                    managed_mcp_names=tuple(server.name for server in context.mcp_servers),
                    expected_sandbox=self._expected_sandbox,
                )
                self._permission_fingerprint = fingerprint
            elif resume_thread_id is not None:
                options.pop("ephemeral", None)
                thread = await client.thread_resume(resume_thread_id, **options)
                resumed_id = getattr(thread, "id", None)
                if resumed_id != resume_thread_id:
                    raise HarnessProtocolError(
                        f"Codex resumed unexpected thread {resumed_id!r}; expected {resume_thread_id!r}"
                    )
                confirmed_model = str(getattr(thread, "model", "") or "")
            elif config.experimental_raw_events:
                thread, confirmed_model = await start_thread_with_raw_events(client=client, sdk=sdk, options=options)
            else:
                thread = await client.thread_start(**options)
                confirmed_model = str(getattr(thread, "model", "") or "")
            await validate_native_plugin_mcp_runtime(
                client,
                config.native_plugins,
                thread_id=str(thread.id),
            )
        except BaseException:
            try:
                await self._close_client(client)
            except Exception:
                # Keep the only process handle reachable when close itself
                # fails; startup rollback will retry this exact client.
                self._client = client
                raise
            raise
        if self._stopping:
            await self._close_client(client)
            raise HarnessStateError("Codex stopped while connecting")
        self._client = client
        self._thread = thread
        self._thread_id = str(thread.id)
        # The App Server response echoes the model it resolved — including
        # members spawned without an explicit model — and it is the value the
        # reliability context reports on retry/failure events. The ``Thread``
        # object itself carries no model field, so keep the confirmed value
        # separate instead of reading it off ``self._thread``.
        self._confirmed_model = confirmed_model

    async def _close_session(self) -> None:
        async with self._session_close_lock:
            connecting = self._connecting
            if connecting is not None:
                await asyncio.wait_for(asyncio.shield(connecting), _DRAIN_TIMEOUT_S)
            client = self._client if self._client is not None else self._closing_client
            native = self._native_turn
            if native is not None:
                await self._drain_native_turn(native, interrupt=True)
            if client is not None:
                await self._close_client(client)
            if self._client is client:
                self._client = None
                self._thread = None
                self._confirmed_model = ""
            if self._native_turn is native:
                self._native_turn = None

    async def _close_client(self, client: Any) -> None:
        # SDK 0.144.4 clears _proc before closing and does not wait after its
        # kill fallback. Keep the exact process reachable across close retries.
        async with self._client_close_lock:
            if self._closing_client is not client:
                transport = getattr(getattr(client, "_client", None), "_sync", None)
                self._closing_client = client
                self._closing_process = getattr(transport, "_proc", None)
                self._closing_process_scoped = bool(getattr(transport, "_jiuwen_process_scope", False))
            await client.close()
            process = self._closing_process
            if process is not None:
                exit_code = await asyncio.to_thread(process.wait, timeout=2)
                if self._closing_process_scoped and exit_code != 0:
                    raise HarnessProtocolError("Codex owned process tree exit is unconfirmed")
            self._closing_process = None
            self._closing_process_scoped = False
            self._closing_client = None

    async def _drain_native_turn(self, native: _NativeTurnDrain, *, interrupt: bool) -> None:
        # Neither a cancelled waiter nor an SDK error may discard the original
        # reader/client. A later stop retries this same owner.
        await asyncio.wait_for(native.started.wait(), _DRAIN_TIMEOUT_S)
        if interrupt and not native.confirmed and native.handle is not None:
            await self._interrupt_handle(native.handle)
        if native.reader is not None:
            await asyncio.wait_for(asyncio.shield(native.reader), _DRAIN_TIMEOUT_S)
        if not native.confirmed:
            raise HarnessProtocolError("Codex native turn exit is unconfirmed")

    async def _execute_turn(self, turn: PendingTurn) -> tuple[TurnEventKind, TurnResult]:
        try:
            return await self._execute_codex_turn(turn)
        finally:
            self._reject_pending_steers()

    def _reject_pending_steers(self) -> None:
        pending, self._pending_steers = self._pending_steers, []
        for _, accepted in pending:
            if not accepted.done():
                accepted.set_exception(HarnessStateError("Codex turn ended before steer acknowledgement"))

    async def _execute_codex_turn(self, turn: PendingTurn) -> tuple[TurnEventKind, TurnResult]:
        timing = TurnTiming()
        text = harness_input_text(turn.content)
        accumulator = CodexTurnAccumulator(turn_id=turn.turn_id)
        if self._startup_source_fingerprint is not None:
            try:
                current = await asyncio.to_thread(validate_startup_sources, self._runtime_config, self._context)
                if current != self._startup_source_fingerprint:
                    raise HarnessProtocolError("Codex startup source scope changed since session activation")
            except Exception as exc:
                await self._close_session()
                error = classify_codex_exception(exc)
                return TurnEventKind.FAILED, accumulator.build_failed_result(error, timing=timing)
        if self._native_plugin_fingerprint is not None:
            try:
                current_plugins = await asyncio.to_thread(
                    validate_native_plugin_packages,
                    self._runtime_config.native_plugins,
                    env=build_process_env(self._runtime_config, self._context.env),
                    protocol_mcp_names=tuple(server.name for server in self._context.mcp_servers),
                )
                if current_plugins != self._native_plugin_fingerprint:
                    raise HarnessProtocolError("Codex native plugin snapshot changed since session activation")
            except Exception as exc:
                await self._close_session()
                error = classify_codex_exception(exc)
                return TurnEventKind.FAILED, accumulator.build_failed_result(error, timing=timing)
        # A failed rollback leaves no usable client. Retry only when a new
        # accepted input arrives; never replay a failed turn in the background.
        if self._client is None and not turn.abort_requested:
            try:
                await self._connect(self._context, model=self._active_model, resume_thread_id=self._thread_id)
            except Exception as exc:
                self._reject_pending_steers()
                if turn.abort_requested:
                    return TurnEventKind.ABORTED, interrupted_result(turn, provider_name=PROVIDER_NAME, timing=timing)
                error = exc.error if isinstance(exc, ProviderStartupError) else classify_codex_exception(exc)
                return TurnEventKind.FAILED, accumulator.build_failed_result(error, timing=timing)
        if turn.abort_requested:
            self._reject_pending_steers()
            await self._close_session()
            return TurnEventKind.ABORTED, interrupted_result(turn, provider_name=PROVIDER_NAME, timing=timing)
        for _attempt in range(2):
            idle_retries = 0
            try:
                while True:
                    try:
                        await self._run_turn(turn, text, accumulator)
                    except _TurnIdleTimeout as exc:
                        can_retry = (
                            idle_retries < self._config.turn_idle_retries
                            and exc.notifications_seen == 0
                            and exc.interrupted
                            and not turn.abort_requested
                        )
                        if not can_retry:
                            raise
                        idle_retries += 1
                        logger.warning(
                            "[codex] turn was silent for %ss; retrying prompt on the same thread (%s/%s)",
                            self._config.turn_idle_timeout_s,
                            idle_retries,
                            self._config.turn_idle_retries,
                        )
                        continue
                    break
            except Exception as exc:
                if turn.abort_requested:
                    return TurnEventKind.ABORTED, interrupted_result(
                        turn,
                        provider_name=PROVIDER_NAME,
                        timing=timing,
                        messages=tuple(accumulator.messages),
                        final_output=accumulator.last_text_output,
                        usage=accumulator.total_usage,
                    )
                if isinstance(exc, _RetryBudgetExceeded):
                    error = exc.error
                    if await self._maybe_activate_fallback(error, accumulator, turn):
                        accumulator = CodexTurnAccumulator(turn_id=turn.turn_id)
                        continue
                elif isinstance(exc, _TurnIdleTimeout):
                    error = TurnError(
                        message=f"Codex produced no turn events for {self._config.turn_idle_timeout_s:g}s",
                        code="CODEX_TURN_IDLE_TIMEOUT",
                        category="network_timeout",
                        retryable=True,
                    )
                else:
                    error = classify_codex_exception(exc)
                    if await self._maybe_activate_fallback(error, accumulator, turn):
                        continue
                if turn.abort_requested:
                    return TurnEventKind.ABORTED, interrupted_result(turn, provider_name=PROVIDER_NAME, timing=timing)
                return TurnEventKind.FAILED, accumulator.build_failed_result(error, timing=timing)
            kind, result = accumulator.build_terminal_result(turn=turn, timing=timing)
            if kind is TurnEventKind.FAILED and await self._maybe_activate_fallback(result.error, accumulator, turn):
                accumulator = CodexTurnAccumulator(turn_id=turn.turn_id)
                continue
            if turn.abort_requested:
                return TurnEventKind.ABORTED, interrupted_result(
                    turn,
                    provider_name=PROVIDER_NAME,
                    timing=timing,
                    messages=tuple(accumulator.messages),
                    final_output=accumulator.last_text_output,
                    usage=accumulator.total_usage,
                )
            return kind, result
        error = TurnError(
            message="Codex authentication fallback did not recover the turn",
            code="CODEX_FALLBACK_EXHAUSTED",
            category="auth_required",
        )
        return TurnEventKind.FAILED, accumulator.build_failed_result(error, timing=timing)

    async def _run_turn(self, turn: PendingTurn, text: str, accumulator: CodexTurnAccumulator) -> None:
        previous = self._native_turn
        if previous is not None:
            await self._drain_native_turn(previous, interrupt=False)
        if turn.abort_requested:
            raise HarnessStateError("Codex turn was cancelled before dispatch")
        thread = self._thread
        if thread is None:
            raise HarnessProtocolError("Codex thread disappeared during an active cycle")
        native = _NativeTurnDrain(client=self._client, failure=asyncio.get_running_loop().create_future())
        self._native_turn = native
        native.reader = asyncio.create_task(
            self._read_native_turn(native, thread, turn, text, accumulator),
            name=f"codex_native_turn[{turn.turn_id}]",
        )
        try:
            await asyncio.wait((native.reader, native.failure), return_when=asyncio.FIRST_COMPLETED)
            if native.failure.done():
                await self._drain_native_turn(native, interrupt=False)
                raise native.failure.result()
            await native.reader
            if not native.confirmed:
                raise HarnessProtocolError("Codex turn stream ended without matching native completion")
        finally:
            if not native.confirmed:
                native.project_events = False

    async def _read_native_turn(
        self,
        native: _NativeTurnDrain,
        thread: Any,
        turn: PendingTurn,
        text: str,
        accumulator: CodexTurnAccumulator,
    ) -> None:
        handle = None
        try:
            handle = await thread.turn(text)
            native.handle = handle
            self._active_handle = handle
            native.started.set()
            if turn.abort_requested:
                await self._interrupt_handle(handle)
            pending_steers, self._pending_steers = self._pending_steers, []
            for steer_text, accepted in pending_steers:
                if accepted.done():
                    continue
                try:
                    if turn.abort_requested or native.failure.done():
                        raise HarnessStateError("Codex turn is no longer accepting steer")
                    await self._steer_handle(handle, steer_text)
                except _SteerNotAccepted as exc:
                    # The original turn may have completed successfully before
                    # its handle arrived. Keep its only reader and result intact.
                    if not accepted.done():
                        accepted.set_exception(exc)
                except Exception as exc:
                    if not accepted.done():
                        accepted.set_exception(exc)
                    if not native.failure.done():
                        native.failure.set_result(exc)
                        await self._interrupt_handle(handle)
                else:
                    if not accepted.done():
                        accepted.set_result(None)
            will_retry_count = 0
            terminal_seen = False
            stream = handle.stream().__aiter__()
            while True:
                pending = asyncio.create_task(anext(stream))
                try:
                    if native.failure.done():
                        notification = await pending
                    else:
                        try:
                            notification = await asyncio.wait_for(
                                asyncio.shield(pending),
                                timeout=self._config.turn_idle_timeout_s,
                            )
                        except asyncio.TimeoutError:
                            interrupted = await self._interrupt_handle(handle)
                            native.failure.set_result(
                                _TurnIdleTimeout(
                                    notifications_seen=accumulator.notifications_seen,
                                    interrupted=interrupted,
                                )
                            )
                            # Keep the same SDK read alive: cancelling anext
                            # unregisters its queue while to_thread can keep
                            # consuming the only terminal notification.
                            notification = await pending
                except StopAsyncIteration:
                    native.confirmed = terminal_seen
                    break
                self._observe(notification)
                if str(getattr(notification, "method", "")) == "turn/completed":
                    completed = getattr(getattr(notification, "payload", None), "turn", None)
                    if str(getattr(completed, "id", "")) != str(handle.id):
                        continue
                    terminal_seen = True
                mapped_events, retrying = accumulator.consume(notification)
                for mapped in mapped_events:
                    if native.project_events:
                        await self._emit(mapped.payload, turn=turn, item_id=mapped.item_id)
                if retrying is not None:
                    will_retry_count += 1
                    if will_retry_count > self._config.max_will_retry_count and not native.failure.done():
                        await self._interrupt_handle(handle)
                        native.failure.set_result(_RetryBudgetExceeded(retrying.error))
        finally:
            native.started.set()
            if native.confirmed and self._active_handle is handle:
                self._active_handle = None
            self._reject_pending_steers()

    def _observe(self, notification: Any) -> None:
        observer = self._notification_observer
        if observer is None:
            return
        try:
            observer(notification)
        except Exception:
            logger.exception("[codex] notification observer raised")

    async def send(
        self,
        content: HarnessInput,
        *,
        mode: DeliveryMode = DeliveryMode.AUTO,
    ) -> SendReceipt:
        try:
            return await super().send(content, mode=mode)
        except _SteerNotAccepted:
            # Only explicit native non-acceptance permits a new input. The base
            # queue still owns ordering, lifecycle and the truthful receipt.
            if mode is not DeliveryMode.STEER:
                raise
            return await super().send(content, mode=DeliveryMode.FOLLOW_UP)

    async def _steer(self, turn: PendingTurn, content: HarnessInput) -> None:
        text = harness_input_text(content)
        handle = self._active_handle
        if handle is None:
            if self._active_turn is not turn or turn.abort_requested:
                raise HarnessStateError("there is no active Codex turn to steer")
            # ``thread.turn()`` has not returned yet; ``_run_turn`` flushes the
            # queue as soon as the SDK handle exists.
            accepted = asyncio.get_running_loop().create_future()
            self._pending_steers.append((text, accepted))
            await accepted
            return
        await self._steer_handle(handle, text)

    async def _steer_handle(self, handle: Any, text: str) -> None:
        try:
            await handle.steer(text)
        except Exception as exc:
            if _is_no_active_turn_to_steer(exc):
                raise _SteerNotAccepted("the Codex turn ended before the steer was accepted") from exc
            raise

    async def _interrupt_turn(self, turn: PendingTurn, mode: AbortMode) -> None:
        _ = turn, mode
        handle = self._active_handle
        if handle is not None:
            await self._interrupt_handle(handle)

    async def _interrupt_handle(self, handle: Any) -> bool:
        try:
            await asyncio.wait_for(handle.interrupt(), timeout=_INTERRUPT_TIMEOUT_S)
            return True
        except Exception as exc:
            logger.warning("[codex] turn interrupt failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Authentication fallback
    # ------------------------------------------------------------------

    def _fallback_applies(
        self,
        error: TurnError | None,
        fallback: CodexModelConfig | None,
        turn: PendingTurn,
    ) -> bool:
        """Report whether the auth fallback may still replace this turn.

        The category itself is the gate: an ``auth_required`` failure means the
        request never reached the model on the native endpoint, so replaying
        the turn on the fallback cannot duplicate meaningful work. A mid-turn
        token expiry may have emitted partial output before failing; replaying
        it is still preferred over failing the turn, and no worse than the
        manual retry the caller would perform anyway.

        Args:
            error: The failure classified so far, when there is one.
            fallback: The configured fallback endpoint, when there is one.
            turn: The turn being considered for a restart.

        Returns:
            True when every precondition for activating the fallback holds.
        """
        if error is None or fallback is None:
            return False
        if error.category != "auth_required" or self._fallback_activated:
            return False
        return not turn.abort_requested

    async def _emit_model_changed(self) -> None:
        """Announce the model the Codex thread actually runs on.

        ``RuntimeReliabilityContext`` reports the model on external-runtime
        failure messages; without this event it stays empty and failures say
        ``model=<unknown>`` even though the thread is serving requests. The
        App Server confirms the resolved model on the thread start/resume
        response — including members spawned without an explicit
        ``model_name`` — and ``model/rerouted`` notifications update it when
        the server re-routes mid-session.
        """
        model = self._confirmed_model
        if not model:
            configured = self._active_model
            model = configured.model if configured is not None else ""
        await self._emit(
            ProviderEvent(
                provider=PROVIDER_NAME,
                event_type=_MODEL_CHANGED_EVENT,
                schema_version="1",
                payload={"model": model} if model else {},
            ),
        )

    async def _maybe_activate_fallback(
        self,
        error: TurnError | None,
        accumulator: CodexTurnAccumulator,
        turn: PendingTurn,
    ) -> bool:
        fallback = self._config.fallback_model
        if not self._fallback_applies(error, fallback, turn):
            return False
        context = self._context
        if context is None:
            return False
        thread_id = self._thread_id
        await self._close_session()
        if turn.abort_requested:
            return False
        try:
            await self._connect(context, model=fallback, resume_thread_id=thread_id)
        except Exception as exc:
            logger.warning("[codex] authentication fallback activation failed: %s", exc)
            return False
        if turn.abort_requested:
            await self._close_session()
            return False
        ratified = await self._confirm_provider_extension(
            AUTH_FALLBACK_REQUEST_TYPE,
            {"model": fallback.model, "provider": fallback.provider, "api_base": fallback.api_base},
        )
        if turn.abort_requested:
            await self._close_session()
            return False
        if not ratified:
            # The host could not persist the switch; resume the thread on the
            # native endpoint so the member does not run on an unrecorded one.
            logger.warning("[codex] host declined the authentication fallback; restoring the native endpoint")
            await self._close_session()
            if turn.abort_requested:
                return False
            try:
                await self._connect(context, model=self._runtime_config.model, resume_thread_id=thread_id)
            except Exception as exc:
                logger.warning("[codex] restoring the native endpoint failed: %s", exc)
                return False
            if turn.abort_requested:
                await self._close_session()
                return False
            self._active_model = self._runtime_config.model
            self._fallback_activated = False
            await self._emit_model_changed()
            return False
        self._active_model = fallback
        self._fallback_activated = True
        await self._emit_model_changed()
        await self._publish_checkpoint(
            {
                "thread_id": self._thread_id,
                "resumed": True,
                "fallback": True,
                "permission_fingerprint": self._permission_fingerprint,
                "runtime_policy_fingerprint": self._runtime_policy_fingerprint,
                "startup_source_fingerprint": self._startup_source_fingerprint,
                "native_plugin_fingerprint": self._native_plugin_fingerprint,
            },
            reason=CheckpointReason.STATE_CHANGED,
        )
        await self._emit(
            ProviderEvent(
                provider=PROVIDER_NAME,
                event_type="auth_fallback_activated",
                schema_version="1",
                payload={"model": fallback.model, "provider": fallback.provider, "api_base": fallback.api_base},
            ),
            turn=turn,
        )
        return True

    # ------------------------------------------------------------------
    # Approval routing (runs on the SDK reader thread)
    # ------------------------------------------------------------------

    def _approval_handler(self, method: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """Answer App Server requests: tool approvals and ``request_user_input``."""
        if method == USER_INPUT_METHOD:
            return self._handle_user_input_request(params or {})
        mcp_approval = method == "mcpServer/elicitation/request"
        decline = {"action": "decline"} if mcp_approval else {"decision": "decline"}
        if mcp_approval:
            meta = (params or {}).get("_meta", {})
            if (
                (params or {}).get("mode") != "form"
                or not isinstance(meta, Mapping)
                or meta.get("codex_approval_kind") != "mcp_tool_call"
                or not (params or {}).get("serverName")
            ):
                return decline
        elif method not in _APPROVAL_METHODS:
            return {}
        loop = self._loop
        if loop is None or loop.is_closed():
            return decline
        future = asyncio.run_coroutine_threadsafe(self._route_approval(method, params or {}), loop)
        try:
            return future.result(timeout=_APPROVAL_WAIT_TIMEOUT_S)
        except Exception as exc:
            future.cancel()
            logger.warning("[codex] approval routing for %s failed: %s", method, exc)
            return decline

    def _handle_user_input_request(self, params: Mapping[str, Any]) -> dict[str, Any]:
        context = self._context
        loop = self._loop
        if context is None or HostCapability.USER_INPUT not in context.host_capabilities:
            logger.warning("[codex] request_user_input arrived without a USER_INPUT host; answering nothing")
            return _EMPTY_USER_INPUT_ANSWER
        if loop is None or loop.is_closed():
            return _EMPTY_USER_INPUT_ANSWER
        future = asyncio.run_coroutine_threadsafe(self._route_user_input(params), loop)
        while True:
            try:
                return future.result(timeout=_USER_INPUT_WAIT_SLICE_S)
            except TimeoutError:
                if loop.is_closed():
                    future.cancel()
                    return _EMPTY_USER_INPUT_ANSWER
            except Exception as exc:
                logger.warning("[codex] user input routing failed: %s", exc)
                return _EMPTY_USER_INPUT_ANSWER

    async def _route_user_input(self, params: Mapping[str, Any]) -> dict[str, Any]:
        active = self._active_turn
        item_id = str(params.get("itemId") or params.get("item_id") or "codex-user-input")
        questions = _user_input_questions(params)
        if not questions:
            return _EMPTY_USER_INPUT_ANSWER
        request = UserInputRequest(
            request_id=f"codex-ask:{item_id}",
            prompt=_render_questions(questions),
            provider_session_id=self._thread_id,
            turn_id=active.turn_id if active is not None else None,
            choices=_first_choices(questions),
            provider_data={
                "tool_name": "request_user_input",
                "call_id": item_id,
                "questions": to_json_safe(questions),
                "is_blocking": bool(params.get("isBlocking", True)),
            },
        )
        response = await self._request_interaction(request)
        if response is None or response.status is not InteractionResponseStatus.COMPLETED:
            return _EMPTY_USER_INPUT_ANSWER
        answers = _answers_from_response(json_value_to_builtin(response.content), questions)
        return {"answers": {question_id: {"answers": values} for question_id, values in answers.items()}}

    async def _route_approval(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        active = self._active_turn
        mcp_approval = method == "mcpServer/elicitation/request"
        item_id = str(params.get("itemId") or params.get("item_id") or "codex-approval")
        if mcp_approval:
            item_id = f"mcp-{uuid4().hex}"
        arguments = {key: value for key, value in to_json_object(params).items() if key not in {"threadId", "turnId"}}
        request = ToolApprovalRequest(
            request_id=f"codex-approval:{item_id}",
            call_id=item_id,
            tool_name=(
                f"mcp__{params['serverName']}"
                if mcp_approval
                else "apply_patch"
                if method == "item/fileChange/requestApproval"
                else "shell"
            ),
            arguments=arguments,
            description=str(params.get("message") or "") if mcp_approval else None,
            provider_session_id=self._thread_id,
            turn_id=active.turn_id if active is not None else None,
            provider_data={"method": method},
        )
        context = self.context
        authorization = BeforeToolContext(
            agent_name=context.agent_name if context else "",
            provider_session_id=self._thread_id,
            turn_id=request.turn_id,
            call_id=item_id,
            tool_name=request.tool_name,
            arguments=arguments,
        )
        response = await self._request_interaction(request) if await self._authorize_tool(authorization) else None
        allowed = (
            response is not None
            and response.updated_arguments is None
            and response.decision
            in (
                ToolApprovalDecision.ALLOW,
                ToolApprovalDecision.ALLOW_FOR_SESSION,
            )
        )
        if allowed:
            allowed = await self._authorize_tool(authorization)
        if active is not None and (active.abort_requested or active.stop_requested):
            allowed = False
        if mcp_approval:
            return {"action": "accept", "content": {}} if allowed else {"action": "decline"}
        if allowed:
            return {"decision": "accept"}
        return {"decision": "decline"}


_EMPTY_USER_INPUT_ANSWER: dict[str, Any] = {"answers": {}}


def _user_input_questions(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the ``request_user_input`` questions as plain dicts (id, question, options...)."""
    raw = params.get("questions")
    if not isinstance(raw, list):
        return []
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        question = dict(to_json_object(item))
        question.setdefault("id", f"q{index}")
        questions.append(question)
    return questions


def _render_questions(questions: list[dict[str, Any]]) -> str:
    """Render the questions as one prompt; options are listed by label."""
    lines: list[str] = []
    for question in questions:
        header = str(question.get("header") or "").strip()
        text = str(question.get("question") or "").strip()
        lines.append(f"{header}: {text}" if header and text else header or text or "Input requested")
        options = question.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, Mapping) or not option.get("label"):
                    continue
                description = str(option.get("description") or "").strip()
                label = str(option["label"])
                lines.append(f"  - {label}: {description}" if description else f"  - {label}")
    return "\n".join(lines)


def _first_choices(questions: list[dict[str, Any]]) -> tuple[str, ...]:
    options = questions[0].get("options") if questions else None
    if not isinstance(options, list):
        return ()
    return tuple(str(item.get("label")) for item in options if isinstance(item, Mapping) and item.get("label"))


def _answers_from_response(content: Any, questions: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Normalize a host user-input response into per-question answer lists.

    Hosts may answer with a mapping keyed by question id or question text,
    a positional list, a ``{"answer": ...}`` object, or a bare scalar for the
    first question.
    """
    ids = [str(question["id"]) for question in questions]
    by_text = {str(question.get("question") or ""): question_id for question, question_id in zip(questions, ids)}
    if isinstance(content, Mapping):
        answers = content.get("answers")
        if isinstance(answers, Mapping):
            return _keyed_answers(answers, ids, by_text)
        if "answer" in content:
            return {ids[0]: _answer_values(content["answer"])}
        return _keyed_answers(content, ids, by_text)
    if isinstance(content, list):
        return {question_id: _answer_values(value) for question_id, value in zip(ids, content)}
    if content is None:
        return {}
    return {ids[0]: _answer_values(content)}


def _keyed_answers(mapping: Mapping[Any, Any], ids: list[str], by_text: Mapping[str, str]) -> dict[str, list[str]]:
    answers: dict[str, list[str]] = {}
    for key, value in mapping.items():
        key_text = str(key)
        question_id = key_text if key_text in ids else by_text.get(key_text)
        if question_id is not None:
            answers[question_id] = _answer_values(value)
    return answers


def _answer_values(value: Any) -> list[str]:
    if isinstance(value, Mapping) and isinstance(value.get("answers"), list):
        return [str(item) for item in value["answers"]]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _install_approval_handler(client: Any, handler: Callable[[str, Mapping[str, Any] | None], dict[str, Any]]) -> None:
    """Route App Server approval requests to ``handler`` on the low-level client.

    The high-level ``AsyncCodex`` never exposes the approval handler; it lives
    on the wrapped synchronous ``CodexClient``. An incompatible SDK must fail
    before thread creation instead of keeping its default accept-all handler.
    """
    low_level = getattr(getattr(client, "_client", None), "_sync", None)
    if low_level is None or not hasattr(low_level, "_approval_handler"):
        raise HarnessProtocolError("Codex SDK does not expose the required host approval handler")
    low_level._approval_handler = handler
    if low_level._approval_handler != handler:
        raise HarnessProtocolError("Codex SDK did not retain the required host approval handler")


def _is_no_active_turn_to_steer(exc: Exception) -> bool:
    """Return whether Codex rejected steer because its turn already ended."""
    message = getattr(exc, "message", None)
    return (
        getattr(exc, "code", None) == _NO_ACTIVE_TURN_ERROR_CODE
        and isinstance(message, str)
        and _NO_ACTIVE_TURN_ERROR_MESSAGE in message.lower()
    )


__all__ = ["ADAPTER_VERSION", "USER_INPUT_METHOD", "CodexHarness", "NotificationObserver"]
