"""Original SQLite transactions with live host authority, no provider/network."""

import asyncio
import copy
import json
import pickle
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, text, update

from openjiuwen.agent_teams.monitor.models import MemberInfo
from openjiuwen.agent_teams.schema.status import MemberStatus
from openjiuwen.agent_teams.tools.database import (
    DatabaseConfig,
    MemberRecordAuthorizer,
    MemberRecordDenied,
    MemberWritePermit,
    MemberWriteReceipt,
    TeamDatabase,
    member_record_stamp,
)
from openjiuwen.agent_teams.tools.models import TeamMember
from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope


@pytest_asyncio.fixture
async def case(tmp_path):
    c = SimpleNamespace(actor=object(), entity=object(), receipts={}, hook=None, enabled=True)
    c.origin = ExecutionOrigin(c.actor)

    def bind(op, origin):
        if origin is not c.origin or op.database is not c.db or not c.enabled:
            raise MemberRecordDenied("wrong original owner")
        actor, entity = c.actor, c.entity
        old = c.receipts.get(op.member_name)

        def check(before, proposed):
            if c.actor is not actor or c.entity is not entity or not c.enabled:
                raise MemberRecordDenied("owner changed")
            if c.hook:
                c.hook(op, before, proposed)

        return MemberWritePermit(op, origin, entity, actor, "alice-source", old.stamp if old else None, check)

    c.authorizer = MemberRecordAuthorizer(bind)
    c.config = DatabaseConfig(connection_string=str(tmp_path / "team.sqlite"))
    c.db = TeamDatabase(c.config, member_record_authorizer=c.authorizer)
    await c.db.initialize()
    await c.db.team.create_team("same-team", "Team", "member")

    async def create():
        with execution_origin_scope(c.origin):
            receipt = await c.db.member.create_member(
                "member",
                "same-team",
                "Alice",
                "{}",
                "ready",
                execution_status="idle",
                options=json.dumps({"model_ref": {"model_name": "a"}, "fallback_model_ref": {"model_name": "b"}}),
                return_receipt=True,
            )
        c.receipts["member"] = receipt
        return receipt

    c.create = create
    try:
        yield c
    finally:
        await c.db.close()


async def mutate(c, kind, *, receipt=True):
    dao = c.db.member
    kw = {"return_receipt": receipt}
    if kind == "status":
        return await dao.update_member_status("member", "same-team", "busy", **kw)
    if kind == "transition":
        return await dao.try_transition_member_status(
            "member", "same-team", MemberStatus.READY, MemberStatus.BUSY, **kw
        )
    if kind == "execution":
        return await dao.update_member_execution_status("member", "same-team", "starting", **kw)
    if kind == "reset":
        return await dao.reset_member_execution_status("member", "same-team", "idle", **kw)
    if kind == "worktree":
        return await dao.update_member_worktree(
            "member", "same-team", isolation="worktree", worktree_path="/fixture", **kw
        )
    return await dao.promote_member_fallback_model("member", "same-team", **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["status", "transition", "execution", "reset", "worktree", "fallback"])
async def test_authorized_mutations_keep_record_identity_and_advance_revision(case, kind):
    c = case
    first = await c.create()
    with execution_origin_scope(c.origin):
        changed = await mutate(c, kind)
    assert type(changed) is MemberWriteReceipt
    with execution_origin_scope(c.origin):
        changed.check_current()
    assert changed.stamp.nonce == first.stamp.nonce
    assert changed.stamp.revision == 2
    assert changed._transaction is not first._transaction
    assert changed.operation.database is c.db
    row = await c.db.member.get_member("member", "same-team")
    info = MemberInfo.from_internal(row)
    assert info.record_stamp == changed.stamp
    assert "record_stamp" not in info.model_dump()
    assert MemberInfo.model_validate(info.model_dump()).record_stamp is None
    assert "record_nonce" not in info.model_dump_json()
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        await mutate(c, kind)  # original receipt is now stale


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["status", "transition", "execution", "reset", "worktree", "fallback", "delete"])
async def test_other_owner_cannot_write_even_knowing_original_stamp(case, kind):
    c = case
    old = await c.create()
    with execution_origin_scope(ExecutionOrigin(object())), pytest.raises(MemberRecordDenied):
        if kind == "delete":
            await c.db.team.delete_team("same-team")
        else:
            await mutate(c, kind)
    assert member_record_stamp(await c.db.member.get_member("member", "same-team")) == old.stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["status", "transition", "execution", "reset", "worktree", "fallback", "delete"])
