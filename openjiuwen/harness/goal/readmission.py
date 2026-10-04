# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Private fresh-Goal admission; persisted Goal data is never an authority."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope
from openjiuwen.harness.goal.schema import GoalRecord, GoalStatus
from openjiuwen.harness_protocol import freeze_json_object


def _facts(record):
    return None if record is None else freeze_json_object(record.to_dict())


def _sync(check):
    value = check()
    if inspect.iscoroutine(value):
        value.close()
    if value is not None:
        raise TypeError("Goal admission checker must synchronously return None")


@dataclass(frozen=True, eq=False, repr=False)
class _IdleGoalReadmission:
    manager: object
    store: object
    execution: object
    lock: object
    agent: object
    session: object
    controller: object
    events: object
    output: object
    send_lock: object
    original: object
    slot: object

    def check_static(self, *, facts=None, slot=None, initial=True):
        m, a = self.manager, self.agent
        if (
            m._store is not self.store
            or self.store._session is not self.session
            or m._execution is not self.execution
            or m._control_lock is not self.lock
            or a.goal_manager is not m
            or self.execution._owner is not a
            or self.execution._event_manager is not self.events
            or a._interaction_session is not self.session
            or a.loop_controller is not self.controller
            or a._event_manager is not self.events
            or a._interaction_control_lock is not self.lock
            or a._interaction_send_lock is not self.send_lock
            or a._interaction_output is not self.output
            or not a._is_interaction_running()
            or self.store.session_id != self.session.get_session_id()
            or m._execution_origin is not (self.slot if initial else slot)
            or _facts(self.store.load()) != (self.original if initial else facts)
        ):
            raise PermissionError("original idle Goal admission target changed")

    def check_quiescent(self):
        a, e = self.agent, self.events
        if (
            e._user_queue
            or e._goal_queue
            or e._dequeued is not None
            or e.active_work is not None
            or a._active_interaction_round is not None
            or (a._interaction_round_task is not None and not a._interaction_round_task.done())
            or any(not task.done() for task in a._stopping_interaction_round_tasks)
            or any(not task.done() for task in a._interaction_emit_tasks)
            or a._invoke_active
            or a._auto_invoke_scheduled
            or (a._stream_process_task is not None and not a._stream_process_task.done())
        ):
            raise PermissionError("original Goal execution inventories are not quiescent")
        if a.load_state(self.session).pending_follow_ups:
            raise PermissionError("legacy Goal follow-up ownership is unknown")
        c = self.controller
        if c is not None:
            queues = c._get_interaction_queues()
            if queues is not None and (not queues.steering.empty() or not queues.follow_up.empty()):
                raise PermissionError("original Goal input remains queued")
            scheduler = c.task_scheduler
            if scheduler is not None:
                from openjiuwen.core.controller.schema.task import TaskStatus

                terminal = {TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.FAILED}
                if any(not task.done() for task in scheduler._owned_execution_tasks.values()) or any(
                    task.session_id == self.session.get_session_id() and task.status not in terminal
                    for task in scheduler._task_manager.tasks.values()
                ):
                    raise PermissionError("original Goal scheduler exit is unconfirmed")
        a._check_unattributed_subagent_exit(self.session)


def capture(manager, expected_record):
    if type(expected_record) is not GoalRecord:
        raise TypeError("idle Goal admission requires an exact Goal record")
    execution, store, lock = manager._execution, manager._store, manager._control_lock
    a = getattr(execution, "_owner", None)
    if a is None:
        raise PermissionError("idle Goal admission requires a Native execution owner")
    target = _IdleGoalReadmission(
        manager,
        store,
        execution,
        lock,
        a,
        a._interaction_session,
        a.loop_controller,
        a._event_manager,
        a._interaction_output,
        a._interaction_send_lock,
        _facts(expected_record),
        manager._execution_origin,
    )
    target.check_static()
    if target.slot is not None and (
        target.slot[:3] != (expected_record.session_id, expected_record.goal_id, expected_record.revision)
        or not isinstance(target.slot[3], ExecutionOrigin)
    ):
        raise PermissionError("original Goal live source does not match its record")
    return target


@dataclass(repr=False)
class _AdmissionProgress:
    task: object = None
    origin: object = None
    previous_check: object = None
    result: object = None
    result_facts: object = None
    stream: object = None
    signature: object = None
    slot: object = None


@dataclass(frozen=True, eq=False, repr=False)
class _NativeGoalReadmissionPlan:
    """Host-private intent. Only Native can supply the original exit proof."""

    selector: _IdleGoalReadmission
    action: str
    check_current: object
    previous_turn: object = None
    _run: _AdmissionProgress = field(default_factory=_AdmissionProgress)

    def result(self):
        p = self._run
        if p.task is None or not p.task.done() or p.task.cancelled():
            raise PermissionError("Goal readmission has no completed result")
        p.task.result()
        from openjiuwen.harness.goal.owned_control import _identity

        task, origin, result, facts, checker = p.task, p.origin, p.result, p.result_facts, self.check_current
        signature, slot = p.signature, p.slot
        selector = signature[0]
        current = selector.store.load()
        current_facts = _facts(current)
        if _identity(current) != _identity(result):
            raise PermissionError("Goal readmission result no longer identifies the stored Goal")
        _sync(checker)
        origin._check_current()
        if (
            self._run is not p
            or p.task is not task
            or p.origin is not origin
            or p.result is not result
            or p.result_facts is not facts
            or _facts(result) != facts
            or self.check_current is not checker
            or self.selector is not selector
            or self.action != signature[1]
            or self.previous_turn is not signature[3]
            or p.signature is not signature
            or p.slot is not slot
        ):
            raise PermissionError("Goal readmission result changed")
        selector.check_static(facts=current_facts, slot=slot, initial=False)
        return result.copy_for_response()


