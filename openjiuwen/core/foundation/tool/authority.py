# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Mandatory authorization at the final Native invocation boundary.

Bindings belong to one AbilityManager call, not an agent, Turn, or wire payload.
The authorization proof covers final callbacks; the execution certificate covers
only the registered original method after those callbacks have succeeded.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from weakref import ref

from openjiuwen.harness_protocol import BeforeToolContext, json_value_to_builtin

ToolAuthorizer = Callable[[BeforeToolContext], Awaitable[bool | None]]
_DENIED = "[PERMISSION_DENIED] Final tool authority rejected the operation"


@dataclass(frozen=True, slots=True)
class _HostSource:
    operation: BeforeToolContext
    current: Callable[[], bool]
    task: asyncio.Task
    runtime_kwargs: Mapping
    kwargs_snapshot: Mapping

    def is_current(self) -> bool:
        """Require the original live task, source and runtime dependencies."""
        return asyncio.current_task() is self.task and self.is_current_origin()

    def is_current_origin(self) -> bool:
        """Recheck an existing source, never admit another host invocation."""
        return (
            not self.task.done()
            and not self.task.cancelling()
            and self.current() is True
            and self.runtime_kwargs.keys() == self.kwargs_snapshot.keys()
            and all(value is self.runtime_kwargs[key] for key, value in self.kwargs_snapshot.items())
        )


@dataclass
class _Call:
    ctx: Any
    agent: Any
    session: Any
    callbacks: list[ToolAuthorizer] = field(default_factory=list)
    active: bool = True
    inherited: bool = False
    protected: bool = False
    host: _HostSource | None = None


@dataclass
class _Invocation:
    call: _Call
    executor: Any
    outer_invoke: Any
    original_invoke: Any
    resolve_executor: Any
    card: Any
    card_id: str
    card_name: str
    runtime_kwargs: Mapping
    active: bool = True
    final_entered: bool = False
    task: asyncio.Task | None = field(default_factory=asyncio.current_task, repr=False)
    execution: ToolExecution | None = field(default=None, repr=False)
    consumer_required: bool = False
    consumer_check: Callable[[], None] | None = field(default=None, repr=False)
    consumer_seal: tuple | None = field(default=None, repr=False)


_CALL: ContextVar[_Call | None] = ContextVar("native_authority_call", default=None)
_EXECUTION: ContextVar[_Invocation | None] = ContextVar("native_authority_execution", default=None)
_PROOF: ContextVar[ToolInvocation | None] = ContextVar("native_authority_proof", default=None)
_METHOD_EXECUTION: ContextVar[ToolExecution | None] = ContextVar("tool_method_execution", default=None)
_REGISTERED: dict[int, tuple] = {}


def _register_tool_invocation(executor: Any, outer: Any, original: Any) -> None:
    key = id(executor)

    def remove(reference):
        if _REGISTERED.get(key, (None,))[0] is reference:
            _REGISTERED.pop(key, None)

    _REGISTERED[key] = (ref(executor, remove), ref(outer), ref(original))


def _registered(executor):
    record = _REGISTERED.get(id(executor))
    if record is None or record[0]() is not executor:
        return None
    return record[1](), record[2]()


def _has_tool_call_scope(ctx: Any) -> bool:
    call = _CALL.get()
    return call is not None and call.active and call.ctx is ctx


def bind_tool_authorizer(ctx: Any, callback: ToolAuthorizer) -> None:
    """Append a mandatory callback to the current, exact per-tool callback context.

    Rebinding the same callable is idempotent. Long-lived agent contexts and
    expired/inherited call contexts cannot install or replace an authority.
    """
    if not callable(callback) or not _has_tool_call_scope(ctx):
        raise ValueError("an active, exact AbilityManager tool-call context is required")
    call = _CALL.get()
    call.protected = True
    if callback not in call.callbacks:
        call.callbacks.append(callback)


@contextmanager
def _tool_call_scope(ctx: Any):
    parent = _CALL.get()
    if parent is not None and parent.host is not None:
        raise PermissionError(_DENIED)
    call = _Call(
        ctx,
        getattr(ctx, "agent", None),
        getattr(ctx, "session", None),
        inherited=bool(parent and (parent.protected or parent.inherited)),
        host=ctx if isinstance(ctx, _HostSource) else None,
    )
    token = _CALL.set(call)
    try:
        yield
    finally:
        call.active = False
        call.callbacks.clear()
        _CALL.reset(token)


def _mandatory() -> bool:
    call = _CALL.get()
    return call is not None and bool(call.protected or call.inherited)


