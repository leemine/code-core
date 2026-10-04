"""Live member/child model factories; actual Model and SDK construction."""

import asyncio
import json

import httpx
import pytest

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, SubAgentSpec, TeamModelConfig

pytestmark = pytest.mark.level0


def model_spec():
    return TeamModelConfig(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_base="https://model.invalid/v1",
            api_key="placeholder",
            credential_reference="catalog:first",
        ),
        model_request_config=ModelRequestConfig(model="fixture-model"),
    )


def child(**kwargs):
    return SubAgentSpec(agent_card=AgentCard(id="child-id", name="child"), system_prompt="test", **kwargs)


def member_spec(**kwargs):
    return DeepAgentSpec(
        model=model_spec(),
        card=AgentCard(id="member-card", name="member"),
        enable_sys_operation=False,
        enable_security_rail=False,
        **kwargs,
    )


def test_legacy_build_is_an_actual_model():
    assert isinstance(model_spec().build(), Model)
    assert isinstance(model_spec().build(BuildContext()), Model)


def test_member_factory_sees_derived_identity_and_spec():
    observed = []

    def factory(spec, context):
        observed.append((spec, context))
        return spec.build()

    base = BuildContext(member_name="worker", role="teammate", member_card_id="old", model_factory=factory)
    spec = member_spec()
    parts = spec.resolve_parts(base)
    assert isinstance(parts.config.model, Model)
    assert observed[0][0] is spec.model
    assert observed[0][1].member_card_id == "member-card"
    assert observed[0][1].member_name == "worker"
    assert observed[0][1].subagent_name is None
    assert base.member_card_id == "old" and base.extras == {}


@pytest.mark.parametrize("explicit", [False, True])
def test_child_is_reconstructed_with_distinct_subject(explicit):
    built = []

    def factory(spec, context):
        value = spec.build()
        built.append((context, value))
        return value

    parent_spec = model_spec()
    parent = parent_spec.build()
    context = BuildContext(
        member_name="worker",
        role="teammate",
        model_factory=factory,
        extras={"_parent_model_spec": parent_spec, "_parent_model": parent},
    )
    result = child(model=model_spec() if explicit else None).build(parent_model=parent, language="en", context=context)
    assert isinstance(result.model, Model) and result.model is not parent
    assert built[0][0].subagent_name == "child-id"
    assert built[0][0].member_name == "worker"
    assert context.subagent_name is None and context.extras["_parent_model"] is parent


def test_child_missing_spec_cannot_inherit_parent_instance():
    parent = model_spec().build()
    context = BuildContext(model_factory=lambda *_: pytest.fail("factory without spec"))
    with pytest.raises(ValueError, match="subagent model spec"):
        child().build(parent_model=parent, language="en", context=context)


def test_child_factory_rejection_propagates_without_parent_fallback():
    def deny(spec, context):
        assert context.subagent_name == "child-id"
        raise PermissionError("child lacks independent authority")

    with pytest.raises(PermissionError):
        child(model=model_spec()).build(
            parent_model=model_spec().build(), language="en", context=BuildContext(model_factory=deny)
        )


@pytest.mark.parametrize("name", ["core.subagent.explore", "unknown-provider"])
def test_arbitrary_provider_is_rejected_before_provider_callback(name):
    with pytest.raises(ValueError, match="subagent providers"):
        child(factory_name=name).build(
            parent_model=model_spec().build(),
            language="en",
            context=BuildContext(model_factory=lambda *_: pytest.fail("called")),
        )


@pytest.mark.parametrize("spec", [DeepAgentSpec(), member_spec(add_general_purpose_agent=True)])
def test_implicit_root_or_child_model_is_rejected(spec):
    with pytest.raises(ValueError, match="model_factory requires"):
        spec.resolve_parts(BuildContext(model_factory=lambda *_: pytest.fail("called")))


def test_invalid_factory_return_does_not_select_legacy_model():
    with pytest.raises(TypeError, match="must return a Model"):
        model_spec().build(BuildContext(model_factory=lambda *_: None))


@pytest.mark.asyncio
async def test_real_pool_native_harness_and_sdk_each_use_member_factory(monkeypatch):
    from openjiuwen.agent_teams.harness.native_harness import NativeHarness
    from openjiuwen.agent_teams.models.pool import ModelPoolEntry

    requests = []
    bound = []

    async def send(transport, request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)

    def factory(spec, context):
        assert context.subagent_name is None
        member = context.member_name
        reference = spec.model_client_config.credential_reference

        async def authorize(target):
            bound.append((member, reference, target.model))
            return {"Authorization": "Bearer synthetic-" + member}

        return Model(spec.model_client_config, spec.model_request_config, request_authority=authorize)

    for member in ("leader", "worker"):
        pool = ModelPoolEntry(
            model_name="fixture-model",
            api_provider="OpenAI",
            api_base_url="https://model.invalid/v1",
            api_key="placeholder",
            metadata={"client": {"credential_reference": "catalog:" + member}},
        )
        # Real JSON roundtrip, allocation materializer and NativeHarness creation.
        pool = ModelPoolEntry.model_validate_json(pool.model_dump_json())
        spec = member_spec().model_copy(update={"model": pool.to_team_model_config()})
        harness = NativeHarness(spec, BuildContext(member_name=member, role=member, model_factory=factory))
        await asyncio.wait_for(harness.deep_config.model.invoke("hello"), timeout=3)
        assert "model_factory" not in json.loads(spec.model_dump_json())
    assert [(member, ref) for member, ref, _ in bound] == [("leader", "catalog:leader"), ("worker", "catalog:worker")]
    assert [r.headers["authorization"] for r in requests] == ["Bearer synthetic-leader", "Bearer synthetic-worker"]
