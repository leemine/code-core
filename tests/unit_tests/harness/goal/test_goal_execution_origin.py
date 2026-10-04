"""Original Goal admission survives snapshots and supervisor context changes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from openjiuwen.core.controller.schema.execution_origin import (
    ExecutionOrigin,
    current_execution_origin,
    execution_origin_scope,
)
from openjiuwen.harness.goal.schema import GoalAssessment, GoalAssessmentStatus, GoalRecord, GoalStatus
from .test_goal_manager import ManagerHarness


@pytest.fixture
def case():
    h = ManagerHarness()
    live = SimpleNamespace(value=True)

    def check():
        if not live.value:
            raise PermissionError("original synthetic Goal source revoked")

    source = ExecutionOrigin(object(), _checker=check)
    outputs = []
    h.manager._execution._emit_event = lambda event: outputs.append((event, current_execution_origin()))
    return SimpleNamespace(h=h, source=source, live=live, outputs=outputs)


async def set_goal(case):
    with execution_origin_scope(case.source):
        return await case.h.manager.set("ordinary synthetic Goal")


@pytest.mark.asyncio
async def test_set_work_and_output_retain_source_after_admission_scope_exits(case):
    record = await set_goal(case)
    work = case.h.events.next_work()
    assert work.execution_origin is case.source
    assert case.outputs[0][1] is case.source
    assert current_execution_origin() is None
    assert GoalRecord.from_dict(record.to_dict()) == record
    assert "execution_origin" not in json.dumps(asdict(record))
    assert "execution_origin" not in json.dumps(case.h.session.get_state("harness.goal.record"))


@pytest.mark.asyncio
async def test_supervisor_ensure_masks_foreign_ambient_and_uses_original(case):
    await set_goal(case)
    original = case.h.events.next_work()
    case.h.events.mark_started(original)
    case.h.events.mark_finished(original)
    with execution_origin_scope(ExecutionOrigin(object())):
        assert case.h.manager.ensure_active_goal_work_locked()
    assert case.h.events.next_work().execution_origin is case.source


@pytest.mark.asyncio
async def test_continue_assessment_queues_next_attempt_and_output_with_original(case):
    record = await set_goal(case)
    first = case.h.events.next_work()
    case.h.events.mark_started(first)
    begun = await case.h.manager.begin_attempt(goal_id=record.goal_id, revision=record.revision, attempt_index=1)
    assert begun.attempt_count == 1
    case.h.events.mark_finished(first)
    with execution_origin_scope(ExecutionOrigin(object())):
        settled = await case.h.manager.apply_assessment(
            goal_id=record.goal_id,
            revision=record.revision,
            attempt_index=1,
            assessment=GoalAssessment(status=GoalAssessmentStatus.CONTINUE, evidence="synthetic continue"),
        )
    assert settled.last_assessed_attempt == 1
    assert case.h.events.next_work().execution_origin is case.source
    assert all(source is case.source for _, source in case.outputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ensure", "begin", "assessment", "usage"])
async def test_revocation_rejects_original_consumers_without_rebinding(case, operation):
    record = await set_goal(case)
    work = case.h.events.next_work()
    case.h.events.mark_started(work)
    if operation in {"assessment", "usage"}:
        await case.h.manager.begin_attempt(goal_id=record.goal_id, revision=record.revision, attempt_index=1)
    case.h.events.mark_finished(work)
    before = case.h.store.load().to_dict()
    case.live.value = False
    with execution_origin_scope(ExecutionOrigin(object())), pytest.raises(PermissionError):
        if operation == "ensure":
            case.h.manager.ensure_active_goal_work_locked()
        elif operation == "begin":
            await case.h.manager.begin_attempt(goal_id=record.goal_id, revision=record.revision, attempt_index=1)
        elif operation == "assessment":
            await case.h.manager.apply_assessment(
                goal_id=record.goal_id,
                revision=record.revision,
                attempt_index=1,
                assessment=GoalAssessment(status=GoalAssessmentStatus.CONTINUE, evidence="synthetic continue"),
            )
        else:
            await case.h.manager.accumulate_usage(
                goal_id=record.goal_id, revision=record.revision, attempt_index=1, input_tokens=5
            )
    assert case.h.store.load().to_dict() == before
    assert not case.h.events.has_pending_work()


@pytest.mark.asyncio
async def test_set_captures_before_lock_and_checks_after_wait(case):
    lock = case.h.manager._control_lock
    await lock.acquire()
    with execution_origin_scope(case.source):
        task = asyncio.create_task(case.h.manager.set("waited goal"))
        await asyncio.sleep(0)
    case.live.value = False
    lock.release()
    with pytest.raises(PermissionError):
        await task
    assert case.h.store.load() is None and not case.h.events.has_pending_work()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "begin", "assessment"])
async def test_commit_wait_revocation_prevents_return_dispatch_and_output(case, operation):
    record = None
    if operation != "set":
        record = await set_goal(case)
        work = case.h.events.next_work()
        case.h.events.mark_started(work)
        if operation == "assessment":
            await case.h.manager.begin_attempt(goal_id=record.goal_id, revision=record.revision, attempt_index=1)
        case.h.events.mark_finished(work)
    entered, release = asyncio.Event(), asyncio.Event()

    async def commit():
        entered.set()
        await release.wait()

    case.h.session.commit = commit
    output_count = len(case.outputs)
    with execution_origin_scope(case.source):
        if operation == "set":
            task = asyncio.create_task(case.h.manager.set("commit wait"))
        elif operation == "begin":
            task = asyncio.create_task(
                case.h.manager.begin_attempt(goal_id=record.goal_id, revision=record.revision, attempt_index=1)
            )
        else:
            task = asyncio.create_task(
                case.h.manager.apply_assessment(
                    goal_id=record.goal_id,
                    revision=record.revision,
                    attempt_index=1,
                    assessment=GoalAssessment(status=GoalAssessmentStatus.CONTINUE, evidence="synthetic continue"),
                )
            )
        await entered.wait()
    case.live.value = False
    release.set()
    with pytest.raises(PermissionError):
        await task
    assert not case.h.events.has_pending_work() and len(case.outputs) == output_count
    with pytest.raises(PermissionError):
        case.h.manager.ensure_active_goal_work_locked()


@pytest.mark.asyncio
async def test_expired_inherited_admission_cannot_become_legacy(case):
    gate = asyncio.Event()

    async def late():
        await gate.wait()
        await case.h.manager.set("late goal")

    with execution_origin_scope(case.source):
        task = asyncio.create_task(late())
    gate.set()
    with pytest.raises(RuntimeError, match="scope expired"):
        await task
    assert case.h.store.load() is None


@pytest.mark.asyncio
async def test_new_set_does_not_rewrite_old_work_source(case):
    old = await set_goal(case)
    work = case.h.events.next_work()
    case.h.events.mark_started(work)
    other = ExecutionOrigin(object())
    with execution_origin_scope(other):
        new = await case.h.manager.set("explicit new goal", overwrite_confirmed=True)
    assert old.goal_id != new.goal_id and work.execution_origin is case.source
    case.h.events.mark_finished(work)
    assert case.h.events.next_work().execution_origin is other


@pytest.mark.asyncio
async def test_legacy_goal_masks_foreign_supervisor_ambient(case):
    await case.h.manager.set("legacy goal")
    work = case.h.events.next_work()
    case.h.events.mark_started(work)
    case.h.events.mark_finished(work)
    with execution_origin_scope(case.source):
        case.h.manager.ensure_active_goal_work_locked()
    assert case.h.events.next_work().execution_origin is None


@pytest.mark.asyncio
async def test_managed_idle_resume_does_not_rebind_to_new_source(case):
    record = await set_goal(case)
    work = case.h.events.next_work()
    case.h.events.mark_started(work)
    case.h.events.mark_finished(work)
    await case.h.manager.pause()
    with execution_origin_scope(ExecutionOrigin(object())), pytest.raises(RuntimeError, match="new host admission"):
        await case.h.manager.resume()
    assert case.h.store.load().status is GoalStatus.PAUSED
    assert case.h.store.load().revision == record.revision
    assert not case.h.events.has_pending_work()


@pytest.mark.asyncio
async def test_inflight_pause_resume_keeps_original_source_revision_and_work(case):
    record = await set_goal(case)
    work = case.h.events.next_work()
    case.h.events.mark_started(work)
    await case.h.manager.pause()
    resumed = await case.h.manager.resume()
    assert resumed.revision == record.revision
    assert work.execution_origin is case.source
    assert not case.h.events.has_pending_work()
    assert all(source is case.source for _, source in case.outputs)


@pytest.mark.asyncio
async def test_unexpected_record_revision_rejects_original_binding(case):
    record = await set_goal(case)
    work = case.h.events.next_work()
    case.h.events.mark_started(work)
    case.h.events.mark_finished(work)
    record.revision += 1
    case.h.store.save(record)
    with pytest.raises(RuntimeError, match="identity changed"):
        case.h.manager.ensure_active_goal_work_locked()


@pytest.mark.asyncio
async def test_real_interaction_emitter_task_is_tagged_with_original_goal_source(case):
    from openjiuwen.harness.deep_agent import DeepAgent

    gate = asyncio.Event()
    seen = []

    async def emit(payload, *, expected_token):
        seen.append(expected_token)
        await gate.wait()

    owner = SimpleNamespace(
        _interaction_output=SimpleNamespace(current_token=lambda: "original-output", emit=emit),
        _interaction_emit_tasks=set(),
    )
    case.h.manager._execution._emit_event = lambda event: DeepAgent._emit_interaction_event(owner, event)
    await set_goal(case)
    tasks = tuple(owner._interaction_emit_tasks)
    assert len(tasks) == 1 and tasks[0]._jiuwen_execution_origin is case.source
    gate.set()
    await asyncio.gather(*tasks)
    assert seen == ["original-output"] and not owner._interaction_emit_tasks


def test_native_adapter_direct_admission_checks_source_before_queue(case):
    case.live.value = False
    record = GoalRecord.create(session_id="session-1", objective="direct goal")
    with execution_origin_scope(case.source), pytest.raises(PermissionError):
        case.h.manager._execution.ensure_work(record)
    assert not case.h.events.has_pending_work()


@pytest.mark.asyncio
async def test_rebuilt_manager_does_not_restore_live_authority_from_record(case):
    record = await set_goal(case)
    rebuilt = ManagerHarness()
    rebuilt.store.save(GoalRecord.from_dict(record.to_dict()))
    # Core intentionally cannot infer managed host mode from persisted data.
    # A managed host must block attach or supply a new explicit admission.
    with execution_origin_scope(case.source):
        assert rebuilt.manager.ensure_active_goal_work_locked()
    assert rebuilt.events.next_work().execution_origin is None
    assert (await rebuilt.manager.get()).goal_id == record.goal_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "clear"])
async def test_control_emit_rejection_does_not_claim_to_roll_back_persisted_action(case, action):
    record = await set_goal(case)
    before_emits = len(case.outputs)
    case.live.value = False
    with pytest.raises(PermissionError):
        await getattr(case.h.manager, action)()
    assert len(case.outputs) == before_emits
    assert not case.h.events.has_pending_work()
    if action == "pause":
        assert case.h.store.load().status is GoalStatus.PAUSED
    else:
        assert case.h.store.load() is None
        assert case.h.cancel_calls == [
            {"expected_goal_id": record.goal_id, "expected_run_kind": "goal", "reason": "goal_clear"}
        ]


@pytest.mark.asyncio
async def test_old_goal_cancel_tail_cannot_write_or_poison_replacement_slot(case):
    old = await set_goal(case)
    work = case.h.events.next_work()
    case.h.events.mark_started(work)
    await case.h.manager.begin_attempt(goal_id=old.goal_id, revision=old.revision, attempt_index=1)
    gate = asyncio.Event()
    tails = []

    async def cancel(**kwargs):
        assert kwargs["expected_goal_id"] == old.goal_id

        async def late():
            await gate.wait()
            assert await case.h.manager.begin_attempt(goal_id=old.goal_id, revision=old.revision) is None
            assert (
                await case.h.manager.apply_assessment(
                    goal_id=old.goal_id,
                    revision=old.revision,
                    attempt_index=1,
                    assessment=GoalAssessment(status=GoalAssessmentStatus.COMPLETE, evidence="old cancelled result"),
                )
                is None
            )
            await case.h.manager.accumulate_usage(
                goal_id=old.goal_id, revision=old.revision, input_tokens=99, attempt_index=1
            )

        with execution_origin_scope(case.source):
            tails.append(asyncio.create_task(late()))

    case.h.manager._execution._cancel_active_round = cancel
    new_source = ExecutionOrigin(object())
    with execution_origin_scope(new_source):
        new = await case.h.manager.set("replacement", overwrite_confirmed=True)
    before = case.h.store.load().to_dict()
    case.live.value = False
    gate.set()
    await asyncio.gather(*tails)
    assert case.h.store.load().to_dict() == before
    assert new.goal_id != old.goal_id
    case.h.events.mark_finished(work)
    assert case.h.events.next_work().execution_origin is new_source
    assert [source for _, source in case.outputs] == [case.source, new_source]


@pytest.mark.asyncio
async def test_replacement_rechecks_new_source_after_old_cancel_wait(case):
    old = await case.h.manager.set("legacy old goal")
    entered, release = asyncio.Event(), asyncio.Event()

    async def cancel(**kwargs):
        assert kwargs["expected_goal_id"] == old.goal_id
        entered.set()
        await release.wait()

    case.h.manager._execution._cancel_active_round = cancel
    with execution_origin_scope(case.source):
        task = asyncio.create_task(case.h.manager.set("managed replacement", overwrite_confirmed=True))
        await entered.wait()
    replacement = case.h.store.load()
    assert replacement.goal_id != old.goal_id
    case.live.value = False
    release.set()
    with pytest.raises(PermissionError):
        await task
    work = case.h.events.next_work()
    assert work.execution_origin is case.source
    with pytest.raises(PermissionError):
        work.execution_origin._check_current()