@contextmanager
def _tool_execution_scope(ctx: Any, executor: Any, resolve_executor: Callable, runtime_kwargs: Mapping):
    call = _CALL.get()
    if not _mandatory():
        yield
        return
    if call is None or not call.active or call.ctx is not ctx or not call.callbacks:
        raise PermissionError(_DENIED)
    if call.host is not None:
        if not call.host.is_current():
            raise PermissionError(_DENIED)
    elif ctx.agent is not call.agent or ctx.session is not call.session:
        raise PermissionError(_DENIED)
    registered = _registered(executor)
    if registered is None or executor.invoke is not registered[0]:
        raise PermissionError(_DENIED)
    invocation = _Invocation(
        call,
        executor,
        *registered,
        resolve_executor,
        executor.card,
        executor.card.id,
        executor.card.name,
        MappingProxyType(dict(runtime_kwargs)),
    )
    token = _EXECUTION.set(invocation)
    try:
        yield
    finally:
        invocation.active = False
        _EXECUTION.reset(token)


def _operation(
    invocation: _Invocation, inputs: Any, kwargs: dict, *, origin_only: bool = False
) -> BeforeToolContext:
    call = invocation.call
    ctx = call.ctx
    final_runtime = {key: value for key, value in kwargs.items() if key != "inputs"}
    if final_runtime.keys() != invocation.runtime_kwargs.keys() or any(
        value is not invocation.runtime_kwargs[key] for key, value in final_runtime.items()
    ):
        raise PermissionError(_DENIED)
    if call.host is not None:
        if not isinstance(inputs, Mapping):
            raise PermissionError(_DENIED)
        source_current = call.host.is_current_origin() if origin_only else call.host.is_current()
        if not source_current:
            raise PermissionError(_DENIED)
        source = call.host.operation
        return BeforeToolContext(
            source.agent_name,
            source.provider_session_id,
            source.turn_id,
            source.call_id,
            source.tool_name,
            inputs,
        )
    tool_call = ctx.inputs.tool_call
    if (
        not isinstance(inputs, Mapping)
        or kwargs.get("session") is not call.session
        or ctx.agent is not call.agent
        or ctx.session is not call.session
    ):
        raise PermissionError(_DENIED)
    if tool_call is None or tool_call.name != invocation.executor.card.name or ctx.inputs.tool_name != tool_call.name:
        raise PermissionError(_DENIED)
    return BeforeToolContext(
        str(getattr(call.agent.card, "name", "") or ""),
        call.session.get_session_id() if call.session is not None else None,
        None,
        str(tool_call.id or ""),
        tool_call.name,
        inputs,
    )


def _fingerprint(operation: BeforeToolContext) -> tuple:
    return (
        operation.agent_name,
        operation.provider_session_id,
        operation.turn_id,
        operation.call_id,
        operation.tool_name,
        json.dumps(json_value_to_builtin(operation.arguments), sort_keys=True, allow_nan=False),
    )


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Read-only in-process evidence, never a serializable protocol contract."""

    operation: BeforeToolContext
    executor: Any
    agent_context: Any
    original_invoke: Any
    _invocation: _Invocation = field(repr=False)
    _inputs: Any = field(repr=False)
    _kwargs: dict = field(repr=False)
    _callbacks: tuple = field(repr=False)
    _live: list[bool] = field(default_factory=lambda: [True], repr=False)

    def _require_consumer_check(self) -> None:
        """Declare this original invocation's private consumer requirement."""
        if not self.is_current() or self._invocation.consumer_seal is not None:
            raise PermissionError(_DENIED)
        self._invocation.consumer_required = True

    def _bind_consumer_check(self, check: Callable[[], None]) -> None:
        """Bind once in the original final scope; never mint another operation."""
        invocation = self._invocation
        if (not self.is_current() or invocation.consumer_seal is not None
                or invocation.consumer_required is not True or not callable(check)
                or (invocation.consumer_check is not None and invocation.consumer_check is not check)):
            raise PermissionError(_DENIED)
        invocation.consumer_check = check

    @property
    def source_operation(self) -> BeforeToolContext | None:
        """Original host object, distinct from the final transformed operation."""
        source = self._invocation.call.host
        return source.operation if source is not None else None

    def is_current(self) -> bool:
        """Check lifetime, exact execution identity, and final inputs after awaits."""
        invocation = self._invocation
        try:
            return (
                _PROOF.get() is self
                and self._live[0]
                and invocation.active
                and invocation.call.active
                and (invocation.call.host is None or invocation.call.host.is_current())
                and _EXECUTION.get() is invocation
                and _CALL.get() is invocation.call
                and invocation.executor is self.executor
                and invocation.resolve_executor() is self.executor
                and self.executor.card is invocation.card
                and self.executor.card.id == invocation.card_id
                and self.executor.card.name == invocation.card_name
                and self.executor.invoke is invocation.outer_invoke
                and _registered(self.executor) == (invocation.outer_invoke, self.original_invoke)
                and invocation.original_invoke is self.original_invoke
                and tuple(invocation.call.callbacks) == self._callbacks
                and _fingerprint(_operation(invocation, self._inputs, self._kwargs)) == _fingerprint(self.operation)
            )
        except Exception:
            return False


