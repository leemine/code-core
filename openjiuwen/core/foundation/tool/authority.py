# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Mandatory authorization at the final Native invocation boundary.

Bindings belong to one AbilityManager call, not an agent, Turn, or wire payload.
The public proof is available only while its final authorizers are executing.
"""

from __future__ import annotations

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


@dataclass
class _Call:
    ctx: Any
    agent: Any
    session: Any
    callbacks: list[ToolAuthorizer] = field(default_factory=list)
    active: bool = True
    inherited: bool = False
    protected: bool = False


@dataclass
class _Invocation:
    call: _Call
    executor: Any
    outer_invoke: Any
    original_invoke: Any
    resolve_executor: Any
    card: Any
    card_id: str
    runtime_kwargs: Mapping
    active: bool = True


_CALL: ContextVar[_Call | None] = ContextVar("native_authority_call", default=None)
_EXECUTION: ContextVar[_Invocation | None] = ContextVar("native_authority_execution", default=None)
_PROOF: ContextVar[ToolInvocation | None] = ContextVar("native_authority_proof", default=None)
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
    call = _Call(
        ctx,
        ctx.agent,
        ctx.session,
        inherited=bool(parent and (parent.protected or parent.inherited)),
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
    if ctx.agent is not call.agent or ctx.session is not call.session:
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
        MappingProxyType(dict(runtime_kwargs)),
    )
    token = _EXECUTION.set(invocation)
    try:
        yield
    finally:
        invocation.active = False
        _EXECUTION.reset(token)


def _operation(invocation: _Invocation, inputs: Any, kwargs: dict) -> BeforeToolContext:
    call = invocation.call
    ctx = call.ctx
    tool_call = ctx.inputs.tool_call
    final_runtime = {key: value for key, value in kwargs.items() if key != "inputs"}
    if final_runtime.keys() != invocation.runtime_kwargs.keys() or any(
        value is not invocation.runtime_kwargs[key] for key, value in final_runtime.items()
    ):
        raise PermissionError(_DENIED)
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

    def is_current(self) -> bool:
        """Check lifetime, exact execution identity, and final inputs after awaits."""
        invocation = self._invocation
        try:
            return (
                _PROOF.get() is self
                and self._live[0]
                and invocation.active
                and invocation.call.active
                and _EXECUTION.get() is invocation
                and _CALL.get() is invocation.call
                and invocation.executor is self.executor
                and invocation.resolve_executor() is self.executor
                and self.executor.card is invocation.card
                and self.executor.card.id == invocation.card_id
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


async def _authorize_final_invocation(executor: Any, original: Any, args: tuple, kwargs: dict) -> None:
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
                if await callback(operation) is not True or not proof.is_current():
                    raise PermissionError(_DENIED)
        finally:
            live[0] = False
            _PROOF.reset(token)
    except Exception as exc:
        raise PermissionError(_DENIED) from exc


def _deny_protected_stream() -> None:
    if _mandatory():
        raise PermissionError("[PERMISSION_DENIED] Mandatory authority does not support Tool.stream")
