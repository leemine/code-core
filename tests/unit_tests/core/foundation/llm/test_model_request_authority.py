# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Actual OpenAI SDK + HTTPX sends, with a synthetic in-memory network peer."""

import asyncio
import json
from dataclasses import FrozenInstanceError

import httpx
import pytest

from openjiuwen.core.foundation.llm import (
    Model,
    ModelClientConfig,
    ModelRequestConfig,
    ModelRequestDenied,
    init_model,
)
from openjiuwen.core.foundation.llm.model_clients import create_model_client
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient
from openjiuwen.core.foundation.llm.request_authority import (
    _ModelRequestDeniedSignal,
    guarded_request_hook,
    unwrap_request_denial,
)


@pytest.fixture
def network(monkeypatch):
    sent = []
    replies = []

    async def handle(_transport, request):
        sent.append(request)
        if replies:
            response = replies.pop(0)
            if callable(response):
                response = response(request)
            return response
        payload = json.loads(request.content)
        if payload.get("stream"):
            chunk = {
                "id": "test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": payload["model"],
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None}],
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
            )
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return sent, replies


def config(**changes):
    return ModelClientConfig(
        client_provider="OpenAI",
        api_key="fixture-static-key",
        api_base="https://model.invalid/v1",
        max_retries=2,
        **changes,
    )


def model(authority, **changes):
    return Model(config(**changes), ModelRequestConfig(model="fixture-model"), request_authority=authority)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [False, True])
async def test_authority_cancellation_does_not_retain_host_secret(network, factory):
    async def cancelled(_target):
        raise asyncio.CancelledError("synthetic-host-secret")

    class Factory:
        def bind_for_call(self):
            raise asyncio.CancelledError("synthetic-host-secret")

    with pytest.raises(asyncio.CancelledError) as caught:
        await model(Factory() if factory else cancelled).invoke("hello")
    assert not caught.value.args
    assert caught.value.__context__ is None
    assert not network[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_actual_sdk_invoke_stream_use_current_authority_and_final_model(network, stream, monkeypatch):
    sent, _ = network
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-never-use")
    monkeypatch.setenv("OPENAI_ORG_ID", "ambient-org-never-use")

    async def authority(target):
        calls.append(target)
        with pytest.raises(FrozenInstanceError):
            target.model = "changed"
        return {"Authorization": "Bearer current-fixture-key"}

    guarded = model(authority)
    if stream:
        chunks = [chunk async for chunk in guarded.stream("hello", model="actual-override")]
        assert chunks[0].content == "OK"
    else:
        assert (await guarded.invoke("hello", model="actual-override")).content == "OK"
    assert len(calls) == len(sent) == 1
    assert calls[0].model == "actual-override"
    assert calls[0].method == "POST"
    assert calls[0].url == "https://model.invalid/v1/chat/completions"
    assert calls[0].api_mode == "chat_completions"
    assert calls[0].operation == ("stream" if stream else "invoke")
    assert calls[0].implementation == "OpenAIModelClient"
    assert sent[0].headers["authorization"] == "Bearer current-fixture-key"
    assert not sent[0].headers.get("openai-organization")
    assert "current-fixture-key" not in guarded.model_client_config.model_dump_json()
    assert "fixture-static-key" not in guarded.model_client_config.model_dump_json()


@pytest.mark.asyncio
async def test_separate_instances_do_not_share_client_authority_or_revoked_secret(network):
    sent, _ = network
    permitted = True
    seen = []

    async def alice(target):
        seen.append("alice")
        return {"Authorization": "Bearer alice-fixture"} if permitted else None

    async def bob(target):
        seen.append("bob")
        return {"Authorization": "Bearer bob-fixture"}

    before = dict(OpenAIModelClient._client_cache)
    first, second = model(alice), model(bob)
    await asyncio.gather(first.invoke("one"), second.invoke("two"))
    permitted = False
    with pytest.raises(ModelRequestDenied) as denied:
        await first.invoke("three")
    assert denied.value.fatal and not denied.value.recoverable
    await second.invoke("four")
    assert [request.headers["authorization"] for request in sent] == [
        "Bearer alice-fixture",
        "Bearer bob-fixture",
        "Bearer bob-fixture",
    ]
    assert seen.count("alice") == 2
    assert OpenAIModelClient._client_cache == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_retry_reauthorizes_and_revocation_is_not_retried(network, stream):
    sent, replies = network
    calls = []
    replies.append(httpx.Response(429, headers={"retry-after-ms": "1"}, json={"error": {"message": "retry"}}))

    async def authority(target):
        calls.append(target)
        return {"Authorization": "Bearer first-fixture"} if len(calls) == 1 else None

    guarded = model(authority)
    with pytest.raises(ModelRequestDenied):
        if stream:
            _ = [chunk async for chunk in guarded.stream("hello")]
        else:
            await guarded.invoke("hello")
    assert len(calls) == 2  # No third attempt after authority's refusal.
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        True,
        {"X-Key": "fixture"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer a\nb"},
        {"Authorization": "Basic fixture"},
        {"Authorization": "Bearer x", "extra": "no"},
    ],
)
async def test_invalid_authority_results_fail_before_network_without_sdk_retry(network, answer):
    calls = []

    async def authority(target):
        calls.append(target)
        return answer

    with pytest.raises(ModelRequestDenied):
        await model(authority).invoke("hello")
    assert len(calls) == 1
    assert network[0] == []


