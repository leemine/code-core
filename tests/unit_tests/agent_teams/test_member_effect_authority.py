import pytest

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.paths import configure_openjiuwen_home, reset_openjiuwen_home, team_session_worktrees_dir
from openjiuwen.agent_teams.tools.database import MemberRecordDenied
from tests.unit_tests.agent_teams.test_member_record_authority import case as member_record_case

case = member_record_case


@pytest.mark.asyncio
async def test_denied_original_owner_must_not_clean_worktree_before_dao(case, tmp_path):
    c = case
    await c.create()
    configure_openjiuwen_home(tmp_path / "isolated-home")
    token = set_session_id("fixture-session")
    try:
        marker = team_session_worktrees_dir("same-team", "fixture-session") / "owned.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("synthetic owned fixture only")
        with pytest.raises(MemberRecordDenied):
            await c.db.force_delete_team_session("same-team")
        assert await c.db.member.get_member("member", "same-team") is not None
        assert marker.exists(), "Original cleanup ran before denied member DAO authorization"
    finally:
        reset_session_id(token)
        reset_openjiuwen_home()


@pytest.mark.asyncio
async def test_spawn_denied_source_never_writes_identity(case, tmp_path):
    from unittest.mock import AsyncMock

    from openjiuwen.agent_teams.messager import Messager
    from openjiuwen.agent_teams.paths import team_member_workspace_dir
    from openjiuwen.agent_teams.team_workspace.manager import TeamWorkspaceManager
    from openjiuwen.agent_teams.team_workspace.models import TeamWorkspaceConfig
    from openjiuwen.agent_teams.team_workspace.workspace_cache import WorkspaceCache
    from openjiuwen.agent_teams.team_workspace.workspace_store import WorkspaceStore
    from openjiuwen.agent_teams.tools.team import TeamBackend
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    configure_openjiuwen_home(tmp_path / "spawn-home")
    try:
        backend = TeamBackend("same-team", "leader", True, case.db, AsyncMock(spec=Messager))
        manager = TeamWorkspaceManager(TeamWorkspaceConfig(), str(tmp_path / "workspace"), "same-team")
        manager.attach_workspace_cache(WorkspaceCache(WorkspaceStore(), "same-team"))
        backend.attach_workspace_manager(manager)
        with pytest.raises(MemberRecordDenied):
            await backend.spawn_member("new-member", "Member", AgentCard(name="new-member"), desc="synthetic body")
        assert not team_member_workspace_dir("same-team", "new-member").exists()
    finally:
        reset_openjiuwen_home()


@pytest.fixture
def backend_factory(tmp_path):
    from unittest.mock import AsyncMock

    from openjiuwen.agent_teams.messager import Messager
    from openjiuwen.agent_teams.team_workspace.manager import TeamWorkspaceManager
    from openjiuwen.agent_teams.team_workspace.models import TeamWorkspaceConfig
    from openjiuwen.agent_teams.team_workspace.workspace_cache import WorkspaceCache
    from openjiuwen.agent_teams.team_workspace.workspace_store import WorkspaceStore
    from openjiuwen.agent_teams.tools.team import TeamBackend

    configure_openjiuwen_home(tmp_path / "effect-home")

    def build(db):
        backend = TeamBackend("same-team", "leader", True, db, AsyncMock(spec=Messager))
        manager = TeamWorkspaceManager(TeamWorkspaceConfig(), str(tmp_path / "workspace"), "same-team")
        manager.attach_workspace_cache(WorkspaceCache(WorkspaceStore(), "same-team"))
        backend.attach_workspace_manager(manager)
        return backend

    try:
        yield build
    finally:
        reset_openjiuwen_home()


def allow_effect(c, committed=None, check=None):
    from openjiuwen.agent_teams.tools.database import MemberEffectPermit

    def bind(operation, origin):
        assert operation.database is c.db and origin is c.origin
        return MemberEffectPermit(
            operation,
            origin,
            c.entity,
            c.actor,
            "alice-source",
            check or (lambda: None),
            committed or (lambda receipt: c.receipts.__setitem__(operation.member_name, receipt)),
        )

    object.__setattr__(c.authorizer, "bind_for_effect", bind)


@pytest.mark.asyncio
async def test_original_effect_preserves_real_workspace_identity_and_receipt(case, backend_factory):
    from openjiuwen.agent_teams.paths import team_member_workspace_dir
    from openjiuwen.agent_teams.tools.database import MemberWriteReceipt
    from openjiuwen.core.controller.schema.execution_origin import execution_origin_scope
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    c = case
    allow_effect(c)
    backend = backend_factory(c.db)
    with execution_origin_scope(c.origin):
        result = await backend.spawn_member("new-member", "New", AgentCard(name="new-member"), desc="synthetic body")
        receipt = c.receipts["new-member"]
        receipt.check_current()
    assert result.ok and type(receipt) is MemberWriteReceipt
    assert receipt.operation.database is c.db
    row = await c.db.member.get_member("new-member", "same-team")
    assert row.desc == "synthetic body"
    assert (
        "synthetic body"
        in (team_member_workspace_dir("same-team", "new-member") / "prompts/identity/card.md").read_text()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["owner", "database", "home", "card"])
