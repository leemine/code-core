"""Built-in plan and Team tools keep their registered final authority boundary."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.agent_teams.tools.tool_factory import _wrap_invoke_with_logging, create_team_tools
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import bind_tool_authorizer, current_tool_invocation
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.harness.rails.agent_mode_rail import AgentModeRail
from openjiuwen.harness.schema.state import DeepAgentState
from openjiuwen.harness.tools.agent_mode_tools import EnterPlanModeTool, ExitPlanModeTool, resolve_plan_file_path

pytestmark = [pytest.mark.asyncio, pytest.mark.level0]


@pytest.fixture
def execution():
    holder = SimpleNamespace(callback=None, seen=[])
    manager = AbilityManager(owner_id="builtin-authority-owner")
    session = SimpleNamespace(get_session_id=lambda: "builtin-session", get_state=lambda *_: None)

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL and holder.callback:
                bind_tool_authorizer(ctx, holder.callback)

    agent = SimpleNamespace(
        card=SimpleNamespace(id="owner", name="owner"),
        ability_manager=manager,
        agent_callback_manager=Callbacks(),
        system_prompt_builder=SimpleNamespace(language="en"),
    )

    async def invoke(name, inputs=None):
        return await manager.execute(
            AgentCallbackContext(agent=agent),
            ToolCall(id="call", type="function", name=name, arguments=json.dumps(inputs or {})),
            session=session,
        )

    async def allow(operation):
        proof = current_tool_invocation()
        assert proof is not None and proof.is_current()
        assert proof.original_invoke.__self__ is proof.executor
        assert proof.original_invoke.__func__ is type(proof.executor).invoke
        holder.seen.append(operation.tool_name)
        return True

    holder.manager, holder.session, holder.agent = manager, session, agent
    holder.invoke, holder.allow = invoke, allow
    yield holder
    manager.teardown_tools()


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("phase", ["new", "existing", "empty", "complete"])
async def test_plan_suffix_and_mode_effects_preserved(execution, tmp_path, protected, phase):
    state = DeepAgentState()
    state.plan_mode.plan_slug = "known-plan" if phase != "new" else None
    plan_path = resolve_plan_file_path(str(tmp_path), "known-plan")
    if phase in {"existing", "complete"}:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("Approved plan", encoding="utf-8")
    agent = execution.agent
    agent.deep_config = SimpleNamespace(workspace=SimpleNamespace(root_path=str(tmp_path)))
    agent.load_state = Mock(return_value=state)
    agent.save_state = Mock()
    agent.get_plan_file_path = lambda _: plan_path
    agent.restore_mode_after_plan_exit = Mock()
    rail = AgentModeRail(enter_plan_instructions="ENTER-SUFFIX", exit_plan_notification="EXIT-SUFFIX")
    rail.init(agent)
    execution.callback = execution.allow if protected else None
    name = "enter_plan_mode" if phase in {"new", "existing"} else "exit_plan_mode"
    result = (await execution.invoke(name))[0][0]
    suffix = "ENTER-SUFFIX" if name == "enter_plan_mode" else "EXIT-SUFFIX"
    # Compare the existing direct constructor contract without configuration:
    # only the original two-newline suffix differs, no duplicate injection.
    baseline_tool = EnterPlanModeTool(agent, "en") if name == "enter_plan_mode" else ExitPlanModeTool(agent, "en")
    baseline = await baseline_tool.invoke({}, session=execution.session)
    assert result == baseline + "\n\n" + suffix
    assert execution.seen == ([name] if protected else [])
    if phase == "new":
        agent.save_state.assert_called_once()
    elif phase == "complete":
        assert agent.restore_mode_after_plan_exit.call_count == 2
    else:
        agent.restore_mode_after_plan_exit.assert_not_called()


@pytest.mark.parametrize("name", ["enter_plan_mode", "exit_plan_mode"])
async def test_plan_denial_never_reads_or_mutates_state(execution, name):
    agent = execution.agent
    agent.load_state = Mock(side_effect=AssertionError("denied tool ran"))
    agent.get_plan_file_path = Mock(side_effect=AssertionError("denied tool ran"))
    AgentModeRail(enter_plan_instructions="ENTER", exit_plan_notification="EXIT").init(agent)

    async def deny(_):
        assert current_tool_invocation() is not None
        return False

    execution.callback = deny
    assert "PERMISSION_DENIED" in str(await execution.invoke(name))
    agent.load_state.assert_not_called()
    agent.get_plan_file_path.assert_not_called()


@pytest.mark.parametrize("decision", [True, False, None])
async def test_team_logging_and_structured_result_follow_final_authority(execution, monkeypatch, decision):
    records = Mock(return_value=[])
    parent = SimpleNamespace(async_tool_runtime=SimpleNamespace(list_all=records))
    backend = TeamBackend(team_name="review", member_name="leader", is_leader=True, db=Mock(), messager=Mock())
    tools = create_team_tools(role="leader", agent_team=backend, parent_agent=parent, lang="en")
    tool = next(item for item in tools if item.card.name == "async_tasks_list")
    original_outer = tool.invoke
    _wrap_invoke_with_logging(tool)
    _wrap_invoke_with_logging(tool)
    assert tool.invoke is original_outer
    execution.manager.add_ability(tool.card, tool)
    logs = Mock()
    monkeypatch.setattr("openjiuwen.core.common.logging.team_logger.debug", logs)

    async def authority(operation):
        await execution.allow(operation)
        return decision

    execution.callback = authority if decision is not None else None
    result = await execution.invoke(tool.card.name)
    if decision is False:
        assert "PERMISSION_DENIED" in str(result)
        records.assert_not_called()
        logs.assert_not_called()
    else:
        records.assert_called_once_with()
        output, message = result[0]
        assert output.success is True and output.data == {"tasks": []}
        assert message.content == "No async tasks."
        assert logs.call_args_list[0].args == ("[async_tasks_list] invoke start, inputs={}",)
        assert logs.call_args_list[1].args == (f"[async_tasks_list] invoke end, output={output}",)
        assert logs.call_count == 2