@pytest.mark.asyncio
async def test_authority_error_is_sanitized_and_cancellation_preserved(network):
    async def faulty(target):
        raise RuntimeError("secret-fixture-not-for-error")

    with pytest.raises(ModelRequestDenied) as error:
        await model(faulty).invoke("hello")
    assert "secret-fixture-not-for-error" not in str(error.value)
    assert error.value.__cause__ is None
    context = error.value.__context__
    while context is not None:
        assert "secret-fixture-not-for-error" not in str(context)
        context = context.__context__

    async def cancelled(target):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await model(cancelled).invoke("hello")
    assert not network[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["method", "url", "body", "stream", "stream-content", "host"])
async def test_request_changed_during_authority_await_is_denied(change):
    request = httpx.Request(
        "POST", "https://model.invalid/v1/chat/completions", json={"model": "fixture", "stream": False}
    )

    async def authority(target):
        await asyncio.sleep(0)
        if change == "method":
            request.method = "GET"
        elif change == "url":
            request.url = httpx.URL("https://other.invalid/v1/chat/completions")
        elif change == "body":
            request._content = b'{"model":"other"}'
        elif change == "stream":
            request.stream = httpx.ByteStream(b'{"model":"other"}')
        elif change == "stream-content":
            request.stream._stream = b'{"model":"other"}'
        else:
            request.headers["Host"] = "other.invalid"
        return {"Authorization": "Bearer fixture"}

    hook = guarded_request_hook(authority, str(request.url))
    with pytest.raises(ModelRequestDenied):
        try:
            await hook(request)
        except _ModelRequestDeniedSignal as error:
            unwrap_request_denial(error)
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("direct_client", [False, True])
async def test_private_http_stop_signal_never_escapes_public_api_or_retries(network, stream, direct_client):
    calls = []

    async def deny(target):
        calls.append(target)
        return None

    guarded = model(deny)
    client = guarded._client if direct_client else guarded
    # OpenAI 2.24 catches Exception broadly; the private stop must bypass that
    # retry block and be converted back at the core client boundary.
    assert not issubclass(_ModelRequestDeniedSignal, Exception)
    with pytest.raises(ModelRequestDenied) as error:
        if stream:
            _ = [chunk async for chunk in client.stream("hello")]
        else:
            await client.invoke("hello")
    assert type(error.value) is ModelRequestDenied
    assert len(calls) == 1
    assert network[0] == []


@pytest.mark.asyncio
async def test_guarded_transport_has_no_redirect_or_connection_retry(network):
    sent, replies = network
    replies.append(httpx.Response(307, headers={"location": "https://other.invalid/leak"}))

    async def authority(target):
        return {"Authorization": "Bearer fixture"}

    guarded = model(authority)
    from openjiuwen.core.foundation.llm.request_authority import _authority_call

    with _authority_call(guarded._client, new_call=True):
        sdk = guarded._client._build_async_openai_client()
        try:
            assert sdk._client.follow_redirects is False
            assert sdk._client._transport._pool._retries == 0
            assert sdk._client._trust_env is False
        finally:
            await sdk.close()
    with pytest.raises(Exception):
        await guarded.invoke("hello")
    assert len(sent) == 1
    assert str(sent[0].url) == "https://model.invalid/v1/chat/completions"


