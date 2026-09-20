# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-process Native integration ports; never serialized into provider config."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.interaction import SendInputRequest
    from openjiuwen.harness_protocol import HarnessContext, HarnessInput


@dataclass(frozen=True, slots=True)
class NativeHostHooks:
    """Host-specific construction and dispatch, inside the provider lifecycle.

    ``create_session`` supplies a fresh session, without calling pre_run/start.
    The harness owns pre_run, agent start/stop and session post_run, including
    startup rollback. ``before_start`` installs the host's input guard after
    pre_run and before agent start.

    ``dispatch_input`` runs after the sole output lease is acquired. It may
    translate the default request to a host-owned request preserving Python
    object identity, or dispatch a Native Goal command. True means drain the
    acquired stream; False means this command did not start output-producing
    work (e.g. a rejected Goal operation). On steer the existing lease is used.
    The final argument distinguishes an interrupt continuation from the first
    dispatch. Hooks must not start another event consumer or call harness stop.
    """

    create_session: Callable[[HarnessContext, DeepAgent], Awaitable[Any]] | None = field(default=None, repr=False)
    before_start: Callable[[DeepAgent, Any], Awaitable[None]] | None = field(default=None, repr=False)
    dispatch_input: Callable[[DeepAgent, SendInputRequest, HarnessInput, bool], Awaitable[bool]] | None = field(
        default=None, repr=False
    )


_AGENT_OWNERS: WeakKeyDictionary = WeakKeyDictionary()
_OWNER_LOCK = RLock()


def claim_agent(agent: Any, owner: object) -> None:
    """Prevent two protocol harnesses claiming the same unstarted instance."""
    with _OWNER_LOCK:
        current = _AGENT_OWNERS.get(agent)
        if current is not None and current is not owner:
            raise RuntimeError("Native agent already belongs to another harness")
        _AGENT_OWNERS[agent] = owner


def release_agent(agent: Any, owner: object) -> None:
    """Release only the matching owner after agent shutdown has completed."""
    with _OWNER_LOCK:
        if _AGENT_OWNERS.get(agent) is owner:
            del _AGENT_OWNERS[agent]
