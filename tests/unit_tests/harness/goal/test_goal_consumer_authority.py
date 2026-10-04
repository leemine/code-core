"""Real Goal/AbilityManager reads with an original final-invocation consumer."""

import asyncio
import json
from contextvars import copy_context
from types import SimpleNamespace

import pytest
import pytest_asyncio

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import bind_tool_authorizer, current_tool_invocation
from openjiuwen.core.foundation.tool.authority import _capture_tool_consumer_check
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security.permission_engine.host import ToolPermissionHost
from openjiuwen.harness.tools.goal import GetCurrentGoalTool
from tests.unit_tests.harness.goal.test_goal_manager import ManagerHarness

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def case():
    goal = ManagerHarness(output_attached=False)
    original = await goal.manager.set("original private objective")
    holder = SimpleNamespace(callbacks=[], rail=None, checks=0, revoked=False, saved=[], entered=asyncio.Event())
    tool = GetCurrentGoalTool(goal.manager)
    abilities = AbilityManager(owner_id="goal-consumer")
    abilities.add_ability(tool.card, tool)

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
                for callback in holder.callbacks:
                    bind_tool_authorizer(ctx, callback)
                if holder.rail:
                    await holder.rail.before_tool_call(ctx)

    agent = SimpleNamespace(card=SimpleNamespace(id="owner", name="owner"),
                            ability_manager=abilities, agent_callback_manager=Callbacks())

    def check():
        holder.checks += 1
        holder.entered.set()
        current = goal.store.load()
        if (holder.revoked or goal.manager._store is not goal.store or tool._goal_manager is not goal.manager
                or current is None or (current.goal_id, current.revision) != (original.goal_id, original.revision)):
            raise PermissionError("original Goal resource/source expired")
        holder.saved.append(copy_context())

    async def authorize(_):
        proof = current_tool_invocation()
        assert proof.executor is tool
        proof._require_consumer_check()
        proof._bind_consumer_check(check)
        proof._bind_consumer_check(check)
        return True

    async def invoke():
        return await abilities.execute(AgentCallbackContext(agent=agent),
                                       ToolCall(id="goal-call", type="function", name=tool.card.name,
                                                arguments=json.dumps({})), session=goal.session)

    holder.goal, holder.tool, holder.invoke, holder.authorize = goal, tool, invoke, authorize
    holder.original, holder.check = original, check
    yield holder
    abilities.teardown_tools()


async def test_managed_get_uses_original_goal_and_consumer(case):
    case.callbacks = [case.authorize]
    result = await case.invoke()
    assert "original private objective" in str(result)
    assert case.checks >= 4


async def test_existing_native_policy_allow_does_not_require_new_consumer(case):
    seen = []

    async def allow(_):
        seen.append(current_tool_invocation() is not None)
        return True

    case.rail = PermissionInterruptRail(
        config={"enabled": True, "defaults": {"*": "allow"}, "file_guard": {"enabled": False}},
        host=ToolPermissionHost(authorize_tool=allow),
    )
    assert "original private objective" in str(await case.invoke())
    assert seen == [False, False, True]
    assert case.checks == 0


async def test_missing_required_checker_denies_before_read(case):
    async def missing(_):
        current_tool_invocation()._require_consumer_check()
        return True

    case.callbacks = [missing]
    result = await case.invoke()
    assert "PERMISSION_DENIED" in str(result)
    assert "original private objective" not in str(result)


@pytest.mark.parametrize("change", ["revoked", "goal", "manager", "store", "lock"])
async def test_waiting_for_original_lock_rechecks_before_actual_read(case, change):
    case.callbacks = [case.authorize]
    lock = case.goal.manager._control_lock
    await lock.acquire()
    task = asyncio.create_task(case.invoke())
    try:
        await asyncio.wait_for(case.entered.wait(), 1)
        assert not task.done()
        if change == "revoked":
            case.revoked = True
        elif change == "goal":
            data = case.original.to_dict()
            data.update(goal_id="successor", objective="must never disclose successor")
            case.goal.store.save(GoalRecord.from_dict(data))
        elif change == "manager":
            case.tool._goal_manager = ManagerHarness(output_attached=False).manager
        elif change == "store":
            case.goal.manager._store = ManagerHarness(output_attached=False).store
        else:
            case.goal.manager._control_lock = asyncio.Lock()
    finally:
        lock.release()
    result = await asyncio.wait_for(task, 1)
    assert "PERMISSION_DENIED" in str(result)
    assert "original private objective" not in str(result)
    assert "must never disclose successor" not in str(result)
    assert "has_goal" not in str(result)