def current_tool_invocation() -> ToolInvocation | None:
    """Return the actual final invocation only during live mandatory callbacks."""
    proof = _PROOF.get()
    return proof if proof is not None and proof.is_current() else None


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """Read-only origin evidence for an already authorized, running method.

    This is not a grant for another operation. Internal I/O consumers must check
    their actual post-parse arguments and resource authority independently.
    """

    operation: BeforeToolContext
    executor: Any
    agent_context: Any
    original_invoke: Any
    owning_task: asyncio.Task
    _invocation: _Invocation = field(repr=False)
    _kwargs: dict = field(repr=False)
    _callbacks: tuple = field(repr=False)
    _live: list[bool] = field(default_factory=lambda: [True], repr=False)

    _consumer_seal: tuple | None = field(default=None, repr=False)

    def _check_consumer(self) -> None:
        """Recheck the fixed current operation without approvals or rebinding."""
        seal = self._consumer_seal
        if (not self.is_current() or seal is None or seal[0] is not True or not callable(seal[1])):
            raise PermissionError(_DENIED)
        try:
            result = seal[1]()
            if inspect.iscoroutine(result):
                result.close()
            if result is not None or self._consumer_seal is not seal or not self.is_current():
                raise PermissionError(_DENIED)
        except Exception as exc:
            raise PermissionError(_DENIED) from exc

    @property
    def source_operation(self) -> BeforeToolContext | None:
        """Original host source identity, or None for AbilityManager calls."""
        source = self._invocation.call.host
        return source.operation if source is not None else None

    def is_current_origin(self) -> bool:
        """Validate a captured origin, including from an inherited SDK task.

        Does not bypass a task-bound host predicate. The caller cannot acquire
        a new certificate or bind a new tool operation through this method.
        """
        invocation = self._invocation
        try:
            return (
                _METHOD_EXECUTION.get() is self
                and self._live[0]
                and invocation.consumer_seal is self._consumer_seal
                and self._consumer_seal is not None
                and invocation.consumer_required is self._consumer_seal[0]
                and invocation.consumer_check is self._consumer_seal[1]
                and invocation.execution is self
                and invocation.task is self.owning_task
                and not self.owning_task.done()
                and not self.owning_task.cancelling()
                and invocation.active
                and invocation.call.active
                and _EXECUTION.get() is invocation
                and _CALL.get() is invocation.call
                and invocation.executor is self.executor
                and invocation.resolve_executor() is self.executor
                and self.executor.card is invocation.card
                and self.executor.card.id == invocation.card_id
                and self.executor.card.name == invocation.card_name
                and self.executor.invoke is invocation.outer_invoke
                and _registered(self.executor) == (invocation.outer_invoke, self.original_invoke)
                and invocation.original_invoke is self.original_invoke
                and tuple(invocation.call.callbacks) == self._callbacks
                and _fingerprint(_operation(
                    invocation, json_value_to_builtin(self.operation.arguments), self._kwargs, origin_only=True
                )) == _fingerprint(self.operation)
            )
        except Exception:
            return False

    def is_current(self) -> bool:
        """Require the original owning task as well as current origin evidence."""
        try:
            return asyncio.current_task() is self.owning_task and self.is_current_origin()
        except RuntimeError:
            return False


def current_tool_execution() -> ToolExecution | None:
    """Capture execution evidence only inside the original method's own task."""
    execution = _METHOD_EXECUTION.get()
    return execution if execution is not None and execution.is_current() else None


def _capture_tool_consumer_check(executor):
    """Return only an explicitly required managed check; old policy is unchanged."""
    invocation, execution = _EXECUTION.get(), _METHOD_EXECUTION.get()
    seals = (getattr(invocation, "consumer_seal", None), getattr(execution, "_consumer_seal", None))
    required = (getattr(invocation, "consumer_required", False) is True
                or any(seal is not None and seal[0] is True for seal in seals))
    if not required:
        return None
    if execution is None or execution.executor is not executor or not execution.is_current():
        raise PermissionError(_DENIED)
    execution._check_consumer()
    return execution._check_consumer


@contextmanager
def _mask_tool_execution():
    """Callbacks and nested tool entry must not borrow a parent's certificate."""
    token = _METHOD_EXECUTION.set(None)
    try:
        yield
    finally:
        _METHOD_EXECUTION.reset(token)


