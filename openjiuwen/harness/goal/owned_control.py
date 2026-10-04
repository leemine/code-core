# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Private original-Goal control receipt, never a persisted authority or queue."""

from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import dataclass, field

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope


def _identity(record):
    # Accounting/status may legitimately progress without changing this Goal.
    return (
        None
        if record is None
        else (
            record.session_id,
            record.goal_id,
            record.revision,
            record.objective,
            record.token_budget,
            record.max_attempts,
        )
    )


@dataclass(repr=False)
class _Progress:
    task: object = None
    signature: object = None
    checker: object = None
    ack_checker: object = None
    expected: object = None
    slot: object = None
    cancellation: object = None
    applied: bool = False
    result: object = None
    result_facts: object = None


@dataclass(frozen=True, eq=False, repr=False)
class _OwnedGoalControl:
    manager: object
    store: object
    execution: object
    lock: object
    session_id: str
    origin: ExecutionOrigin
    target: object
    original: object
    slot: object
    _run: _Progress = field(default_factory=_Progress)

    def _check_static(self, *, initial=False):
        m, p = self.manager, self._run
        expected, slot = (self.original, self.slot) if initial else (p.expected, p.slot)
        if (
            m._store is not self.store
            or m._execution is not self.execution
            or m._control_lock is not self.lock
            or self.store.session_id != self.session_id
            or m._execution_origin is not slot
            or _identity(self.store.load()) != expected
        ):
            raise PermissionError("original Goal control target changed")
        if slot is not None and slot[3] is not self.origin:
            raise PermissionError("original Goal source changed")

    def check(self, *, initial=False, live=False, mutation=False):
        self.origin._check_current()
        result = self._run.checker()
        if inspect.iscoroutine(result):
            result.close()
        if result is not None:
            raise TypeError("Goal control checker must synchronously return None")
        # All callback code has finished. Recheck references, record and source
        # without invoking another callback before the actual mutation.
        self._check_static(initial=initial)
        self.execution._check_owned_control(self.target, live=live, mutation=mutation)

    def check_result(self):
        p = self._run
        if p.task is None or not p.applied:
            raise PermissionError("Goal control has no completed mutation")
        exit_task = p.cancellation
        ack = p.ack_checker
        if exit_task is not None and (
            not exit_task.done() or exit_task.cancelled() or exit_task.exception() is not None
        ):
            raise PermissionError("original Goal exit is unconfirmed")
        if p.signature[0] != "clear" or ack is None or exit_task is None:
            self.check()
            return
        # This is the Task returned by the original exact drain, not an EOF or
        # arbitrary terminal Future. No execution source is revived for an ACK.
        signature, expected, slot = p.signature, p.expected, p.slot
        operation, applied, result, facts = p.task, p.applied, p.result, p.result_facts
        frozen_facts = copy.deepcopy(facts)

        def exact():
            if (
                p.cancellation is not exit_task
                or p.ack_checker is not ack
                or p.signature != signature
                or p.expected != expected
                or p.slot is not slot
                or p.task is not operation
                or p.applied is not applied
                or p.result is not result
                or p.result_facts is not facts
                or facts != frozen_facts
                or (None if result is None else result.to_dict()) != frozen_facts
            ):
                raise PermissionError("original Goal clear result changed")
            self._check_static()
            self.execution._check_owned_control(self.target)

        exact()
        acknowledgement = ack()
        if inspect.iscoroutine(acknowledgement):
            acknowledgement.close()
        if acknowledgement is not None:
            raise TypeError("Goal ACK checker must synchronously return None")
        exact()

    def saved(self, record):
        self._run.expected = _identity(record)
        self._run.slot = self.manager._execution_origin

    def discard(self):
        self.execution._discard_owned_control(self.target)

    def cancel(self):
        if self._run.cancellation is None:
            self._run.cancellation = self.execution._start_owned_control_exit(self.target)