@pytest.mark.parametrize("change", ["clear_requirement", "replace_checker", "bind_replacement"])
async def test_later_final_callback_cannot_replace_original_consumer(case, change):
    async def replace(_):
        proof = current_tool_invocation()
        if change == "clear_requirement":
            proof._invocation.consumer_required = False
        elif change == "replace_checker":
            proof._invocation.consumer_check = lambda: None
        else:
            proof._bind_consumer_check(lambda: None)
        return True

    case.callbacks = [case.authorize, replace]
    assert "PERMISSION_DENIED" in str(await case.invoke())


@pytest.mark.parametrize("result", [True, "async"])
async def test_consumer_must_be_synchronous_and_return_none(case, result):
    async def invalid_async():
        return None

    def invalid():
        return invalid_async() if result == "async" else result

    async def authorize(_):
        proof = current_tool_invocation()
        proof._require_consumer_check()
        proof._bind_consumer_check(invalid)
        return True

    case.callbacks = [authorize]
    assert "PERMISSION_DENIED" in str(await case.invoke())


async def test_inherited_expired_managed_scope_cannot_downgrade_to_legacy(case):
    case.callbacks = [case.authorize]
    await case.invoke()
    assert case.saved

    async def late():
        with pytest.raises(PermissionError):
            _capture_tool_consumer_check(case.tool)

    await case.saved[-1].run(asyncio.create_task, late())


@pytest.mark.parametrize("change", ["requirement", "checker", "certificate", "executor"])
async def test_actual_consumer_cannot_mutate_its_frozen_certificate(case, change):
    from openjiuwen.core.foundation.tool import authority

    def changing():
        invocation = authority._EXECUTION.get()
        if change == "requirement":
            invocation.consumer_required = False
        elif change == "checker":
            invocation.consumer_check = lambda: None
        elif change == "certificate":
            authority._METHOD_EXECUTION.set(None)
        else:
            case.tool._goal_manager = ManagerHarness(output_attached=False).manager

    async def authorize(_):
        proof = current_tool_invocation()
        proof._require_consumer_check()
        proof._bind_consumer_check(changing)
        return True

    case.callbacks = [authorize]
    result = await case.invoke()
    assert "PERMISSION_DENIED" in str(result)
    assert "original private objective" not in str(result)


async def test_live_inherited_task_cannot_borrow_consumer(case):
    case.callbacks = [case.authorize]
    lock = case.goal.manager._control_lock
    await lock.acquire()
    task = asyncio.create_task(case.invoke())
    try:
        await asyncio.wait_for(case.entered.wait(), 1)

        async def child():
            with pytest.raises(PermissionError):
                _capture_tool_consumer_check(case.tool)

        await case.saved[-1].run(asyncio.create_task, child())
    finally:
        lock.release()
    assert "original private objective" in str(await asyncio.wait_for(task, 1))


async def test_legacy_direct_get_preserves_original_missing_goal_behavior(case):
    assert (await case.tool.invoke({}))["has_goal"] is True
    await case.goal.manager.clear()
    assert (await case.tool.invoke({}))["has_goal"] is False


@pytest.mark.parametrize("change", ["store", "lock", "backing"])
async def test_first_consumer_callback_cannot_retarget_goal_read(case, change):
    replacement = ManagerHarness(output_attached=False)
    replacement.store.save(case.original)

    def reenter():
        if change == "store":
            case.goal.manager._store = replacement.store
        elif change == "lock":
            case.goal.manager._control_lock = asyncio.Lock()
        else:
            case.goal.store._session = replacement.session

    async def authorize(_):
        proof = current_tool_invocation()
        proof._require_consumer_check()
        proof._bind_consumer_check(reenter)
        return True

    case.callbacks = [authorize]
    result = await case.invoke()
    assert "PERMISSION_DENIED" in str(result)
    assert "original private objective" not in str(result)
