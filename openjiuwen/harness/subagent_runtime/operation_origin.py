"""Private live ownership of existing subagent operations, never resource authority."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, _capture_live_execution_origin


@dataclass(eq=False, repr=False)
class _OperationLifetime:
    origin: ExecutionOrigin
    parent: _OperationLifetime | None = None
    cancel_requested: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    acquire_task: asyncio.Task | None = None
    run_task: asyncio.Task | None = None
    instance: object | None = None
    operation: object | None = None

    def check_admission(self):
        value = self
        while value is not None:
            if value.cancel_requested:
                raise RuntimeError("original subagent operation no longer accepts work")
            value = value.parent
        if self.done.is_set():
            raise RuntimeError("original subagent operation has exited")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        raise TypeError("subagent operation lifetime cannot be serialized")


@dataclass
class _OperationScope:
    value: _OperationLifetime | None
    active: bool = True


_operation = ContextVar("subagent_operation_lifetime", default=None)


@contextmanager
def _operation_scope(value):
    scope = _OperationScope(value)
    token = _operation.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _operation.reset(token)


def _capture_operation_lifetime():
    scope = _operation.get()
    if scope is not None:
        if not scope.active:
            raise RuntimeError("original subagent operation scope expired")
        parent = scope.value
        if parent is not None:
            parent.check_admission()
            return _OperationLifetime(parent.origin, parent=parent)
        return None
    source = _capture_live_execution_origin()
    if source is None:
        return None
    return _OperationLifetime(source)


def _current_operation_lifetime():
    scope = _operation.get()
    if scope is None:
        return None
    if not scope.active:
        raise RuntimeError("original subagent operation scope expired")
    return scope.value