@contextmanager
def _original_tool_execution(executor: Any, original: Any, operation: BeforeToolContext | None, kwargs: dict):
    invocation = _EXECUTION.get()
    task = asyncio.current_task()
    execution = None
    live = [True]
    if operation is not None and invocation is not None and invocation.task is task:
        if (invocation.execution is None and invocation.executor is executor
                and invocation.original_invoke is original):
            execution = ToolExecution(operation, executor, invocation.call.ctx, original, task,
                                      invocation, kwargs, tuple(invocation.call.callbacks), live,
                                      _consumer_seal=invocation.consumer_seal)
            invocation.execution = execution
    token = _METHOD_EXECUTION.set(execution)
    try:
        yield
    finally:
        live[0] = False
        if execution is not None:
            invocation.execution = None
        _METHOD_EXECUTION.reset(token)


async def _authorize_final_invocation(
    executor: Any, original: Any, args: tuple, kwargs: dict
) -> BeforeToolContext | None:
    if not _mandatory():
        return
    invocation = _EXECUTION.get()
    try:
        if (
            invocation is None
            or not invocation.active
            or invocation.executor is not executor
            or invocation.original_invoke is not original
        ):
            raise PermissionError(_DENIED)
        if invocation.call.host is not None:
            if invocation.final_entered:
                raise PermissionError(_DENIED)
            invocation.final_entered = True
        if len(args) > 1:
            raise PermissionError(_DENIED)
        bound = inspect.signature(original).bind(*args, **kwargs)
        inputs = bound.arguments.get("inputs")
        operation = _operation(invocation, inputs, kwargs)
        live = [True]
        proof = ToolInvocation(
            operation,
            executor,
            invocation.call.ctx,
            original,
            invocation,
            inputs,
            kwargs,
            tuple(invocation.call.callbacks),
            live,
        )
        token = _PROOF.set(proof)
        try:
            if not proof.is_current():
                raise PermissionError(_DENIED)
            for callback in tuple(invocation.call.callbacks):
                required, check = invocation.consumer_required, invocation.consumer_check
                if (await callback(operation) is not True or not proof.is_current()
                        or (required and invocation.consumer_required is not True)
                        or (check is not None and invocation.consumer_check is not check)):
                    raise PermissionError(_DENIED)
            if (type(invocation.consumer_required) is not bool
                    or (invocation.consumer_required and not callable(invocation.consumer_check))):
                raise PermissionError(_DENIED)
            invocation.consumer_seal = (invocation.consumer_required, invocation.consumer_check)
        finally:
            live[0] = False
            _PROOF.reset(token)
        return operation
    except Exception as exc:
        raise PermissionError(_DENIED) from exc


def _deny_protected_stream() -> None:
    if _mandatory():
        raise PermissionError("[PERMISSION_DENIED] Mandatory authority does not support Tool.stream")


async def invoke_tool_with_authority(
    executor: Any,
    inputs: Mapping,
    *,
    operation: BeforeToolContext,
    authorizer: ToolAuthorizer,
    runtime_kwargs: Mapping,
    is_current: Callable[[], bool],
    resolve_executor: Callable[[], Any],
) -> Any:
    """Invoke a host-owned Tool through its existing final mandatory boundary.

    No Agent or global registration is created. The host supplies a fixed source
    operation and must verify its resource/session ownership in ``is_current``
    and ``authorizer``. The authorizer sees actual transformed inputs; its live
    ToolInvocation.source_operation retains the original object for host proof.
    """
    task = asyncio.current_task()
    parent = _CALL.get()
    registered = _registered(executor)
    if task is None or not isinstance(operation, BeforeToolContext) or not isinstance(inputs, Mapping):
        raise PermissionError(_DENIED)
    if (
        not isinstance(runtime_kwargs, Mapping)
        or "inputs" in runtime_kwargs
        or any(not isinstance(key, str) for key in runtime_kwargs)
        or not all(callable(value) for value in (authorizer, is_current, resolve_executor))
    ):
        raise PermissionError(_DENIED)
    if registered is None or executor.invoke is not registered[0]:
        raise PermissionError(_DENIED)
    if parent is not None and parent.host is not None:
        raise PermissionError(_DENIED)
    if json.dumps(json_value_to_builtin(operation.arguments), sort_keys=True, allow_nan=False) != json.dumps(
        json_value_to_builtin(inputs), sort_keys=True, allow_nan=False
    ):
        raise PermissionError(_DENIED)
    snapshot = MappingProxyType(dict(runtime_kwargs))
    source = _HostSource(operation, is_current, task, runtime_kwargs, snapshot)
    if not source.is_current() or resolve_executor() is not executor:
        raise PermissionError(_DENIED)
    with _tool_call_scope(source):
        bind_tool_authorizer(source, authorizer)
        with _tool_execution_scope(source, executor, resolve_executor, snapshot):
            return await registered[0](inputs=inputs, **snapshot)