async def test_second_legacy_dao_cannot_downgrade_governed_row(case, kind):
    c = case
    old = await c.create()
    legacy = TeamDatabase(c.config)
    await legacy.initialize()
    try:
        if kind in ("worktree", "fallback", "delete"):
            with pytest.raises(MemberRecordDenied):
                if kind == "delete":
                    await legacy.team.delete_team("same-team")
                else:
                    await mutate(SimpleNamespace(db=legacy), kind)
        else:
            assert await mutate(SimpleNamespace(db=legacy), kind) is False
        assert member_record_stamp(await c.db.member.get_member("member", "same-team")) == old.stamp
    finally:
        await legacy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["overwrite", "recreate"])
async def test_same_id_row_replacement_never_validates_original_receipt(case, mutation):
    c = case
    old = await c.create()
    # Direct SQL is a trusted-process escape, not a permitted host writer. This
    # reproduces the observed global-row pollution and checks read-side evidence.
    async with c.db._sessions.write() as tx:
        if mutation == "overwrite":
            await tx.execute(update(TeamMember).values(display_name="Bob", status="busy"))
        else:
            await tx.execute(delete(TeamMember))
        await tx.commit()
    if mutation == "recreate":
        c.receipts.clear()
        new = await c.create()
        assert new.stamp.nonce != old.stamp.nonce
        c.receipts["member"] = old
    else:
        with pytest.raises(MemberRecordDenied):
            MemberInfo.from_internal(await c.db.member.get_member("member", "same-team"))
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        await mutate(c, "status")


@pytest.mark.asyncio
async def test_delete_cascade_checks_original_owner_and_recreate_gets_new_nonce(case):
    c = case
    old = await c.create()
    with execution_origin_scope(c.origin):
        assert await c.db.team.delete_team("same-team") is True
    assert await c.db.member.get_member("member", "same-team") is None
    await c.db.team.create_team("same-team", "Team", "member")
    c.receipts.clear()
    new = await c.create()
    assert new.stamp.nonce != old.stamp.nonce
    assert new.stamp.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["nonce", "revision", "source_id"])
async def test_wrong_original_stamp_rejected(case, field):
    c = case
    old = await c.create()
    wrong = replace(old.stamp, **{field: {"nonce": "0" * 64, "revision": 123, "source_id": "bob"}[field]})
    c.receipts["member"] = SimpleNamespace(stamp=wrong)
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        await mutate(c, "status")


@pytest.mark.asyncio
async def test_failure_after_update_before_commit_rolls_back_original_transaction(case, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.dml import Update

    c = case
    old = await c.create()
    after_sql = False
    execute = AsyncSession.execute

    async def record_sql(session, statement, *args, **kwargs):
        nonlocal after_sql
        result = await execute(session, statement, *args, **kwargs)
        if isinstance(statement, Update):
            after_sql = True
        return result

    monkeypatch.setattr(AsyncSession, "execute", record_sql)

    def late_revoke(op, before, proposed):
        if after_sql:
            raise MemberRecordDenied("revoked after SQL, before commit")

    c.hook = late_revoke
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        await mutate(c, "status")
    assert after_sql
    assert member_record_stamp(await c.db.member.get_member("member", "same-team")) == old.stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["operation", "authorizer", "expected_stamp", "entity"])
async def test_callback_reference_drift_is_not_committed(case, mutation):
    c = case
    await c.create()

    def drift(op, before, proposed):
        if mutation == "operation":
            object.__setattr__(op, "member_name", "other")
        elif mutation == "authorizer":
            object.__setattr__(c.authorizer, "bind_for_write", lambda *_: None)
        elif mutation == "expected_stamp" and before:
            object.__setattr__(before, "revision", 900)
        elif mutation == "entity":
            c.entity = object()

    c.hook = drift
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        await mutate(c, "status")
    row = await c.db.member.get_member("member", "same-team")
    assert row.status == "ready" and row.record_revision == 1


