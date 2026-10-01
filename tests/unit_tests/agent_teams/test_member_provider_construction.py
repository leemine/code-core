# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the actual leader, process payload and recovery construction paths."""
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.agent_teams import (
    TEAM_MEMBER_RUNTIME_FACTORY, TeamAgent, TeamAgentSpec, TeamMemberRuntimeBuild,
)
from openjiuwen.agent_teams.agent.agent_configurator import AgentConfigurator
from openjiuwen.agent_teams.external.member_runtime import ExternalHarnessMemberRuntime
from openjiuwen.agent_teams.schema.blueprint import DeepAgentSpec, StorageSpec
from openjiuwen.agent_teams.schema.build_context import BuildContext
from openjiuwen.agent_teams.schema.team import TeamRole, TeamRuntimeContext
from openjiuwen.harness_protocol import HarnessCard, HarnessContext, HarnessState
from tests.unit_tests.agent_teams.external.test_member_runtime import _FakeHarness


class Factory:
    def __init__(self, provider='codex'):
        self.provider_id = provider
        self.requests = []
        self.runtimes = []

    def validate_team_spec(self, spec):
        assert spec.execution_provider == self.provider_id

    def build_member_runtime(self, request: TeamMemberRuntimeBuild):
        self.requests.append(request)
        harness = _FakeHarness(state=HarnessState.TERMINATED)
        harness._card = HarnessCard(name=self.provider_id, implementation_version='test')
        runtime = ExternalHarnessMemberRuntime(harness=harness, context=HarnessContext(
            agent_name=request.context.member_name, agent_id=request.card.id,
            host_session_id='session', system_prompt='', metadata={'role': request.context.role.value},
        ))
        self.runtimes.append(runtime)
        return runtime


def spec_for(factory=None, **overrides):
    values = dict(
        agents={'leader': DeepAgentSpec(), 'teammate': DeepAgentSpec()},
        team_name='provider-team', execution_provider='codex', evolution_enabled=False,
        spawn_mode='inprocess', storage=StorageSpec(type='memory'),
        build_context=BuildContext(extras={TEAM_MEMBER_RUNTIME_FACTORY: factory}) if factory else None,
    )
    values.update(overrides)
    return TeamAgentSpec(**values)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENJIUWEN_HOME', str(tmp_path))
    # Any implicit Native construction is a failure, including for the leader.
    from openjiuwen.agent_teams.harness.team_harness import TeamHarness
    monkeypatch.setattr(TeamHarness, 'build', Mock(side_effect=AssertionError('Native fallback')))
    from openjiuwen.agent_teams.spawn.shared_resources import cleanup_shared_resources
    cleanup_shared_resources()
    yield
    cleanup_shared_resources()


@pytest.mark.parametrize('provider', ['codex', 'opencode'])
def test_leader_uses_selected_provider_without_a_native_model(provider):
    factory = Factory(provider)
    spec = spec_for(factory, execution_provider=provider)
    before = spec.model_dump(mode='json')
    leader = spec.build()
    assert leader.harness is factory.runtimes[0]
    assert factory.requests[0].context.role is TeamRole.LEADER
    assert factory.requests[0].team_backend is leader.team_backend
    assert leader.build_context.member_name == spec.leader.member_name
    assert 'build_context' not in before
    assert before['execution_provider'] == provider
    assert leader.harness.harness.start_contexts == []


@pytest.mark.parametrize('factory,provider', [(None, 'codex'), (Factory('opencode'), 'codex')])
def test_unavailable_or_mismatched_factory_fails_before_infrastructure(monkeypatch, factory, provider):
    infra = Mock(side_effect=AssertionError('allocated before admission'))
    monkeypatch.setattr(AgentConfigurator, 'setup_infra', infra)
    with pytest.raises(ValueError, match='factory'):
        spec_for(factory, execution_provider=provider).build()
    infra.assert_not_called()


@pytest.mark.parametrize('field,value', [('enable_swarmflow', True), ('enable_fork', True),
    ('enable_hitt', True), ('enable_bridge', True),
    ('tiny_agents', {'helper': {'system_prompt': 'help', 'model_name': 'model'}}),
    ('external_cli_agents', [{'cli_agent': 'codex'}])])
def test_unadapted_llm_paths_do_not_fall_back_to_native(field, value):
    with pytest.raises(ValueError, match='does not yet support'):
        spec_for(Factory(), **{field: value}).build()