@pytest.mark.parametrize(
    "updates",
    [
        {"api_mode": "responses"},
        {"api_mode": "anthropic_messages"},
        {"auth_mode": "openai_account_oauth"},
        {"client_provider": "Anthropic"},
        {"client_provider": "custom-fixture"},
        {"extensions": {"kv_cache": {"mode": "affinity"}}},
        {"api_base": "https://fixture:fixture@model.invalid/v1"},
        {"api_base": "https://model.invalid/v1?token=fixture"},
        {"api_base": "https://model.invalid/v1#fixture"},
    ],
)
def test_unsupported_factory_rejected_before_client_creation(updates, monkeypatch):
    cfg = config().model_copy(update=updates)
    if "extensions" in updates:
        from openjiuwen.core.foundation.llm.schema.config import LLMExtensionsConfig

        cfg.extensions = LLMExtensionsConfig(**updates["extensions"])

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported client/auth/network factory must not run")

    monkeypatch.setattr("openjiuwen.core.foundation.llm.model_clients._builtin_model_client", forbidden)
    with pytest.raises(ModelRequestDenied):
        create_model_client(cfg, ModelRequestConfig(model="fixture"), request_authority=forbidden)


@pytest.mark.asyncio
async def test_config_mutation_and_unsupported_operations_rejected(network):
    async def authority(target):
        return {"Authorization": "Bearer fixture"}

    guarded = model(authority)
    for operation in ("generate_image", "generate_video", "generate_speech"):
        with pytest.raises(ModelRequestDenied):
            await getattr(guarded._client, operation)([] if operation != "generate_speech" else "hello")
    with pytest.raises(ModelRequestDenied):
        await guarded._client.evict_kvc(session_id="fixture")
    guarded._client.model_client_config.api_mode = "responses"
    with pytest.raises(ModelRequestDenied):
        await guarded.invoke("hello")
    assert network[0] == []


@pytest.mark.asyncio
async def test_init_model_consumes_authority_without_request_config_or_body_leak(network):
    async def authority(target):
        return {"Authorization": "Bearer fixture"}

    guarded = init_model(
        "OpenAI", "fixture-model", "placeholder", "https://model.invalid/v1", request_authority=authority
    )
    await guarded.invoke("hello")
    assert "request_authority" not in guarded.model_config.model_dump()
    assert "request_authority" not in json.loads(network[0][0].content)


@pytest.mark.asyncio
async def test_actual_input_transform_and_extra_body_model_are_authorized(network, monkeypatch):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback import AsyncCallbackFramework
    from openjiuwen.core.runner.callback.events import LLMCallEvents

    framework = AsyncCallbackFramework(enable_logging=False)
    monkeypatch.setattr(Runner, "callback_framework", framework)
    seen = []

    @framework.on_transform(LLMCallEvents.LLM_INVOKE_INPUT)
    async def transform(**kwargs):
        return (), {"messages": kwargs["messages"], "extra_body": {"model": "transformed-final-model"}}

    async def authority(target):
        seen.append(target.model)
        return {"Authorization": "Bearer fixture"}

    await model(authority).invoke("hello")
    assert seen == ["transformed-final-model"]
    assert json.loads(network[0][0].content)["model"] == "transformed-final-model"


@pytest.mark.asyncio
async def test_allowed_sdk_retry_consumes_a_fresh_credential(network):
    sent, replies = network
    replies.append(httpx.Response(503, headers={"retry-after-ms": "1"}, json={"error": {"message": "retry"}}))
    count = 0

    async def authority(target):
        nonlocal count
        count += 1
        return {"Authorization": f"Bearer fixture-{count}"}

    assert (await model(authority).invoke("hello")).content == "OK"
    assert [request.headers["Authorization"] for request in sent] == ["Bearer fixture-1", "Bearer fixture-2"]


@pytest.mark.asyncio
async def test_unprotected_sdk_preserves_legacy_static_auth(network):
    legacy = Model(config(use_shared_llm_http_client=False), ModelRequestConfig(model="fixture"))
    assert (await legacy.invoke("hello")).content == "OK"
    assert network[0][0].headers["Authorization"] == "Bearer fixture-static-key"


