# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Instance-bound authority for the final model HTTP request.

The callback belongs to the trusted host. It is deliberately not model config,
request kwargs, a global registry, or a tool/harness permission hook.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Protocol, runtime_checkable

import httpx

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import ModelRequestDenied, build_error
from openjiuwen.core.foundation.llm.schema.config import LLMApiMode, LLMAuthMode, ModelClientConfig, ProviderType


@dataclass(frozen=True, slots=True)
class ModelRequestTarget:
    """Actual, credential-free HTTP target after model input transformations."""

    method: str
    url: str
    model: str
    api_mode: str
    operation: str
    implementation: str


ModelRequestAuthority = Callable[[ModelRequestTarget], Awaitable[Mapping[str, str] | None]]


@runtime_checkable
class ModelRequestAuthorityFactory(Protocol):
    """Bind the host authority synchronously at logical model-call entry."""

    def bind_for_call(self) -> ModelRequestAuthority:
        """Return the fixed authority for this call and all of its HTTP retries."""


@dataclass(slots=True)
class _CallAuthority:
    owner: object
    callback: ModelRequestAuthority
    active: bool = True


_current_call: ContextVar[_CallAuthority | None] = ContextVar("model_request_authority_call", default=None)


def _bind_call(owner, *, new_call: bool):
    source = getattr(owner, "_request_authority", None)
    if source is None:
        return None, False
    current = _current_call.get()
    if not new_call and current is not None and current.owner is owner:
        if not current.active:
            raise request_denied("model request authority call is closed")
        return current, False
    cancelled = False
    try:
        callback = source.bind_for_call() if isinstance(source, ModelRequestAuthorityFactory) else source
        if inspect.isawaitable(callback):
            if inspect.iscoroutine(callback):
                callback.close()
            raise request_denied("model authority factory must bind synchronously")
        if not callable(callback):
            raise request_denied("model authority factory did not bind a callback")
    except asyncio.CancelledError:
        cancelled = True
        callback = None
    except Exception:
        # Do not retain a host exception in the public error's context chain.
        callback = None
    if cancelled:
        raise asyncio.CancelledError() from None
    if callback is None:
        raise request_denied("model authority could not bind this call")
    return _CallAuthority(owner, callback), True


@contextmanager
def _authority_slice(bound):
    if bound is None:
        yield
        return
    token = _current_call.set(bound)
    try:
        yield
    finally:
        _current_call.reset(token)


@contextmanager
def _authority_call(owner, *, new_call: bool):
    bound, owned = _bind_call(owner, new_call=new_call)
    try:
        with _authority_slice(bound):
            yield
    finally:
        if owned:
            bound.active = False


def authorize_model_call(*, model_wrapper: bool = False):
    """Bind before transforms/awaits, with no context token across a stream yield."""

    def decorate(function):
        if inspect.isasyncgenfunction(function):

            @wraps(function)
            async def stream(self, *args, **kwargs):
                owner = self._client if model_wrapper else self  # pylint: disable=protected-access
                bound, owned = _bind_call(owner, new_call=model_wrapper)
                iterator = function(self, *args, **kwargs)
                try:
                    while True:
                        try:
                            with _authority_slice(bound):
                                item = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        yield item
                finally:
                    try:
                        with _authority_slice(bound):
                            await iterator.aclose()
                    finally:
                        if owned:
                            bound.active = False

            return stream

        @wraps(function)
        async def invoke(self, *args, **kwargs):
            owner = self._client if model_wrapper else self  # pylint: disable=protected-access
            with _authority_call(owner, new_call=model_wrapper):
                return await function(self, *args, **kwargs)

        return invoke

    return decorate


def authority_for_http(owner) -> ModelRequestAuthority:
    """Capture one active logical call; never select a newer host authority here."""
    bound = _current_call.get()
    if bound is None or bound.owner is not owner or not bound.active:
        raise request_denied("model HTTP request has no active bound authority")

    async def authorize(target):
        if not bound.active:
            raise request_denied("model authority call is closed")
        headers = await bound.callback(target)
        if not bound.active:
            raise request_denied("model authority call closed during authorization")
        return headers

    return authorize


def request_denied(reason: str) -> ModelRequestDenied:
    """Create a non-retryable error using only a static, non-secret reason."""
    return build_error(StatusCode.MODEL_REQUEST_AUTHORIZATION_INVALID, reason=reason)