async def attach(agent, plan, origin, check_previous):
    if (
        type(plan) is not _NativeGoalReadmissionPlan
        or type(plan.selector) is not _IdleGoalReadmission
        or plan.selector.agent is not agent
        or plan.action not in {"resume", "attach"}
        or not isinstance(origin, ExecutionOrigin)
        or not callable(plan.check_current)
    ):
        raise PermissionError("invalid explicit Goal readmission")
    p = plan._run
    if p.task is None:
        p.origin, p.previous_check = origin, check_previous
        p.signature = (plan.selector, plan.action, plan.check_current, plan.previous_turn)
        p.task = asyncio.create_task(_attach(plan, origin, check_previous))
    elif p.origin is not origin or p.previous_check is not check_previous:
        raise PermissionError("Goal readmission operation was rebound")
    stream = await asyncio.shield(p.task)
    return stream


async def _attach(plan, origin, check_previous):
    s, p, action, checker = plan.selector, plan._run, plan.action, plan.check_current
    m, a = s.manager, s.agent
    expected, slot = s.original, s.slot
    original_plan = (plan.selector, plan.action, plan.check_current, plan.previous_turn, plan._run)

    def check(*, idle=True):
        origin._check_current()
        _sync(checker)
        _sync(check_previous)
        if (
            plan.selector is not original_plan[0]
            or plan.action != action
            or plan.check_current is not checker
            or plan.previous_turn is not original_plan[3]
            or plan._run is not p
            or p.origin is not origin
            or p.previous_check is not check_previous
            or p.task is not asyncio.current_task()
        ):
            raise PermissionError("Goal readmission input changed")
        s.check_static(facts=expected, slot=slot, initial=False)
        if idle:
            s.check_quiescent()

    with execution_origin_scope(origin):
        async with s.send_lock:
            async with s.lock:
                check()
                record = s.store.load()
                if (action == "resume" and record.status not in {GoalStatus.PAUSED, GoalStatus.BLOCKED}) or (
                    action == "attach" and record.status is not GoalStatus.ACTIVE
                ):
                    raise PermissionError("Goal readmission action does not match stored status")
                stream = await a._attach_output_locked()
                if stream is None:
                    raise PermissionError("Goal output already has its original consumer")
                p.stream = stream
                work = None
                try:
                    check()
                    if action == "resume":
                        record.settle_active_time(keep_active=False)
                        record.status = GoalStatus.ACTIVE
                        record.start_timing()
                        record.touch(bump_revision=True)
                    slot = (record.session_id, record.goal_id, record.revision, origin)
                    m._execution_origin = slot
                    p.slot = slot
                    if action == "resume":
                        s.store.save(record)
                    expected = _facts(record)
                    check()
                    if action == "resume":
                        await m._commit_store_locked()
                        check()
                    # The adapter's original work constructor is reused. Its
                    # after-source check is the last synchronous guard before
                    # insertion into the original EventManager queue.
                    work = m._execution._ensure_readmitted_work(record, check)
                    check(idle=False)
                    m._execution.goal_updated(record.copy_for_response())
                    check(idle=False)
                    p.result = record.copy_for_response()
                    p.result_facts = _facts(p.result)
                    return stream
                except BaseException:
                    if work is not None:
                        s.events._discard_captured_work((work,))
                    # Do not call generic detach_output: it discards other work.
                    await s.output.detach(stream._lease.token)
                    raise


async def attach_existing(agent, origin):
    """Ordinary managed attach must not bootstrap a persisted legacy Goal."""
    if not isinstance(origin, ExecutionOrigin):
        raise PermissionError("managed output requires its exact Pending source")
    send_lock, lock = agent._interaction_send_lock, agent._interaction_control_lock
    session, output = agent._interaction_session, agent._interaction_output
    async with send_lock:
        async with lock:
            manager = agent.goal_manager
            store = None if manager is None else manager._store
            slot = None if manager is None else manager._execution_origin
            record = None if store is None else store.load()
            facts = _facts(record)

            def check():
                origin._check_current()
                if (
                    agent._interaction_send_lock is not send_lock
                    or agent._interaction_control_lock is not lock
                    or agent._interaction_session is not session
                    or agent._interaction_output is not output
                    or agent.goal_manager is not manager
                    or not agent._is_interaction_running()
                    or (
                        manager is not None
                        and (
                            manager._store is not store
                            or store._session is not session
                            or manager._execution_origin is not slot
                            or _facts(store.load()) != facts
                        )
                    )
                ):
                    raise PermissionError("managed output owner changed")
                if (
                    record is not None
                    and record.status is GoalStatus.ACTIVE
                    and (
                        slot is None
                        or slot[:3] != (record.session_id, record.goal_id, record.revision)
                        or slot[3] is not origin
                    )
                ):
                    raise PermissionError("stored ACTIVE Goal requires explicit readmission")

            check()
            stream = await agent._attach_output_locked()
            try:
                check()
                if stream is not None and record is not None and record.status is GoalStatus.ACTIVE:
                    manager.ensure_active_goal_work_locked()
                    check()
                    agent._notify_work()
                return stream
            except BaseException:
                if stream is not None:
                    await output.detach(stream._lease.token)
                raise