def test_guard_does_not_inherit_support_to_unknown_subclass():
    class CustomClient(OpenAIModelClient):
        pass

    with pytest.raises(ModelRequestDenied):
        CustomClient(ModelRequestConfig(model="fixture"), config(), request_authority=lambda target: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_factory_pins_old_call_before_transform_and_cannot_borrow_new_turn(network, monkeypatch, stream):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback import AsyncCallbackFramework
    from openjiuwen.core.runner.callback.events import LLMCallEvents

    framework = AsyncCallbackFramework(enable_logging=False)
    monkeypatch.setattr(Runner, "callback_framework", framework)
    entered, resume = asyncio.Event(), asyncio.Event()
    generation = 0
    bound = []

    class Factory:
        def bind_for_call(self):
            captured = generation
            bound.append(captured)

            async def authorize(target):
                return {"Authorization": f"Bearer generation-{captured}"} if captured == generation else None

            return authorize

    async def transform(**kwargs):
        if kwargs["messages"] == "old":
            entered.set()
            await resume.wait()
        return (), {"messages": kwargs["messages"]}

    framework.on_transform(LLMCallEvents.LLM_INVOKE_INPUT)(transform)
    framework.on_transform(LLMCallEvents.LLM_STREAM_INPUT)(transform)
    guarded = model(Factory())
    old_stream = guarded.stream("old") if stream else None
    old_call = asyncio.create_task(anext(old_stream) if stream else guarded.invoke("old"))
    await entered.wait()
    generation = 1
    assert (await guarded.invoke("new")).content == "OK"
    resume.set()
    with pytest.raises(ModelRequestDenied):
        await old_call
    if old_stream is not None:
        await old_stream.aclose()
    assert bound == [0, 1]
    assert len(network[0]) == 1
    assert network[0][0].headers["Authorization"] == "Bearer generation-1"


@pytest.mark.asyncio
async def test_factory_binds_once_for_all_sdk_retries(network):
    sent, replies = network
    replies.append(httpx.Response(503, headers={"retry-after-ms": "1"}, json={"error": {"message": "retry"}}))
    binds, calls = [], []

    class Factory:
        def bind_for_call(self):
            binds.append(True)

            async def authorize(target):
                calls.append(target)
                return {"Authorization": "Bearer fixture"}

            return authorize

    await model(Factory()).invoke("hello")
    assert len(binds) == 1 and len(calls) == len(sent) == 2


@pytest.mark.asyncio
async def test_stream_scope_does_not_leak_and_foreign_task_close_invalidates_inherited_work(network):
    from openjiuwen.core.foundation.llm.request_authority import _current_call, authority_for_http

    captured = []
    ready = asyncio.Event()
    deferred = []

    async def authority(target):
        captured.append(authority_for_http(guarded._client))

        async def later():
            await ready.wait()
            return authority_for_http(guarded._client)

        deferred.append(asyncio.create_task(later()))
        return {"Authorization": "Bearer fixture"}

    guarded = model(authority)
    stream = guarded.stream("hello")
    assert (await anext(stream)).content == "OK"
    assert _current_call.get() is None
    await asyncio.create_task(stream.aclose())
    assert _current_call.get() is None
    ready.set()
    with pytest.raises(ModelRequestDenied):
        await deferred[0]
    with pytest.raises(ModelRequestDenied):
        await captured[0](None)


@pytest.mark.asyncio
async def test_invalid_factory_is_fail_closed_before_transform_or_network(network):
    class Factory:
        def bind_for_call(self):
            raise RuntimeError("private-host-detail")

    with pytest.raises(ModelRequestDenied) as error:
        await model(Factory()).invoke("hello")
    assert "private-host-detail" not in str(error.value)
    assert error.value.__context__ is None
    assert network[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["backup", "retry", "force_finish", "after_error"])
async def test_actual_react_rails_cannot_recover_authority_denial(network, recovery):
    from openjiuwen.core.single_agent import AgentCard, ReActAgent, ReActAgentConfig
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from openjiuwen.core.single_agent.rail.model_backup import ModelBackupRail

    calls, observations = [], []

    async def deny(target):
        calls.append(target)
        return None

    primary = model(deny)
    backup = model(lambda target: None)
    agent = ReActAgent(AgentCard(description="model authority fixture")).configure(
        ReActAgentConfig(
            model_config_obj=ModelRequestConfig(model="fixture"),
            model_client_config=config(),
            prompt_template=[{"role": "system", "content": "fixture"}],
        )
    )
    agent.set_llm(primary)
    backup_rail = ModelBackupRail([backup])

    class RecoveryRail(AgentRail):
        async def on_model_exception(self, ctx):
            observations.append("recover")
            if recovery == "retry" and ctx.retry_attempt == 0:
                ctx.request_retry()
            if recovery == "force_finish":
                ctx.request_force_finish({"result_type": "answer", "output": "not allowed"})

        async def after_model_call(self, ctx):
            observations.append("after")
            if recovery == "after_error":
                raise RuntimeError("observer failure must not replace denial")

    await agent.register_rail(RecoveryRail())
    if recovery == "backup":
        await agent.register_rail(backup_rail)
    with pytest.raises(ModelRequestDenied):
        await agent.invoke({"query": "fixture"})
    assert len(calls) == 1
    assert observations == ["after"]
    assert backup_rail.index == 0
    assert agent._get_llm() is primary
    assert network[0] == []
