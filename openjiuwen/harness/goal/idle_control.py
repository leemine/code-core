# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Private idle pause/clear. Controller authority never becomes execution source."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from openjiuwen.harness.goal.readmission import _facts, _sync


@dataclass(repr=False)
class _IdleControlResult:
    task: object = None
    action: str | None = None
    expected: object = None
    slot: object = None
    result: object = None
    result_facts: object = None


@dataclass(frozen=True, eq=False, repr=False)
class _IdleGoalControl:
    selector: object
    command_lock: object
    check_current: object
    check_native: object
    _run: _IdleControlResult = field(default_factory=_IdleControlResult)

    def check(self, **_ignored):
        s, p = self.selector, self._run
        current, native, lock = self.check_current, self.check_native, self.command_lock
        task, action, expected, slot = p.task, p.action, p.expected, p.slot
        result, result_facts = p.result, p.result_facts

        def exact():
            if (self.selector is not s or self._run is not p or self.check_current is not current
                    or self.check_native is not native or self.command_lock is not lock
                    or p.task is not task or p.action != action or p.expected is not expected
                    or p.slot is not slot or p.result is not result or p.result_facts is not result_facts
                    or (result is not None and _facts(result) != result_facts)):
                raise PermissionError("idle Goal control operation changed")
            native()
            s.check_static(facts=expected, slot=slot, initial=task is None)
            s.check_quiescent()

        exact()
        _sync(current)
        exact()

    def saved(self, record):
        # Called synchronously by the original Manager, immediately after save.
        if record is None:
            self.selector.manager._execution_origin = None
        self._run.expected = _facts(record)
        self._run.slot = self.selector.manager._execution_origin

    def discard(self):
        self.check()  # Already empty: never touch another operation's queues.

    def cancel(self):
        self.check()  # Already exited: no Session abort or new cancellation.

    def _emit_updated(self, _record):
        # The original RPC result is the idle control reply. It owns no output
        # lease and must not publish through the previous execution's emitter.
        self.check()

    async def apply(self, *, action):
        if action not in {'pause', 'clear'}:
            raise ValueError("idle Goal control only supports pause/clear")
        p = self._run
        if p.task is None:
            self.check()
            p.expected, p.slot, p.action = self.selector.original, self.selector.slot, action
            p.task = asyncio.create_task(self._apply())
        elif p.action != action:
            raise PermissionError("idle Goal control cannot change action on retry")
        await asyncio.shield(p.task)
        self.check_result()
        return p.result.copy_for_response()

    async def _apply(self):
        s, p = self.selector, self._run
        async with self.command_lock:
            async with s.send_lock:
                async with s.lock:
                    self.check()
                    if p.task is not asyncio.current_task():
                        raise PermissionError("idle Goal operation task changed")
                    operation = s.manager._pause_locked if p.action == 'pause' else s.manager._clear_locked
                    result = await operation(control=self)
                    self.check()
                    p.result, p.result_facts = result, _facts(result)
        self.check()

    def check_result(self):
        p = self._run
        if p.task is None or not p.task.done() or p.task.cancelled():
            raise PermissionError("idle Goal result is not confirmed")
        p.task.result()
        if p.result is None:
            raise PermissionError("idle Goal control requires its original existing Goal")
        self.check()