async def test_await_before_workspace_rechecks_original_effect(case, backend_factory, monkeypatch, tmp_path, drift):
    from openjiuwen.agent_teams.paths import team_member_workspace_dir
    from openjiuwen.core.controller.schema.execution_origin import execution_origin_scope
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    c = case
    active = True

    def check():
        if not active:
            raise MemberRecordDenied("original owner expired")

    allow_effect(c, check=check)
    backend = backend_factory(c.db)
    card = AgentCard(name="new-member")
    original_root = team_member_workspace_dir("same-team", "new-member")
    original = c.db.team.team_exists

    async def drift_after_read(name):
        nonlocal active
        found = await original(name)
        if drift == "owner":
            active = False
        elif drift == "database":
            backend.db = object()
        elif drift == "home":
            configure_openjiuwen_home(tmp_path / "replacement-home")
        else:
            card.name = "changed"
        return found

    monkeypatch.setattr(c.db.team, "team_exists", drift_after_read)
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        await backend.spawn_member("new-member", "New", card, desc="body")
    assert not original_root.exists()
    assert await c.db.member.get_member("new-member", "same-team") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raise", "source", "receipt"])
async def test_committed_callback_failure_preserves_commit_fact(case, backend_factory, failure):
    from openjiuwen.agent_teams.tools.database import MemberWriteCommittedButUnconfirmed
    from openjiuwen.core.controller.schema.execution_origin import execution_origin_scope
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    c = case
    received = []

    def committed(receipt):
        received.append(receipt)
        if failure == "raise":
            raise RuntimeError("synthetic callback failure")
        if failure == "source":
            c.enabled = False
        if failure == "receipt":
            object.__setattr__(receipt, "_transaction", object())

    allow_effect(c, committed=committed)
    backend = backend_factory(c.db)
    with execution_origin_scope(c.origin), pytest.raises(MemberWriteCommittedButUnconfirmed) as caught:
        await backend.spawn_member("new-member", "New", AgentCard(name="new-member"), desc="body")
    assert received == [caught.value.receipt]
    row = await c.db.member.get_member("new-member", "same-team")
    assert row is not None and row.record_revision == 1


@pytest.mark.asyncio
async def test_legacy_backend_workspace_and_delete_still_work(case, backend_factory):
    from openjiuwen.agent_teams.paths import team_member_workspace_dir
    from openjiuwen.agent_teams.tools.database import TeamDatabase
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    legacy = TeamDatabase(case.config)
    await legacy.initialize()
    backend = backend_factory(legacy)
    token = set_session_id("legacy-session")
    try:
        result = await backend.spawn_member("new-member", "New", AgentCard(name="new-member"), desc="legacy body")
        assert result.ok
        assert team_member_workspace_dir("same-team", "new-member").exists()
        marker = team_session_worktrees_dir("same-team", "legacy-session") / "owned.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("legacy fixture")
        assert await legacy.force_delete_team_session("same-team") is True
        assert not marker.exists()
    finally:
        reset_session_id(token)
        await legacy.close()


@pytest.mark.asyncio
async def test_committed_callback_reentry_never_becomes_success(case, backend_factory, monkeypatch):
    from openjiuwen.agent_teams.tools.database import MemberWriteCommittedButUnconfirmed
    from openjiuwen.agent_teams.tools.database.effect_authority import BoundMemberEffect
    from openjiuwen.core.controller.schema.execution_origin import execution_origin_scope
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    c = case
    bound = []
    actual = BoundMemberEffect.committed

    def remember(effect, receipt):
        bound.append(effect)
        return actual(effect, receipt)

    monkeypatch.setattr(BoundMemberEffect, "committed", remember)
    allow_effect(c, committed=lambda receipt: bound[0].check())
    with execution_origin_scope(c.origin), pytest.raises(MemberWriteCommittedButUnconfirmed):
        await backend_factory(c.db).spawn_member("new-member", "New", AgentCard(name="new-member"), desc="body")
    assert (await c.db.member.get_member("new-member", "same-team")).record_revision == 1


@pytest.mark.asyncio
async def test_source_less_legacy_dao_cannot_clean_existing_governed_workspace(case, tmp_path):
    from openjiuwen.agent_teams.tools.database import TeamDatabase

    await case.create()
    legacy = TeamDatabase(case.config)
    await legacy.initialize()
    configure_openjiuwen_home(tmp_path / "source-less-home")
    token = set_session_id("original-session")
    marker = team_session_worktrees_dir("same-team", "original-session") / "original.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("preserved")
    try:
        with pytest.raises(MemberRecordDenied):
            await legacy.force_delete_team_session("same-team")
        assert marker.read_text() == "preserved"
    finally:
        reset_session_id(token)
        reset_openjiuwen_home()
        await legacy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_check_number", [2, 3])
async def test_receipt_checker_cannot_replace_effect_callback(case, backend_factory, monkeypatch, receipt_check_number):
    from openjiuwen.agent_teams.tools.database import (
        MemberEffectPermit,
        MemberWriteCommittedButUnconfirmed,
        MemberWriteReceipt,
    )
    from openjiuwen.core.controller.schema.execution_origin import execution_origin_scope
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    c = case
    permits, delivered, replacement = [], [], []

    def bind(operation, origin):
        permit = MemberEffectPermit(
            operation, origin, c.entity, c.actor, "alice-source", lambda: None, delivered.append
        )
        permits.append(permit)
        return permit

    object.__setattr__(c.authorizer, "bind_for_effect", bind)
    actual = MemberWriteReceipt.check_current
    checks = 0

    def changing_check(receipt):
        nonlocal checks
        actual(receipt)
        checks += 1
        if checks == receipt_check_number:
            object.__setattr__(permits[0], "on_committed", replacement.append)

    monkeypatch.setattr(MemberWriteReceipt, "check_current", changing_check)
    with execution_origin_scope(c.origin), pytest.raises(MemberWriteCommittedButUnconfirmed):
        await backend_factory(c.db).spawn_member("new-member", "New", AgentCard(name="new-member"), desc="body")
    assert not replacement
    assert len(delivered) == (0 if receipt_check_number == 2 else 1)
    assert (await c.db.member.get_member("new-member", "same-team")).record_revision == 1
