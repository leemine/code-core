# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native-private old exit proof; never a new execution authority."""

from openjiuwen.harness_protocol import HarnessStateError


def capture_previous_exit(harness, selector, previous):
    """Pin the original objects before a controller callback can run."""
    from openjiuwen.harness_providers.native.harness import _NativePendingTurn, _NativeTurnExit

    agent, session, slot = selector.agent, selector.session, selector.slot
    old = previous
    barrier = None if old is None else getattr(old, '_exit', None)
    round_handle = None if barrier is None else barrier.round_handle
    cleanup = None if barrier is None else barrier.cleanup
    confirmed = None if barrier is None else barrier.confirmed
    admissions = () if barrier is None else barrier.admissions
    interactions = () if barrier is None else barrier.interactions

    def check():
        if (harness._agent is not agent or harness._agent_session is not session
                or type(old) is not _NativePendingTurn or old._harness_owner is not harness
                or old._agent is not agent or old._session is not session
                or slot is None or old._origin is not slot[3]
                or type(barrier) is not _NativeTurnExit or old._exit is not barrier
                or barrier.turn is not old or barrier.round_handle is not round_handle
                or barrier.cleanup is not cleanup or barrier.confirmed is not confirmed
                or barrier.admissions is not admissions or barrier.interactions is not interactions
                or not old._execution_done.is_set() or old._admissions
                or confirmed is None or not confirmed.done() or confirmed.cancelled()
                or cleanup is None or not cleanup.done() or cleanup.cancelled()
                or round_handle is None or round_handle.agent is not agent
                or round_handle.session is not session or round_handle.origin is not old._origin):
            raise HarnessStateError("original Goal Pending exit is unconfirmed")
        confirmed.result()
        cleanup.result()
        if (any(not task.done() for task in admissions)
                or harness._capture_turn_interactions(old)
                or any(not entry.handle_done.is_set() or entry.handling
                       or (entry.cancel_task is not None and (
                           not entry.cancel_task.done() or entry.cancel_task.cancelled()
                           or entry.cancel_task.exception() is not None)) for entry in interactions)
                or agent._event_manager._capture_origin_work(old._origin)
                or agent._capture_owned_round(old._origin) is not None):
            raise HarnessStateError("original Goal Pending still owns work")
        agent._check_origin_exit(round_handle)
    return check


def capture_idle(harness, *, expected_record, previous_turn, check_current):
    from openjiuwen.harness.goal.idle_control import _IdleGoalControl

    agent, session = harness._agent, harness._agent_session
    command_lock, context, hooks = harness._command_lock, harness._context, harness._host_hooks
    first = harness._first_managed_turn
    buffer, source_hook = harness._event_buffer, hooks.capture_execution_origin
    if agent is None or session is None or hooks.capture_execution_origin is None or not callable(check_current):
        raise HarnessStateError("idle Goal control requires a managed Native Session and controller")
    selector = agent.goal_manager._capture_idle_readmission(expected_record=expected_record)
    previous_check = None if previous_turn is None else capture_previous_exit(harness, selector, previous_turn)

    def check_native():
        harness._require_accepting()
        if (harness._agent is not agent or harness._agent_session is not session
                or selector.agent is not agent or selector.session is not session
                or harness._command_lock is not command_lock or harness._context is not context
                or harness._host_hooks is not hooks or hooks.capture_execution_origin is not source_hook
                or harness._event_buffer is not buffer
                or harness._first_managed_turn is not first
                or harness._active_turn is not None or harness._pending or harness._pending_interactions
                or harness._cleanup_pending):
            raise HarnessStateError("original idle Native controller target changed")
        if previous_check is None:
            if selector.slot is not None or first is not None:
                raise HarnessStateError("cold idle Goal control requires a fresh managed Session")
        else:
            previous_check()

    result = _IdleGoalControl(selector, command_lock, check_current, check_native)
    result.check()
    return result