@pytest.mark.asyncio
async def test_create_requires_source_receipt_live_only_and_flags_strict(case):
    c = case
    with pytest.raises(MemberRecordDenied):
        await c.db.member.create_member("member", "same-team", "A", "{}", "ready")
    receipt = await c.create()
    for item in (c.authorizer, receipt, receipt._permit, receipt.operation):
        assert copy.deepcopy(item) is item
        with pytest.raises(TypeError):
            pickle.dumps(item)
    with pytest.raises(TypeError):
        MemberWriteReceipt()
    with execution_origin_scope(c.origin), pytest.raises(TypeError):
        await mutate(c, "status", receipt=1)


@pytest.mark.asyncio
async def test_queued_source_expiry_prevents_write(case):
    c = case
    old = await c.create()
    await c.db._sessions._write_lock.acquire()
    try:
        with execution_origin_scope(c.origin):
            task = asyncio.create_task(mutate(c, "status"))
            await asyncio.sleep(0)
    finally:
        c.db._sessions._write_lock.release()
    with pytest.raises(MemberRecordDenied):
        await task
    assert member_record_stamp(await c.db.member.get_member("member", "same-team")) == old.stamp


@pytest.mark.asyncio
async def test_legacy_nullable_rows_not_adopted_and_legacy_still_works(case):
    c = case
    legacy = TeamDatabase(c.config)
    await legacy.initialize()
    try:
        assert await legacy.member.create_member("member", "same-team", "Legacy", "{}", "ready") is True
        assert await legacy.member.update_member_status("member", "same-team", "busy") is True
        with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
            await mutate(c, "reset")
        assert await legacy.team.delete_team("same-team") is True
    finally:
        await legacy.close()


@pytest.mark.asyncio
async def test_original_receipt_cas_allows_one_concurrent_writer(case):
    c = case
    await c.create()
    with execution_origin_scope(c.origin):
        results = await asyncio.gather(mutate(c, "status"), mutate(c, "status"), return_exceptions=True)
    assert sum(type(value) is MemberWriteReceipt for value in results) == 1
    assert sum(isinstance(value, MemberRecordDenied) for value in results) == 1
    assert (await c.db.member.get_member("member", "same-team")).record_revision == 2


