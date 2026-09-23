# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve SubagentControl instances scoped to a parent execution session."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.harness.subagent_runtime.control import SubagentControl
from openjiuwen.harness.subagent_runtime.ports import SubagentExecutionFactory

_CONTROL_ATTR = "_subagent_controls"


def get_subagent_control(
    parent_agent: Any,
    session: Any,
    *,
    execution_factory: SubagentExecutionFactory | None = None,
) -> SubagentControl:
    """Return or create the SubagentControl for a parent session."""
    get_session_id = getattr(session, "get_session_id", None)
    if not callable(get_session_id):
        raise build_error(
            StatusCode.TOOL_SESSION_TOOL_INVOKED,
            reason="subagent tools require a valid session in kwargs",
        )
    parent_session_id = str(get_session_id() or "")
    if not parent_session_id:
        raise build_error(
            StatusCode.TOOL_SESSION_TOOL_INVOKED,
            reason="subagent tools require a non-empty parent session id",
        )
    controls = getattr(parent_agent, _CONTROL_ATTR, None)
    if controls is None:
        controls = {}
        setattr(parent_agent, _CONTROL_ATTR, controls)
    control = controls.get(parent_session_id)
    if control is None:
        control = SubagentControl(
            parent_agent,
            parent_session_id,
            parent_session=session,
            execution_factory=execution_factory,
        )
        control.hydrate()
        controls[parent_session_id] = control
    else:
        if (
            execution_factory is not None
            and control.execution_factory is not execution_factory
        ):
            raise build_error(
                StatusCode.TOOL_SESSION_TOOL_INVOKED,
                reason="subagent execution factory cannot change within a parent session",
            )
        control.set_parent_session(session)
    control.merge_persisted_records()
    return control


async def release_subagent_control(
    parent_agent: Any,
    parent_session_id: str,
    reason: str = "parent_ended",
) -> None:
    """Cancel all subagents and drop the cached control for a parent session."""
    controls = getattr(parent_agent, _CONTROL_ATTR, None) or {}
    control = controls.get(parent_session_id)
    if control is not None:
        await control.cancel_all(reason)
        if controls.get(parent_session_id) is control:
            controls.pop(parent_session_id, None)


async def release_all_subagent_controls(
    parent_agent: Any,
    reason: str = "rail_uninit",
) -> None:
    """Cancel all subagents for every cached parent session on an agent."""
    controls = getattr(parent_agent, _CONTROL_ATTR, None) or {}
    session_ids = list(controls.keys())
    for parent_session_id in session_ids:
        await release_subagent_control(parent_agent, parent_session_id, reason=reason)


__all__ = [
    "_CONTROL_ATTR",
    "get_subagent_control",
    "release_all_subagent_controls",
    "release_subagent_control",
]
