# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Single subagent instance with a serial asyncio worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.common.logging import logger
from openjiuwen.harness.execution_subject import ExecutionSubject, execution_subject_scope
from openjiuwen.harness.subagent_runtime.models import (
    ShutdownOp,
    SubagentOp,
    SubagentStatus,
    SubagentStatusKind,
    UserInputOp,
)
from openjiuwen.harness.subagent_runtime.native_execution import NativeSubagentExecution
from openjiuwen.harness.subagent_runtime.ports import (
    SubagentExecution,
    SubagentTurnRequest,
    SubagentTurnResult,
)
from openjiuwen.harness.subagent_runtime.status import StatusChannel, StatusReceiver


class SubagentInstance:
    """One live subagent with a per-turn session factory and serial worker."""

    def __init__(
        self,
        *,
        subagent_id: str,
        subagent_type: str,
        display_name: str,
        role: str,
        parent_session_id: str,
        parent_subject_id: str = "main",
        execution: SubagentExecution | None = None,
        agent: Any | None = None,
        session_factory: Callable[[], Any] | None = None,
        running_semaphore: asyncio.Semaphore,
        on_chunk: Callable[[Any], Awaitable[None]] | None = None,
        turn_timeout_s: float | None = None,
        include_parent_session_id: bool = False,
        on_turn_start: Callable[[Any], Awaitable[None]] | None = None,
        on_turn_finished: Callable[[Any, bool], Awaitable[None]] | None = None,
        on_status_changed: Callable[[SubagentStatus], Awaitable[None]] | None = None,
        on_turn_stream_start: Callable[[UserInputOp], Awaitable[None]] | None = None,
        on_turn_stream_end: Callable[[UserInputOp, SubagentTurnResult], Awaitable[None]] | None = None,
    ) -> None:
        self.subagent_id = subagent_id
        self.subagent_type = subagent_type
        self.display_name = display_name
        self.role = role
        self.parent_session_id = parent_session_id
        self.execution_subject = ExecutionSubject(
            subject_id=f"subagent:{subagent_id}",
            display_name=display_name,
            kind="subagent",
            parent_subject_id=parent_subject_id,
            session_id=subagent_id,
        )

        self.status = StatusChannel()
        self.last_output: str | None = None
        self.last_task_id: str | None = None
        self.current_task_id: str | None = None

        if execution is None:
            if agent is None or session_factory is None:
                raise TypeError("execution or legacy agent/session_factory is required")
            execution = NativeSubagentExecution(
                agent=agent,
                session_factory=session_factory,
                subagent_id=subagent_id,
                parent_session_id=parent_session_id,
                include_parent_session_id=include_parent_session_id,
                on_turn_start=on_turn_start,
                on_turn_finished=on_turn_finished,
            )
        self._execution = execution
        # Compatibility-only inspection hook for callers that historically
        # observed the Native child. Runtime behavior goes through _execution.
        self._agent = getattr(execution, "_agent", agent)
        self._on_chunk = on_chunk
        self._turn_timeout_s = turn_timeout_s
        self._on_status_changed = on_status_changed
        self._on_turn_stream_start = on_turn_stream_start
        self._on_turn_stream_end = on_turn_stream_end

        self._ops: asyncio.Queue[SubagentOp] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._current_run: asyncio.Task[None] | None = None
        self._turn_claimed = False
        self._running_semaphore = running_semaphore
        self._interrupt_requested = False
        self._closed = False
        self._shutdown_lock = asyncio.Lock()

    def agent_status(self) -> SubagentStatus:
        return self.status.current()

    def revision(self) -> int:
        return self.status.version()

    def subscribe_status(self) -> StatusReceiver:
        return self.status.subscribe()

    async def enqueue(self, op: SubagentOp) -> None:
        await self._ops.put(op)

    async def interrupt(self) -> bool:
        run = self._current_run
        if run is None or run.done():
            return False
        self._interrupt_requested = True
        run.cancel()
        await asyncio.wait({run})
        return True

    async def start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_main())

    async def shutdown(self, reason: str) -> None:
        async with self._shutdown_lock:
            if self._closed:
                return
            if self._worker_task is not None and self._worker_task.done():
                # A prior ShutdownOp may have reached the execution close and
                # failed.  Retry the same retained execution directly; never
                # enqueue onto a worker that has already exited.
                await self._handle_shutdown(reason)
                return
            await self.interrupt()
            await self.enqueue(ShutdownOp(reason=reason))
            if self._worker_task is not None:
                await self._worker_task

    def is_evictable(self) -> bool:
        if self._closed:
            return False
        kind = self.status.current().kind
        if kind == SubagentStatusKind.RUNNING:
            return False
        if self._current_run is not None and not self._current_run.done():
            if kind in {SubagentStatusKind.RUNNING, SubagentStatusKind.PENDING_INIT}:
                return False
        return self._ops.empty()

    def is_closed(self) -> bool:
        return self._closed

    def has_pending_work(self) -> bool:
        """Return True when a turn is active or queued after a prior final status."""
        if self._closed:
            return False
        if not self._ops.empty():
            return True
        run = self._current_run
        if run is not None and not run.done():
            return True
        kind = self.status.current().kind
        return kind in {SubagentStatusKind.PENDING_INIT, SubagentStatusKind.RUNNING}

    def has_active_turn(self) -> bool:
        """Return True when a user-input turn is queued or currently executing."""
        if self._turn_claimed or not self._ops.empty():
            return True
        run = self._current_run
        return run is not None and not run.done()

    async def _set_status(self, status: SubagentStatus) -> None:
        await self.status.set(status)
        if self._on_status_changed is not None:
            await self._on_status_changed(status)

    async def _worker_main(self) -> None:
        while True:
            op = await self._ops.get()
            try:
                if isinstance(op, ShutdownOp):
                    await self._handle_shutdown(op.reason)
                    return
                await self._handle_user_input(op)
            finally:
                self._ops.task_done()

    async def _handle_user_input(self, op: UserInputOp) -> None:
        # Claim the turn before waiting for the shared concurrency slot. During
        # that wait the op is no longer queued and _current_run does not exist
        # yet, so both signals alone would incorrectly report the instance idle.
        self._turn_claimed = True
        self.current_task_id = op.task_id
        try:
            async with self._running_semaphore:
                self._interrupt_requested = False
                await self._set_status(SubagentStatus.running())
                self._current_run = asyncio.create_task(self._run_one_turn(op))
                try:
                    if self._turn_timeout_s and self._turn_timeout_s > 0:
                        await asyncio.wait_for(self._current_run, timeout=self._turn_timeout_s)
                    else:
                        await self._current_run
                except asyncio.TimeoutError:
                    await self._on_turn_timeout()
                except asyncio.CancelledError as exc:
                    await self._on_turn_cancelled(exc)
                except Exception as exc:
                    logger.warning(
                        "[SubagentInstance] turn failed: subagent_id=%s error=%s",
                        self.subagent_id,
                        exc,
                        exc_info=True,
                    )
                    if not self.status.current().is_final():
                        await self._set_status(SubagentStatus.errored(str(exc)))
                finally:
                    self._current_run = None
        finally:
            self._turn_claimed = False

    async def _on_turn_timeout(self) -> None:
        if not self.status.current().is_final():
            await self._set_status(
                SubagentStatus.errored("turn timeout", code="TIMEOUT"),
            )

    async def _on_turn_cancelled(self, exc: asyncio.CancelledError) -> None:
        if not self.status.current().is_final():
            await self._set_status(SubagentStatus.interrupted())

        if self._interrupt_requested:
            self._interrupt_requested = False
            return

        run = self._current_run
        if run is not None and not run.done():
            run.cancel()
        raise exc

    async def _run_one_turn(self, op: UserInputOp) -> None:
        owner_root = self._register_observability_owner()
        try:
            with execution_subject_scope(self.execution_subject):
                try:
                    if self._on_turn_stream_start is not None:
                        await self._on_turn_stream_start(op)

                    settled = False

                    async def on_result(result: SubagentTurnResult) -> None:
                        nonlocal settled
                        if settled:
                            message = "subagent execution settled a turn more than once"
                            await self._set_status(SubagentStatus.errored(message))
                            raise RuntimeError(message)
                        # Drain the turn tail before settling: the terminal status doubles as
                        # the turn-end signal, so nothing may be emitted after it.
                        if self._on_turn_stream_end is not None:
                            await self._on_turn_stream_end(op, result)
                        await self._settle_turn(op, result)
                        settled = True

                    await self._execution.run_turn(
                        SubagentTurnRequest(task_id=op.task_id, query=op.query),
                        on_chunk=self._on_chunk,
                        on_result=on_result,
                    )
                    if not settled:
                        raise RuntimeError("subagent execution returned without a turn result")
                except BaseError as exc:
                    if not self.status.current().is_final():
                        await self._set_status(
                            SubagentStatus.errored(str(exc), code=exc.status.name),
                        )
                    raise
                except Exception as exc:
                    if not self.status.current().is_final():
                        await self._set_status(SubagentStatus.errored(str(exc)))
                    raise
        finally:
            self._unregister_observability_owner(owner_root)

    def _register_observability_owner(self) -> Any | None:
        """Alias the parent run root to this subagent's isolated session."""
        try:
            from openjiuwen.extensions.observability.span_context import get_root_span
            from openjiuwen.harness.observability.span_context import register_run_root_span

            owner_root = get_root_span(session_id=self.parent_session_id)
            if owner_root is None or not owner_root.is_recording():
                return None
            register_run_root_span(owner_root, session_id=self.subagent_id)
            return owner_root
        except Exception as exc:
            logger.debug(
                "[SubagentInstance] Failed to bind observability owner: %s",
                exc,
            )
            return None

    def _unregister_observability_owner(self, owner_root: Any | None) -> None:
        """Remove the child-session root alias after all turn callbacks finish."""
        if owner_root is None:
            return
        try:
            from openjiuwen.harness.observability.span_context import unregister_run_root_span

            unregister_run_root_span(owner_root, session_id=self.subagent_id)
        except Exception as exc:
            logger.debug(
                "[SubagentInstance] Failed to unbind observability owner: %s",
                exc,
            )

    async def _settle_turn(self, op: UserInputOp, result: SubagentTurnResult) -> None:
        if result.is_error:
            await self._set_status(
                SubagentStatus.errored(
                    result.output or "subagent execution reported error",
                    code=result.error_code,
                ),
            )
            return
        self.last_output = result.output
        self.last_task_id = op.task_id
        await self._set_status(SubagentStatus.completed(result.output))

    async def _handle_shutdown(self, reason: str) -> None:
        if self._closed:
            return
        run = self._current_run
        if run is not None and not run.done():
            self._interrupt_requested = True
            run.cancel()
            await asyncio.wait({run})
        try:
            await self._execution.close(reason)
        except Exception as exc:
            logger.warning(
                "[SubagentInstance] execution close failed: subagent_id=%s error=%s",
                self.subagent_id,
                exc,
                exc_info=True,
            )
            raise
        await self._set_status(SubagentStatus.closed(reason))
        await self.status.close()
        self._closed = True
