"""Exercise existing model HTTP guards and concurrent source subprocess scopes."""

import asyncio
import json
import os

import httpx
import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.context_engine.processor.compressor.round_level_compressor import RoundLevelCompressor
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig, UserMessage
from openjiuwen.harness.personal_context import PersonalContext, agent_support
from openjiuwen.harness.personal_context.context_pipeline import _profile_fallback_allowed
from openjiuwen.harness.personal_context.fetch import feishu


@pytest.mark.asyncio
async def test_personal_context_compressor_uses_live_authority_before_http(monkeypatch):
    sent, calls = [], []
    allowed = [True]

    async def authorize(target):
        calls.append(target)
        if not allowed[0]:
            raise PermissionError("revoked")
        return {"Authorization": "Bearer current-fixture-only"}

    async def transport(_self, request):
        sent.append(request.headers["Authorization"])
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    rail = agent_support._make_context_processor_rail(
        ModelClientConfig(client_provider="OpenAI", api_key="must-not-send", api_base="https://model.invalid/v1"),
        ModelRequestConfig(model="test"),
        authorize,
    )
    _, config = rail._user_processors[0]
    assert "request_authority" not in config.model_dump()
    model = RoundLevelCompressor(config)._get_model()
    await model.invoke([UserMessage(content="synthetic context")])
    allowed[0] = False
    with pytest.raises(BaseError) as error:
        await model.invoke([UserMessage(content="second synthetic context")])
    assert not _profile_fallback_allowed(error.value)
    assert not agent_support._is_repairable_invoke_error(error.value)
    assert sent == ["Bearer current-fixture-only"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_balanced_pipeline_denial_does_not_publish_rules_fallback(tmp_path, monkeypatch):
    from tests.unit_tests.harness.personal_context.test_balanced_two_stage import _batch, _service

    service = _service(tmp_path)
    calls = []

    async def deny(target):
        calls.append(target)
        raise PermissionError("denied")

    service._model_request_authority = deny

    async def network(*_):
        pytest.fail("unauthorized model reached HTTP transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    await service._process_batch_event("local", "denied-run", _batch())
    with pytest.raises(BaseError):
        await service._finish_run_event("local", "denied-run")
    assert calls
    assert not (tmp_path / "workspace/context/description.md").exists()


@pytest.mark.asyncio
async def test_two_instance_feishu_status_uses_private_environment_without_global_mutation(tmp_path, monkeypatch):
    seen = []
    original = dict(os.environ)
    monkeypatch.setattr(feishu.shutil, "which", lambda _: "/fixture/lark-cli")

    async def spawn(binary, *args, **kwargs):
        await asyncio.sleep(0)
        seen.append(kwargs.get("env"))

        class Process:
            returncode = 0

            async def communicate(self):
                return json.dumps({"authenticated": False}).encode(), b""

        return Process()

    monkeypatch.setattr(feishu.asyncio, "create_subprocess_exec", spawn)
    a = PersonalContext(home=tmp_path / "a", fetch_environment={"HOME": str(tmp_path / "a")})
    b = PersonalContext(home=tmp_path / "b", fetch_environment={"HOME": str(tmp_path / "b")})

    async def scopes(_self, _provider):
        return ("read",)

    monkeypatch.setattr(PersonalContext, "_required_authorization_scopes", scopes)
    await asyncio.gather(a.get_authorization_status("feishu"), b.get_authorization_status("feishu"))
    assert sorted(item["HOME"] for item in seen) == sorted([str(tmp_path / "a"), str(tmp_path / "b")])
    assert dict(os.environ) == original
    assert feishu._cli_environment.get() is None


@pytest.mark.asyncio
async def test_execution_revoked_before_publication_does_not_commit(tmp_path):
    from tests.unit_tests.harness.personal_context.test_balanced_two_stage import _batch, _service

    service = _service(tmp_path)
    # Exercise the original rules pipeline without a model, then revoke its
    # host authority at the final publication boundary.
    service._config = service._config.model_copy(update={"strategy_profile": "rules"})

    def deny():
        raise PermissionError("collection revoked")

    service._execution_check = deny
    await service._process_batch_event("local", "revoked-run", _batch())
    with pytest.raises(PermissionError, match="collection revoked"):
        await service._finish_run_event("local", "revoked-run")
    assert not (tmp_path / "workspace/context/description.md").exists()


@pytest.mark.asyncio
async def test_private_cli_cancellation_waits_for_process_exit(monkeypatch):
    started, killed, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Process:
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            killed.set()

        async def wait(self):
            await exited.wait()
            return -9

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(feishu.shutil, "which", lambda _: "/fixture/lark-cli")
    monkeypatch.setattr(feishu.asyncio, "create_subprocess_exec", spawn)
    token = feishu._cli_environment.set({"HOME": "/fixture/private"})
    try:
        task = asyncio.create_task(feishu._run_lark_cli_once(["auth", "status"]))
    finally:
        feishu._cli_environment.reset(token)
    await started.wait()
    task.cancel()
    await killed.wait()
    await asyncio.sleep(0)
    assert not task.done()
    exited.set()
    with pytest.raises(asyncio.CancelledError):
        await task