def require_supported_request_authority(config: ModelClientConfig) -> None:
    """Reject protected combinations before constructing a client or transport."""

    def value(item):
        return getattr(item, "value", item)

    try:
        endpoint = httpx.URL(config.api_base)
        supported = (
            value(config.client_provider) == ProviderType.OpenAI.value
            and value(config.api_mode) in (None, LLMApiMode.ChatCompletions.value)
            and value(config.auth_mode) == LLMAuthMode.ApiKey.value
            and value(config.extensions.kv_cache.mode) == "none"
            and endpoint.scheme in {"http", "https"}
            and bool(endpoint.host)
            and not endpoint.userinfo
            and not endpoint.query
            and not endpoint.fragment
        )
    except Exception:
        supported = False
    if not supported:
        raise request_denied("request authority supports only OpenAI API-key chat completions")


class _ModelRequestDeniedSignal(BaseException):
    """Private HTTP-to-client signal bypassing SDK connection-error retries.

    Older supported OpenAI SDKs catch every Exception, including OpenAIError.
    The client must consume this signal and expose only ModelRequestDenied.
    It carries no host exception, request, headers or credential.
    """


def unwrap_request_denial(error: BaseException) -> None:
    """Preserve the safe mandatory denial instead of a retryable SDK wrapper."""
    if isinstance(error, ModelRequestDenied):
        raise error
    if isinstance(error, _ModelRequestDeniedSignal):
        raise request_denied("model request authority denied the request") from None


def guarded_request_hook(authority: ModelRequestAuthority, endpoint: str) -> Callable[[httpx.Request], Awaitable[None]]:
    """Build a private HTTP hook; one fresh authorization for every SDK attempt."""

    async def authorize(request: httpx.Request) -> None:
        # Exact built-in types exclude mutable/custom serialization behavior.
        # pylint: disable=unidiomatic-typecheck,too-many-boolean-expressions
        # Never expose SDK placeholders or caller-supplied auth to the host.
        request.headers.pop("Authorization", None)
        cancelled = False
        try:
            body = request.content
            target_url = str(request.url)
            method = request.method
            original_stream = request.stream
            original_headers = tuple(request.headers.multi_items())
            if type(original_stream) is not httpx.ByteStream or b"".join(original_stream) != body:
                raise request_denied("unsupported model HTTP body stream")
            payload = json.loads(body)
            if (
                method != "POST"
                or target_url != endpoint
                or not isinstance(payload, dict)
                or not isinstance(payload.get("model"), str)
                or not payload["model"].strip()
                or type(payload.get("stream", False)) is not bool
            ):
                raise request_denied("unsupported actual model HTTP target")
            target = ModelRequestTarget(
                method=method,
                url=target_url,
                model=payload["model"],
                api_mode=LLMApiMode.ChatCompletions.value,
                operation="stream" if payload.get("stream") else "invoke",
                implementation="OpenAIModelClient",
            )
            headers = await authority(target)
            if not isinstance(headers, Mapping):
                raise request_denied("missing model request authorization")
            headers = dict(headers)
            if len(headers) != 1:
                raise request_denied("missing model request authorization")
            name, authorization = next(iter(headers.items()))
            if (
                type(name) is not str
                or name.lower() != "authorization"
                or type(authorization) is not str
                or not authorization.startswith("Bearer ")
                or not authorization[7:]
                or any(not 33 <= ord(char) <= 126 for char in authorization[7:])
            ):
                raise request_denied("invalid model authorization header")
            if (
                request.method != method
                or str(request.url) != target_url
                or request.content != body
                or request.stream is not original_stream
                or b"".join(original_stream) != body
                or tuple(request.headers.multi_items()) != original_headers
            ):
                raise request_denied("model HTTP target changed during authorization")
            request.headers["Authorization"] = authorization
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # Leave the handler before raising: even __context__ must not retain
            # a host exception that could contain a credential.
            pass
        else:
            return
        if cancelled:
            raise asyncio.CancelledError() from None
        raise _ModelRequestDeniedSignal() from None

    return authorize


__all__ = ["ModelRequestTarget", "ModelRequestAuthority", "ModelRequestAuthorityFactory", "ModelRequestDenied"]