@pytest.mark.asyncio
async def test_precommit_cancel_rolls_back_member_create(case, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    c = case
    original_flush = AsyncSession.flush

    async def cancelled(session, *args, **kwargs):
        await original_flush(session, *args, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(AsyncSession, "flush", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await c.create()
    monkeypatch.setattr(AsyncSession, "flush", original_flush)
    assert await c.db.member.get_member("member", "same-team") is None


@pytest.mark.asyncio
async def test_commit_failure_does_not_return_receipt_or_leave_row(case, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    c = case
    original_commit = AsyncSession.commit

    async def broken(session):
        raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(AsyncSession, "commit", broken)
    with pytest.raises(RuntimeError, match="synthetic"):
        await c.create()
    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    assert not c.receipts
    assert await c.db.member.get_member("member", "same-team") is None


@pytest.mark.asyncio
async def test_nullable_migration_is_idempotent_and_does_not_claim_existing_rows(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine, inspect

    from openjiuwen.agent_teams.tools.database.engine import _ensure_team_member_record_columns

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE team_member (member_name TEXT, team_name TEXT)")
        conn.execute("INSERT INTO team_member VALUES ('member', 'same-team')")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _ensure_team_member_record_columns(conn)
            _ensure_team_member_record_columns(conn)
            assert len(inspect(conn).get_columns("team_member")) == 6
            row = conn.execute(
                text("SELECT record_nonce,record_source_id,record_revision,record_digest FROM team_member")
            ).one()
            assert tuple(row) == (None, None, None, None)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_default_bool_still_enforces_authority(case):
    c = case
    await c.create()
    with execution_origin_scope(c.origin):
        assert await mutate(c, "status", receipt=False) is True
    with execution_origin_scope(ExecutionOrigin(object())), pytest.raises(MemberRecordDenied):
        await mutate(c, "reset", receipt=False)


@pytest.mark.asyncio
async def test_late_team_delete_authority_failure_rolls_back_members_and_parent(case):
    c = case
    old = await c.create()
    calls = 0

    def fail_before_commit(op, before, proposed):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise MemberRecordDenied("original parent replaced")

    c.hook = fail_before_commit
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        await c.db.team.delete_team("same-team")
    assert await c.db.team.team_exists("same-team")
    assert member_record_stamp(await c.db.member.get_member("member", "same-team")) == old.stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_bulk_cleanup_rejects_before_any_mixed_or_dynamic_deletion(case, partial):
    from openjiuwen.agent_teams.tools.database.engine import cleanup_all_runtime_state

    c = case
    await c.create()
    legacy = TeamDatabase(c.config)
    await legacy.initialize()
    try:
        await legacy.member.create_member("legacy", "same-team", "Legacy", "{}", "ready")
        async with c.db.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE team_task_fixture (marker TEXT)"))
            await conn.execute(text("INSERT INTO team_task_fixture VALUES ('preserved')"))
            if partial:
                await conn.execute(
                    update(TeamMember).where(TeamMember.member_name == "member").values(record_digest=None)
                )
        with pytest.raises(MemberRecordDenied):
            await cleanup_all_runtime_state(legacy.engine)
        async with c.db.engine.begin() as conn:
            assert (await conn.execute(text("SELECT marker FROM team_task_fixture"))).scalar() == "preserved"
        assert await c.db.member.get_member("legacy", "same-team") is not None
        assert await c.db.member.get_member("member", "same-team") is not None
        assert await c.db.team.team_exists("same-team")
    finally:
        await legacy.close()


@pytest.mark.asyncio
async def test_repeated_fallback_promotion_preserves_false_noop(case):
    c = case
    await c.create()
    with execution_origin_scope(c.origin):
        promoted = await mutate(c, "fallback")
        c.receipts["member"] = promoted
        assert await mutate(c, "fallback") is False
    assert member_record_stamp(await c.db.member.get_member("member", "same-team")) == promoted.stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["database", "database_sessions", "dao_sessions"])
@pytest.mark.parametrize("phase", ["source", "authorizer"])
async def test_first_source_checker_cannot_redirect_original_database(case, tmp_path, drift, phase):
    c = case
    await c.create()
    other = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "other.sqlite")))
    await other.initialize()
    await other.team.create_team("same-team", "Different database", "member")
    row = await c.db.member.get_member("member", "same-team")
    async with other._sessions.write() as tx:
        tx.add(TeamMember(**row.model_dump()))
        await tx.commit()
    original_guard = c.db._member_record_writes
    original_sessions = c.db._sessions
    original_other_guard = other._member_record_writes

    def switch():
        if drift == "database":
            original_guard.database = other
            other._member_record_writes = original_guard
        elif drift == "database_sessions":
            c.db._sessions = other._sessions
        c.db.member._sessions = other._sessions

    original_bind = c.authorizer.bind_for_write
    if phase == "source":
        object.__setattr__(c.origin, "_checker", switch)
    else:

        def bind_then_switch(op, origin):
            permit = original_bind(op, origin)
            switch()
            return permit

        object.__setattr__(c.authorizer, "bind_for_write", bind_then_switch)
    try:
        with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
            await mutate(c, "status")
    finally:
        object.__setattr__(c.origin, "_checker", None)
        object.__setattr__(c.authorizer, "bind_for_write", original_bind)
        c.db._sessions = original_sessions
        c.db.member._sessions = original_sessions
        original_guard.database = c.db
        other._member_record_writes = original_other_guard
        try:
            for db in (c.db, other):
                unchanged = await db.member.get_member("member", "same-team")
                assert unchanged.status == "ready" and unchanged.record_revision == 1
        finally:
            await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
@pytest.mark.parametrize("return_receipt", [False, True])
async def test_commit_return_after_source_change_is_not_current_success(case, monkeypatch, operation, return_receipt):
    from sqlalchemy.ext.asyncio import AsyncSession

    from openjiuwen.agent_teams.tools.database import MemberWriteCommittedButUnconfirmed

    c = case
    if operation != "create":
        old = await c.create()
    actual_commit = AsyncSession.commit

    async def commit_then_revoke(session):
        await actual_commit(session)
        c.enabled = False

    monkeypatch.setattr(AsyncSession, "commit", commit_then_revoke)
    with execution_origin_scope(c.origin), pytest.raises(MemberWriteCommittedButUnconfirmed) as caught:
        if operation == "create":
            await c.db.member.create_member(
                "member", "same-team", "Alice", "{}", "ready", return_receipt=return_receipt
            )
        elif operation == "update":
            await mutate(c, "status", receipt=return_receipt)
        else:
            await c.db.team.delete_team("same-team")
    receipt = caught.value.receipt
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        receipt.check_current()
    assert type(receipt) is MemberWriteReceipt
    assert receipt.operation.database is c.db
    assert receipt.operation._dao in (c.db.member, c.db.team)
    assert receipt.operation._sessions is c.db._sessions
    row = await c.db.member.get_member("member", "same-team")
    if operation == "delete":
        assert row is None and receipt.stamp == old.stamp
        assert receipt.operation.kind == "delete_team"
    else:
        assert member_record_stamp(row) == receipt.stamp
        assert receipt.stamp.revision == (1 if operation == "create" else 2)
    with pytest.raises(TypeError):
        pickle.dumps(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["stamp", "_transaction", "_source_check"])
async def test_committed_receipt_checks_issuance_facts_not_modified_fields(case, field):
    c = case
    receipt = await c.create()
    value = replace(receipt.stamp, revision=900) if field == "stamp" else (lambda: None)
    object.__setattr__(receipt, field, value)
    with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
        receipt.check_current()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["source", "permit", "transaction"])
