"""Opaque live execution provenance; never a persisted authorization grant."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, PrivateAttr


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ExecutionOrigin:
    """Host-owned source object, preserved by identity through in-memory copies."""

    host_value: object

    def __repr__(self):
        return "ExecutionOrigin(<host>)"

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        raise TypeError("execution origin cannot be serialized")


@dataclass(slots=True)
class _OriginScope:
    origin: ExecutionOrigin | None
    active: bool = True


_scope: ContextVar[_OriginScope | None] = ContextVar("execution_origin", default=None)
ORIGIN_UNSET = object()


def current_execution_origin() -> ExecutionOrigin | None:
    """Read the current live lexical scope; expired inherited tasks see None."""
    scope = _scope.get()
    return scope.origin if scope is not None and scope.active else None


def resolve_execution_origin(origin=ORIGIN_UNSET) -> ExecutionOrigin | None:
    """Capture omission once, while an explicit None always masks the source."""
    if origin is ORIGIN_UNSET:
        return current_execution_origin()
    if origin is not None and not isinstance(origin, ExecutionOrigin):
        raise TypeError("execution origin must be a live ExecutionOrigin")
    return origin


@contextmanager
def execution_origin_scope(origin: ExecutionOrigin | None):
    """Install provenance locally, without changing the queued origin's lifetime."""
    value = _OriginScope(resolve_execution_origin(origin))
    token = _scope.set(value)
    try:
        yield
    finally:
        value.active = False
        _scope.reset(token)


class OriginCarrier(BaseModel):
    """Private provenance: JSON parsing never captures an ambient scope."""

    _execution_origin: ExecutionOrigin | None = PrivateAttr(default=None)

    @property
    def execution_origin(self) -> ExecutionOrigin | None:
        """Original source or None after serialization/restoration."""
        return self._execution_origin

    def with_execution_origin(self, origin: ExecutionOrigin | None) -> Self:
        """Return a new carrier; do not mutate an already enqueued instance."""
        result = self.model_copy()
        result._execution_origin = resolve_execution_origin(origin)
        return result


def shared_execution_origin(values) -> ExecutionOrigin | None:
    """Require one exact source, including explicit missing-source entries."""
    origins = [getattr(value, "execution_origin", None) for value in values]
    origin = origins[0] if origins else None
    if any(item is not origin for item in origins):
        raise ValueError("mixed execution origins")
    return resolve_execution_origin(origin)


__all__ = ["ExecutionOrigin", "OriginCarrier", "current_execution_origin", "execution_origin_scope"]


@dataclass(frozen=True, slots=True)
class _SourcedInput:
    content: object
    origin: ExecutionOrigin


def capture_origin_input(content, origin=ORIGIN_UNSET):
    """Keep live provenance in an existing in-memory input queue."""
    source = resolve_execution_origin(origin)
    return _SourcedInput(content, source) if source is not None else content


def consume_origin_input(value, expected_origin=ORIGIN_UNSET):
    """Validate original provenance before unwrapping a queued input."""
    origin = resolve_execution_origin(expected_origin)
    source = value.origin if isinstance(value, _SourcedInput) else None
    if source is not origin:
        raise ValueError("mixed execution origins in input queue")
    return value.content if isinstance(value, _SourcedInput) else value


def _unwrap_origin_inputs(values):
    """Recover one exact source from an already captured live queue batch."""
    source = None
    messages = []
    for index, value in enumerate(values):
        origin = value.origin if isinstance(value, _SourcedInput) else None
        if index == 0:
            source = origin
        elif origin is not source:
            raise ValueError("mixed execution origins in input batch")
        messages.append(value.content if isinstance(value, _SourcedInput) else value)
    return source, messages