@pytest.mark.asyncio
async def test_serialized_member_payload_rebuilds_factory_and_retains_member_identity(monkeypatch):
    from openjiuwen.agent_teams.agent.payload import SpawnPayloadBuilder
    import openjiuwen.harness.schema.build_context as carriers
    factory = Factory()
    spec = spec_for(factory, build_context_seed={'test_execution': 'codex'})
    leader = spec.build()
    context = TeamRuntimeContext(
        role=TeamRole.TEAMMATE, member_name='reviewer',
        team_spec=leader.blueprint.ctx.team_spec,
        db_config=leader.blueprint.ctx.db_config,
        messager_config=leader.blueprint.ctx.messager_config,
    )
    payload = SpawnPayloadBuilder(spec, leader.blueprint.ctx).build_spawn_config(context).payload
    monkeypatch.setattr(carriers, '_BUILD_CONTEXT_FACTORY',
                        lambda seed: BuildContext(extras={TEAM_MEMBER_RUNTIME_FACTORY: factory}))
    member = await TeamAgent.from_spawn_payload(payload)
    assert factory.requests[-1].context.member_name == 'reviewer'
    assert factory.requests[-1].context.role is TeamRole.TEAMMATE
    assert member.harness is not leader.harness
    assert member.harness.harness is not leader.harness.harness
    assert member.build_context.member_name == 'reviewer'
    monkeypatch.setattr(carriers, '_BUILD_CONTEXT_FACTORY', None)
    with pytest.raises(ValueError, match='requires a host'):
        await TeamAgent.from_spawn_payload(payload)


@pytest.mark.asyncio
async def test_inprocess_spawn_constructs_independent_member_and_retains_runner_contract(monkeypatch):
    from openjiuwen.agent_teams.spawn.inprocess_spawn import inprocess_spawn
    from openjiuwen.core.runner import Runner
    factory = Factory()
    leader = spec_for(factory).build()
    context = leader.blueprint.ctx.model_copy(update={
        'role': TeamRole.TEAMMATE, 'member_name': 'worker',
    })
    runner = AsyncMock(return_value=None)
    monkeypatch.setattr(Runner, 'run_agent_team', runner)
    handle = await inprocess_spawn(leader, context, session_id='team-session')
    assert await handle.wait_for_completion() == 0
    member = handle.agent_ref
    assert member.harness is factory.runtimes[1]
    assert member.harness is not leader.harness
    assert member.card.id != leader.card.id
    runner.assert_awaited_once_with(member, {'query': ''}, member=True, session='team-session')


@pytest.mark.parametrize('state,session', [(HarnessState.IDLE, None),
                                         (HarnessState.TERMINATED, 'old-session')])
def test_factory_cannot_return_started_or_used_runtime(state, session):
    factory = Factory()
    build = factory.build_member_runtime

    def used(request):
        runtime = build(request)
        runtime.harness.state = state
        runtime.harness._session_id = session
        return runtime

    factory.build_member_runtime = used
    with pytest.raises(ValueError, match='fresh unstarted'):
        spec_for(factory).build()


def test_factory_cannot_return_another_provider():
    factory = Factory()
    build = factory.build_member_runtime

    def wrong(request):
        runtime = build(request)
        runtime.harness._card = HarnessCard(name='deepagent', implementation_version='test')
        return runtime

    factory.build_member_runtime = wrong
    with pytest.raises(ValueError, match='Provider does not match'):
        spec_for(factory).build()


def test_cold_recovery_retains_provider_and_refuses_switch():
    from openjiuwen.core.session.agent_team import create_agent_team_session
    from openjiuwen.agent_teams.runtime.metadata import write_team_namespace
    factory = Factory()
    spec = spec_for(factory)
    leader = spec.build()
    session = create_agent_team_session(session_id='recovery-session', team_id=spec.team_name)
    write_team_namespace(session, spec.team_name, {
        'spec': spec.model_dump(mode='json'),
        'context': leader.blueprint.ctx.model_dump(mode='json'),
    })
    recovered = TeamAgent.recover_from_session(session, spec.team_name, runtime_spec=spec)
    assert recovered.harness is not leader.harness
    assert recovered.spec.execution_provider == 'codex'
    with pytest.raises(ValueError, match='immutable'):
        TeamAgent.recover_from_session(session, spec.team_name,
                                      runtime_spec=spec_for(execution_provider='native'))


def test_legacy_runtime_injection_is_rejected_for_uniform_provider_before_infra(monkeypatch):
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    infra = Mock(side_effect=AssertionError('allocated'))
    monkeypatch.setattr(AgentConfigurator, 'setup_infra', infra)
    agent = TeamAgent(AgentCard(id='worker', name='worker'))
    with pytest.raises(ValueError, match='host factory'):
        agent.configure(spec_for(Factory()), TeamRuntimeContext(role=TeamRole.TEAMMATE),
                        member_runtime=object())
    infra.assert_not_called()