@pytest.mark.parametrize("target", ["mapper", "table"])
@pytest.mark.parametrize("operation", ["status", "delete_team"])
async def test_mapper_routing_cannot_replace_original_sql_sink(case, tmp_path, monkeypatch, phase, target, operation):
    from sqlalchemy.ext.asyncio import AsyncSession

    c = case
    await c.create()
    other = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "routing-other.sqlite")))
    await other.initialize()
    await other.team.create_team("same-team", "Different DB", "member")
    row = await c.db.member.get_member("member", "same-team")
    async with other._sessions.write() as tx:
        tx.add(TeamMember(**row.model_dump()))
        await tx.commit()
    factory = c.db.session_local
    original_kw = dict(factory.kw)
    key = TeamMember if target == "mapper" else TeamMember.__table__

    def redirect():
        factory.configure(binds={key: other.engine})

    if phase == "source":
        object.__setattr__(c.origin, "_checker", redirect)
    elif phase == "permit":
        c.hook = lambda *_: redirect()
    else:
        actual_execute = AsyncSession.execute

        async def execute_then_redirect(session, *args, **kwargs):
            result = await actual_execute(session, *args, **kwargs)
            if session.bind is c.db.engine:
                if target == "mapper":
                    session.sync_session.bind_mapper(TeamMember, other.engine.sync_engine)
                else:
                    session.sync_session.bind_table(TeamMember.__table__, other.engine.sync_engine)
            return result

        monkeypatch.setattr(AsyncSession, "execute", execute_then_redirect)
    try:
        with execution_origin_scope(c.origin), pytest.raises(MemberRecordDenied):
            if operation == "delete_team":
                await c.db.team.delete_team("same-team")
            else:
                await mutate(c, "status")
    finally:
        object.__setattr__(c.origin, "_checker", None)
        c.hook = None
        factory.kw.clear()
        factory.kw.update(original_kw)
        monkeypatch.undo()
        try:
            for db in (c.db, other):
                unchanged = await db.member.get_member("member", "same-team")
                assert unchanged.status == "ready" and unchanged.record_revision == 1
                assert await db.team.team_exists("same-team")
        finally:
            await other.close()


