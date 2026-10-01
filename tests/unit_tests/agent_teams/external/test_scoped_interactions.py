"""Exact member replies exercise the existing IO ledger and Runner manager."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.external.member_runtime import ExternalHarnessMemberRuntime
from openjiuwen.agent_teams.external.interaction_address import decode_interaction_address, encode_interaction_address
from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager
from openjiuwen.core.session import InteractiveInput
from openjiuwen.harness_protocol import ToolApprovalRequest, ToolApprovalDecision, HarnessStateError, UserInputRequest
from tests.unit_tests.agent_teams.external.test_member_runtime import _FakeHarness, _context


def reply(key, value):
    answer = InteractiveInput()
    answer.update(key, value)
    return answer


async def pending(runtime, *, approval=True):
    request = (ToolApprovalRequest(request_id='same', call_id='tool', tool_name='shell') if approval
               else UserInputRequest(request_id='same', prompt='Choose', choices=('yes', 'no')))
    task = asyncio.create_task(runtime.request_interaction(request))
    chunk = await asyncio.wait_for(anext(runtime.outputs()), 1)
    return task, chunk.payload.id


@pytest.mark.asyncio
@pytest.mark.parametrize('approval', [True, False])
async def test_same_provider_id_is_scoped_and_answer_cannot_be_replayed(approval):
    runtimes = [ExternalHarnessMemberRuntime(harness=_FakeHarness(), context=_context(),
                auto_approve_tools=False, interaction_scope=('team', member, 'root'))
                for member in ('leader', 'worker')]
    tasks = []
    try:
        for runtime in runtimes:
            await runtime.start()
        first, first_id = await pending(runtimes[0], approval=approval)
        second, second_id = await pending(runtimes[1], approval=approval)
        tasks.extend((first, second))
        assert first_id != second_id
        value = {'approved': False} if approval else {'answers': {'Choose': 'yes'}}
        with pytest.raises(HarnessStateError):
            await runtimes[1].send(reply(first_id, value))
        assert not first.done() and not second.done()
        await runtimes[0].send(reply(first_id, value))
        with pytest.raises(HarnessStateError):
            await runtimes[0].send(reply(first_id, value))
        response = await first
        assert response.request_id == 'same'
        if approval:
            assert response.decision is ToolApprovalDecision.DENY
        assert not second.done()
        await runtimes[1].stop()
        await second
        await runtimes[1].start()
        third, third_id = await pending(runtimes[1], approval=approval)
        tasks.append(third)
        assert third_id != second_id
        with pytest.raises(HarnessStateError):
            await runtimes[1].send(reply(second_id, value))
        await runtimes[1].send(reply(third_id, value))
        await third
        assert all(not runtime.harness.send_calls for runtime in runtimes)
        assert [runtime.harness.events_calls for runtime in runtimes] == [1, 2]
    finally:
        for runtime in runtimes:
            await runtime.stop()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['session', 'member', 'team', 'cycle', 'raw', 'malformed', 'missing_approved'])
async def test_invalid_reply_does_not_consume_pending_or_start_turn(mutation):
    runtime = ExternalHarnessMemberRuntime(harness=_FakeHarness(), context=_context(),
                auto_approve_tools=False, interaction_scope=('team', 'worker', 'root'))
    await runtime.start()
    task, key = await pending(runtime)
    try:
        parts = list(decode_interaction_address(key))
        if mutation in ('session', 'member', 'team', 'cycle'):
            parts[{'team': 0, 'member': 1, 'session': 2, 'cycle': 3}[mutation]] += '-forged'
            key = encode_interaction_address(*parts)
        answer = reply(key, {} if mutation == 'missing_approved' else {'approved': True})
        if mutation == 'raw':
            answer = InteractiveInput({'approved': True})
        if mutation == 'malformed':
            answer = reply('team-interaction:bad', {'approved': True})
        with pytest.raises(HarnessStateError):
            await runtime.send(answer)
        assert not task.done()
        assert runtime.harness.send_calls == []
    finally:
        await runtime.stop()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize('temporary', [False, True])
async def test_manager_routes_reply_to_original_owner_and_reports_stale(temporary):
    owner = 'review:invocation' if temporary else 'worker'
    runtime = ExternalHarnessMemberRuntime(harness=_FakeHarness(), context=_context(),
                auto_approve_tools=False, interaction_scope=('team', owner, 'root'))
    await runtime.start()
    task, key = await pending(runtime)
    manager = TeamRuntimeManager()
    worker = SimpleNamespace(harness=runtime)
    leader = SimpleNamespace(spec=SimpleNamespace(execution_provider='codex'),
               blueprint=SimpleNamespace(member_name='leader'),
               spawn_manager=SimpleNamespace(lookup_inprocess_agent=lambda name: worker if name == 'worker' else None),
               coordination=SimpleNamespace(scheduler=SimpleNamespace(
                   review_interaction_target=lambda name: runtime if temporary and name == owner else None)))
    manager._resolve_entry = AsyncMock(return_value=SimpleNamespace(agent=leader, team_name='team', current_session_id='root'))
    try:
        assert await manager.interact(reply(key, {'approved': True}), team_name='team', session_id='root')
        assert not await manager.interact(reply(key, {'approved': True}), team_name='team', session_id='root')
        assert (await task).decision is ToolApprovalDecision.ALLOW
        assert runtime.harness.send_calls == []
    finally:
        await runtime.stop()