def capture(manager, origin):
    if not isinstance(origin, ExecutionOrigin) or origin._checker is None:
        raise PermissionError("Goal control requires its original live source")
    store, execution, lock = manager._store, manager._execution, manager._control_lock
    record, slot, session_id = store.load(), manager._execution_origin, store.session_id
    if record is not None and (
        slot is None or slot[:3] != (record.session_id, record.goal_id, record.revision) or slot[3] is not origin
    ):
        raise PermissionError("Goal control source does not own the record")
    target = execution._capture_owned_control(record, origin)
    result = _OwnedGoalControl(manager, store, execution, lock, session_id, origin, target, _identity(record), slot)
    result._check_static(initial=True)
    execution._check_owned_control(target, live=True)
    return result


async def apply(
    manager,
    target,
    *,
    action,
    check_current,
    check_ack=None,
    objective=None,
    overwrite_confirmed=False,
    token_budget=None,
    max_attempts=None,
):
    if type(target) is not _OwnedGoalControl or target.manager is not manager:
        raise PermissionError("original Goal control selector required")
    if (
        type(action) is not str
        or action not in {"set", "pause", "clear", "resume"}
        or not callable(check_current)
        or (check_ack is not None and not callable(check_ack))
        or type(overwrite_confirmed) is not bool
        or (objective is not None and type(objective) is not str)
        or any(v is not None and (type(v) is not int or v <= 0) for v in (token_budget, max_attempts))
    ):
        raise ValueError("invalid owned Goal control")
    if action != "set" and (
        objective is not None or overwrite_confirmed or token_budget is not None or max_attempts is not None
    ):
        raise ValueError("unexpected owned Goal control parameters")
    signature = (action, objective, overwrite_confirmed, token_budget, max_attempts)
    p = target._run

    async def finish():
        with execution_origin_scope(target.origin):
            if p.cancellation is not None:
                await asyncio.shield(p.cancellation)
            async with target.lock:
                target.check_result()
                return p.result

    if p.task is not None:
        if p.signature != signature or p.checker is not check_current or p.ack_checker is not check_ack:
            raise PermissionError("Goal control retry differs from original operation")
        if p.applied and p.task.done() and not p.task.cancelled() and p.task.exception() is not None:
            # Only retry the original exit/ack, never replay a durable mutation.
            # A failed exit task is retained until a caller explicitly retries.
            if (
                p.cancellation is not None
                and p.cancellation.done()
                and not p.cancellation.cancelled()
                and p.cancellation.exception() is None
            ):
                target.check_result()
            else:
                target.check()
            if (
                p.cancellation is not None
                and p.cancellation.done()
                and (p.cancellation.cancelled() or p.cancellation.exception() is not None)
            ):
                p.cancellation = target.execution._start_owned_control_exit(target.target)
            p.task = asyncio.create_task(finish())
    else:
        p.signature, p.checker, p.ack_checker = signature, check_current, check_ack
        p.expected, p.slot = target.original, target.slot

        async def run():
            with execution_origin_scope(target.origin):
                async with target.lock:
                    target.check(initial=True, live=True)
                    if action == "set":
                        manager._validate_set(objective or "", token_budget, max_attempts)
                        result = await manager._set_locked(
                            (objective or "").strip(),
                            overwrite_confirmed=overwrite_confirmed,
                            token_budget=token_budget,
                            max_attempts=max_attempts,
                            origin=target.origin,
                            control=target,
                        )
                    else:
                        if action == "resume":
                            target.execution._require_owned_attempt(target.target)
                        result = await getattr(manager, "_" + action + "_locked")(control=target)
                p.applied, p.result = True, result
                p.result_facts = None if result is None else copy.deepcopy(result.to_dict())
                return await finish()

        p.task = asyncio.create_task(run())
    # Caller timeout/cancellation is unknown, not permission to drop the same
    # operation or resend cancellation into an original finally block.
    result = await asyncio.shield(p.task)
    target.check_result()  # A cached successful result is not a fresh authorization.
    return None if result is None else result.copy_for_response()