@pytest.mark.asyncio
async def test_bulk_cleanup_reserves_database_before_protected_row_check(case, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncConnection

    c = case
    legacy = TeamDatabase(c.config)
    await legacy.initialize()
    entered, release, create_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = AsyncConnection.run_sync

    async def gated(conn, function, *args, **kwargs):
        if function.__name__ == "_clear_table" and conn.engine is legacy.engine:
            entered.set()
            await release.wait()
        return await original(conn, function, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "run_sync", gated)
    cleanup = asyncio.create_task(legacy.cleanup_all_runtime_state())

    async def create():
        create_started.set()
        return await c.create()

    creator = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        creator = asyncio.create_task(create())
        await create_started.wait()
        done, _ = await asyncio.wait({creator}, timeout=0.1)
        assert not done, "independent managed create must wait for original DB transaction"
        release.set()
        await asyncio.wait_for(cleanup, 3)
        result = (await asyncio.wait_for(asyncio.gather(creator, return_exceptions=True), 3))[0]
        # SQLite may enforce the removed parent FK or allow the create after
        # cleanup. Neither ordering can confirm a receipt and then erase it.
        if isinstance(result, MemberWriteReceipt):
            current = await c.db.member.get_member("member", "same-team")
            assert member_record_stamp(current) == result.stamp
        else:
            from sqlalchemy.exc import IntegrityError

            assert result is False or isinstance(result, IntegrityError)
    finally:
        release.set()
        await asyncio.gather(cleanup, *([creator] if creator else []), return_exceptions=True)
        await legacy.close()


@pytest.mark.asyncio
async def test_committed_facts_survive_original_scope_without_authorizing_background_read(case):
    c = case
    receipt = await c.create()
    calls = []
    object.__setattr__(c.origin, '_checker', lambda: calls.append('writer'))
    c.enabled = False
    facts = await asyncio.to_thread(receipt.committed_facts)
    assert calls == []
    assert facts.database is c.db and facts.dao is c.db.member and facts.sessions is c.db._sessions
    assert facts.actor is c.actor and facts.entity is c.entity
    assert facts.kind == 'create' and facts.team_name == 'same-team' and facts.member_name == 'member'
    assert facts.source_id == 'alice-source' and facts.stamp == receipt.stamp
    assert dict(facts.record)['display_name'] == 'Alice'
    receipt.check_integrity()
    with pytest.raises(MemberRecordDenied):
        receipt.check_current()
    assert copy.copy(facts) is facts and copy.deepcopy(facts) is facts
    with pytest.raises(TypeError):
        pickle.dumps(facts)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['record', 'stamp', 'operation', 'actor', 'database', 'transaction', 'type'])
async def test_pure_committed_facts_reject_field_changes_not_only_stamp(case, change):
    c = case
    receipt = await c.create()
    facts = receipt.committed_facts()
    if change == 'record':
        object.__setattr__(facts, 'record', tuple((k, 'someone else' if k == 'display_name' else v)
                                                 for k, v in facts.record))
    elif change == 'stamp':
        object.__setattr__(facts.stamp, 'revision', 2)
    elif change == 'operation':
        object.__setattr__(receipt.operation, 'team_name', 'other-team')
    elif change == 'type':
        object.__setattr__(facts.stamp, 'revision', True)
    else:
        object.__setattr__(facts, change, object())
    with pytest.raises(MemberRecordDenied):
        receipt.check_integrity()
    with pytest.raises(MemberRecordDenied):
        receipt.committed_facts()


@pytest.mark.asyncio
async def test_receipt_copied_fields_cannot_forge_original_issuance(case):
    receipt = await case.create()
    fake = object.__new__(MemberWriteReceipt)
    for name in MemberWriteReceipt.__slots__:
        object.__setattr__(fake, name, getattr(receipt, name))
    with pytest.raises(MemberRecordDenied):
        fake.committed_facts()
    blank = object.__new__(MemberWriteReceipt)
    with pytest.raises(MemberRecordDenied):
        blank.check_integrity()
    assert receipt.committed_facts().stamp.revision == 1


@pytest.mark.asyncio
async def test_committed_row_snapshot_updates_only_from_actual_transaction(case):
    c = case
    created = await c.create()
    with execution_origin_scope(c.origin):
        updated = await mutate(c, 'status')
        c.receipts['member'] = updated
    before, after = created.committed_facts(), updated.committed_facts()
    assert before.stamp.revision == 1 and after.stamp.revision == 2
    assert dict(before.record)['status'] == 'ready'
    assert dict(after.record)['status'] == 'busy'
    with execution_origin_scope(c.origin):
        # A delete has the same original immutable row, and remains only a fact.
        assert await c.db.team.delete_team('same-team') is True
    assert updated.committed_facts() is after
    assert await c.db.member.get_member('member', 'same-team') is None
